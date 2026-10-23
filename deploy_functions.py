"""Run after approved infrastructure setup and private image upload."""
import sys,json,pathlib,datetime as dt
from oci_local_access import *
import oci.resource_scheduler as scheduler

path=pathlib.Path('deployment.json');state=json.loads(path.read_text())
def save():path.write_text(json.dumps(state,indent=2))
def ensure(key,creator):
    if key not in state:
        value=creator();state[key]=value.id;save();print(key,value.id,flush=True)
    return state[key]
fc=client(oci.functions.FunctionsManagementClient)
comp=state['compartment_id'];identity=client(oci.identity.IdentityClient)
shared={'TENANCY_ID':state['tenancy_id'],'STORAGE_NAMESPACE':state['namespace'],
    'SNAPSHOT_BUCKET':state['bucket'],'IDENTITY_DOMAIN_URL':state['identity_domain_url'],
    'OAUTH_CLIENT_ID':state['oauth_client_id'],'OAUTH_SCOPE':state['oauth_scope'],
    'APP_URL':state['app_url'],'API_URL':state['api_url']}
oci.wait_until(fc,fc.get_application(state['application_id']),'lifecycle_state','ACTIVE',max_wait_seconds=300)
for role,memory,timeout in [('web',256,30),('api',512,60),('collector',1024,300)]:
    key=role+'_function_id'
    details=oci.functions.models.CreateFunctionDetails(display_name='oci-limits-'+role,application_id=state['application_id'],
        image=state['image'],image_digest=state['image_digest'],memory_in_mbs=memory,timeout_in_seconds=timeout,
        detached_mode_timeout_in_seconds=900 if role=='collector' else None,config={**shared,'APP_ROLE':role},
        freeform_tags={'application':'oci-limits-dashboard','role':role})
    ensure(key,lambda d=details:fc.create_function(d).data)
    oci.wait_until(fc,fc.get_function(state[key]),'lifecycle_state','ACTIVE',max_wait_seconds=300)
for role in ['collector','api']:
    ensure(role+'_dynamic_group_id',lambda role=role:identity.create_dynamic_group(oci.identity.models.CreateDynamicGroupDetails(
        compartment_id=state['tenancy_id'],name='oci-limits-'+role,description='Only the OCI limits '+role+' function',
        matching_rule="ALL {resource.type='fnfunc', resource.id='"+state[role+'_function_id']+"'}")).data)
statements=[
    'Allow dynamic-group oci-limits-collector to read limits in tenancy',
    'Allow dynamic-group oci-limits-collector to inspect resource-availability in tenancy',
    'Allow dynamic-group oci-limits-collector to read resource-availability in tenancy',
    f"Allow dynamic-group oci-limits-collector to {{OBJECT_CREATE, OBJECT_OVERWRITE}} in compartment Sandbox where target.bucket.name='{state['bucket']}'",
    f"Allow dynamic-group oci-limits-api to {{OBJECT_READ}} in compartment Sandbox where target.bucket.name='{state['bucket']}'",
    f"Allow any-user to use fn-invocation in compartment Sandbox where all {{request.principal.type='ApiGateway', request.principal.id='{state['gateway_id']}', any {{target.function.id='{state['web_function_id']}', target.function.id='{state['api_function_id']}'}}}}",
]
ensure('runtime_policy_id',lambda:identity.create_policy(oci.identity.models.CreatePolicyDetails(compartment_id=state['tenancy_id'],name='oci-limits-dashboard-runtime',description='Read-only limits collector, private snapshot access, and exact gateway invocation',statements=statements)).data)
policy_response=identity.get_policy(state['runtime_policy_id'])
if policy_response.data.statements!=statements:
    identity.update_policy(state['runtime_policy_id'],oci.identity.models.UpdatePolicyDetails(statements=statements),if_match=policy_response.headers['etag'])
