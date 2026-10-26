"""Configure 80% service-limit alerts for the current OCI Administrators group.

Creating an EMAIL subscription sends an OCI confirmation message to that address.
Run from the deployment directory with an existing deployment.json.
"""
import argparse
import json
from pathlib import Path
import oci
from oci_local_access import client, config, all_pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--administrator-group', default='Administrators')
    args = parser.parse_args()
    path = Path('deployment.json')
    state = json.loads(path.read_text())
    identity = client(oci.identity.IdentityClient)
    groups = [g for g in all_pages(identity.list_groups, config['tenancy']) if g.name == args.administrator_group]
    if len(groups) != 1:
        raise RuntimeError('Provide one exact administrator group name')
    emails = set()
    for membership in all_pages(identity.list_user_group_memberships,
            compartment_id=config['tenancy'], group_id=groups[0].id):
        user = identity.get_user(membership.user_id).data
        if user.lifecycle_state == 'ACTIVE' and user.email and '@' in user.email:
            emails.add(user.email.strip().lower())
    if not emails:
        raise RuntimeError('No active administrators with email addresses found; no subscriptions created')
    control = client(oci.ons.NotificationControlPlaneClient, state['region'])
    name = 'oci-service-limits-80-alerts'
    topics = all_pages(control.list_topics, state['compartment_id'])
    topic = next((t for t in topics if t.name == name), None)
    if topic is None:
        topic = control.create_topic(oci.ons.models.CreateTopicDetails(
            compartment_id=state['compartment_id'], name=name,
            description='Administrator alerts naming service limits at or above 80% utilization',
            freeform_tags={'application': 'oci-limits-dashboard'})).data
    topic = control.get_topic(topic.topic_id).data
    state.update(notification_topic_id=topic.topic_id, notification_endpoint=topic.api_endpoint)
    path.write_text(json.dumps(state, indent=2))
    signer = oci.signer.Signer(tenancy=config['tenancy'], user=config['user'],
        fingerprint=config['fingerprint'], private_key_file_location=config['key_file'],
        pass_phrase=config.get('pass_phrase'))
    data = oci.ons.NotificationDataPlaneClient(config, signer=signer,
        service_endpoint=topic.api_endpoint, timeout=(10, 30), retry_strategy=oci.retry.NoneRetryStrategy())
    existing = all_pages(data.list_subscriptions, state['compartment_id'], topic_id=topic.topic_id)
    for email in sorted(emails):
        if not any(x.protocol == 'EMAIL' and x.endpoint.lower() == email for x in existing):
            data.create_subscription(oci.ons.models.CreateSubscriptionDetails(
                compartment_id=state['compartment_id'], topic_id=topic.topic_id,
                protocol='EMAIL', endpoint=email))
    statements = [f'Allow dynamic-group oci-limits-collector to {{ONS_TOPIC_PUBLISH, ONS_SUBSCRIPTION_INSPECT}} in compartment id {state["compartment_id"]}']
    if state.get('notification_policy_id'):
        current = identity.get_policy(state['notification_policy_id'])
        if current.data.statements != statements:
            identity.update_policy(current.data.id, oci.identity.models.UpdatePolicyDetails(
                statements=statements), if_match=current.headers['etag'])
    else:
        state['notification_policy_id'] = identity.create_policy(oci.identity.models.CreatePolicyDetails(
            compartment_id=config['tenancy'], name='oci-limits-threshold-notifications',
            description='Limits collector may only publish notifications and inspect subscription readiness in Sandbox',
            statements=statements)).data.id
    functions = client(oci.functions.FunctionsManagementClient, state['region'])
    current = functions.get_function(state['collector_function_id'])
    settings = {**current.data.config, 'NOTIFICATION_TOPIC_ID':topic.topic_id,
        'NOTIFICATION_ENDPOINT':topic.api_endpoint, 'NOTIFICATION_COMPARTMENT_ID':state['compartment_id']}
    functions.update_function(current.data.id, oci.functions.models.UpdateFunctionDetails(config=settings),
        if_match=current.headers['etag'])
    oci.wait_until(functions, functions.get_function(current.data.id), 'lifecycle_state', 'ACTIVE', max_wait_seconds=300)
    path.write_text(json.dumps(state, indent=2))
    subscriptions = all_pages(data.list_subscriptions, state['compartment_id'], topic_id=topic.topic_id)
    report = {'topic_id':topic.topic_id, 'administrator_group':groups[0].name,
        'subscriptions':[{'email':x.endpoint, 'status':x.lifecycle_state} for x in subscriptions if x.protocol == 'EMAIL']}
    Path('notification-setup-state.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'topic':name, 'administrator_count':len(emails),
        'active':sum(x.lifecycle_state == 'ACTIVE' for x in subscriptions),
        'pending':sum(x.lifecycle_state == 'PENDING' for x in subscriptions)}), flush=True)
    print('Recipients must confirm their OCI subscription email. Rerun after administrator membership changes to add new recipients; review and remove departed recipients in OCI Notifications.', flush=True)


if __name__ == '__main__':
    main()