dc=client(oci.apigateway.DeploymentClient)
M=oci.apigateway.models
for role,prefix in [('web','/oci-limits'),('api','/oci-limits-data')]:
    authentication=None
    if role=='api':
        authentication=M.JwtAuthenticationPolicy(token_header='Authorization',token_auth_scheme='Bearer',is_anonymous_access_allowed=False,
            issuers=['https://identity.oraclecloud.com','https://identity.oraclecloud.com/'],audiences=[state['oauth_audience']],
            public_keys=M.StaticPublicKeySet(keys=[M.JsonWebKey(**{**key,'key_ops':['verify']}) for key in json.load(open('public-signing-keys.json'))['keys']]),
            verify_claims=[M.JsonWebTokenClaim(key='scope',values=[state['oauth_scope']],is_required=True)])
    spec=M.ApiSpecification(request_policies=M.ApiSpecificationRequestPolicies(authentication=authentication,
        rate_limiting=M.RateLimitingPolicy(rate_in_requests_per_second=10,rate_key='CLIENT_IP')),
        routes=[M.ApiSpecificationRoute(path='/',methods=['GET','POST'] if role=='web' else ['POST'],
            backend=M.OracleFunctionBackend(function_id=state[role+'_function_id']))])
    existed=role+'_deployment_id' in state
    ensure(role+'_deployment_id',lambda role=role,prefix=prefix,spec=spec:dc.create_deployment(M.CreateDeploymentDetails(
        compartment_id=comp,gateway_id=state['gateway_id'],display_name='OCI Limits '+role,path_prefix=prefix,specification=spec)).data)
    if existed and role=='api':
        oci.wait_until(dc,dc.get_deployment(state[role+'_deployment_id']),'lifecycle_state','ACTIVE',max_wait_seconds=600)
        current=dc.get_deployment(state[role+'_deployment_id'])
        dc.update_deployment(state[role+'_deployment_id'],M.UpdateDeploymentDetails(specification=spec),if_match=current.headers['etag'])
    oci.wait_until(dc,dc.get_deployment(state[role+'_deployment_id']),'lifecycle_state','ACTIVE',max_wait_seconds=600)
sc=client(scheduler.ScheduleClient)
start=dt.datetime.now(dt.timezone.utc).replace(hour=12,minute=0,second=0,microsecond=0)
if start<=dt.datetime.now(dt.timezone.utc):start+=dt.timedelta(days=1)
ensure('schedule_id',lambda:sc.create_schedule(scheduler.models.CreateScheduleDetails(compartment_id=comp,
    display_name='oci-limits-daily-refresh',description='Pull complete tenancy limits and usage daily at 12:00 UTC',
    action='START_RESOURCE',recurrence_type='CRON',recurrence_details='0 12 * * *',time_starts=start,
    resources=[scheduler.models.Resource(id=state['collector_function_id'])])).data)
ensure('schedule_dynamic_group_id',lambda:identity.create_dynamic_group(oci.identity.models.CreateDynamicGroupDetails(
    compartment_id=state['tenancy_id'],name='oci-limits-daily-schedule',description='Only the daily limits resource schedule',
    matching_rule="ALL {resource.type='resourceschedule', resource.id='"+state['schedule_id']+"'}")).data)
ensure('schedule_policy_id',lambda:identity.create_policy(oci.identity.models.CreatePolicyDetails(compartment_id=state['tenancy_id'],
    name='oci-limits-daily-invocation',description='Daily schedule may invoke only the limits collector',statements=[
    f"Allow dynamic-group oci-limits-daily-schedule to use fn-invocation in compartment Sandbox where target.function.id='{state['collector_function_id']}'",
    f"Allow dynamic-group oci-limits-daily-schedule to read fn-function in compartment Sandbox where target.function.id='{state['collector_function_id']}'"])).data)
save()
print('Deployed browser URL:',state['app_url'],flush=True)
print('IAM membership can take time to propagate. Invoke the collector and verify the published snapshot before announcing the app is live.',flush=True)
