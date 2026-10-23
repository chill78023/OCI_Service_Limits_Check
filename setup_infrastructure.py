import sys,json,pathlib,datetime as dt
from oci_local_access import *
from oci._vendor import requests

statepath=pathlib.Path('deployment.json')
state=json.loads(statepath.read_text()) if statepath.exists() else {}
inv=json.load(open('infrastructure-inventory.json'))
COMP=next(x['compartment_id'] for x in inv['us-ashburn-1']['subnets'])
REGION='us-ashburn-1';GATEWAY=next(g for g in inv[REGION]['gateways'] if g['display_name']=='test gateway')
state.update(compartment_id=COMP,region=REGION,tenancy_id=config['tenancy'],gateway_id=GATEWAY['id'],
    app_url='https://'+GATEWAY['hostname']+'/oci-limits/',api_url='https://'+GATEWAY['hostname']+'/oci-limits-data/')
def save():statepath.write_text(json.dumps(state,indent=2))
def ensure(key,creator):
    if key not in state:
        resource=creator();state[key]=resource.id;save();print(key,resource.id,flush=True)
    return state[key]
net=client(oci.core.VirtualNetworkClient)
vcn=next(s['vcn_id'] for s in inv[REGION]['subnets'] if s['display_name']=='FunctionSubnet')
vcn_response=net.get_vcn(vcn)
extra_cidr='10.90.0.0/24'
if extra_cidr not in vcn_response.data.cidr_blocks:
    net.add_vcn_cidr(vcn,oci.core.models.AddVcnCidrDetails(cidr_block=extra_cidr),if_match=vcn_response.headers['etag'])
    oci.wait_until(net,net.get_vcn(vcn),'lifecycle_state','AVAILABLE',max_wait_seconds=180)
    print('Added a dedicated address block; existing VCN addresses retained',flush=True)
nat=ensure('nat_gateway_id',lambda:net.create_nat_gateway(oci.core.models.CreateNatGatewayDetails(compartment_id=COMP,vcn_id=vcn,display_name='oci-limits-outbound')).data)
route=ensure('route_table_id',lambda:net.create_route_table(oci.core.models.CreateRouteTableDetails(compartment_id=COMP,vcn_id=vcn,display_name='oci-limits-private-routes',route_rules=[oci.core.models.RouteRule(destination='0.0.0.0/0',destination_type='CIDR_BLOCK',network_entity_id=nat)])).data)
subnet=ensure('subnet_id',lambda:net.create_subnet(oci.core.models.CreateSubnetDetails(compartment_id=COMP,vcn_id=vcn,display_name='oci-limits-private-subnet',cidr_block='10.90.0.0/25',prohibit_public_ip_on_vnic=True,route_table_id=route)).data)
fc=client(oci.functions.FunctionsManagementClient)
ensure('application_id',lambda:fc.create_application(oci.functions.models.CreateApplicationDetails(compartment_id=COMP,display_name='OCI Limits Dashboard',shape='GENERIC_X86',subnet_ids=[subnet],freeform_tags={'application':'oci-limits-dashboard'})).data)
store=client(oci.object_storage.ObjectStorageClient)
state['namespace']=store.get_namespace().data
state['bucket']='oci-limits-dashboard-snapshots'
if not state.get('bucket_created'):
    try:store.get_bucket(state['namespace'],state['bucket'])
    except oci.exceptions.ServiceError as exc:
        if exc.status!=404:raise
        store.create_bucket(state['namespace'],oci.object_storage.models.CreateBucketDetails(name=state['bucket'],compartment_id=COMP,public_access_type='NoPublicAccess',storage_tier='Standard',versioning='Disabled',freeform_tags={'application':'oci-limits-dashboard'}))
    state['bucket_created']=True;save();print('Private snapshot bucket ready',flush=True)
registry=client(oci.artifacts.ArtifactsClient)
ensure('repository_id',lambda:registry.create_container_repository(oci.artifacts.models.CreateContainerRepositoryDetails(compartment_id=COMP,display_name='sandbox/oci-limits-dashboard',is_public=False,freeform_tags={'application':'oci-limits-dashboard'})).data)
state['image_repository']='iad.ocir.io/'+state['namespace']+'/sandbox/oci-limits-dashboard'
identity=client(oci.identity.IdentityClient)
ensure('image_policy_id',lambda:identity.create_policy(oci.identity.models.CreatePolicyDetails(compartment_id=config['tenancy'],name='oci-limits-function-image-pull',description='Permit only the OCI Limits application to pull its private function image',statements=[f"Allow any-user to read repos in compartment Sandbox where all {{request.principal.type='fnapp', request.principal.id='{state['application_id']}', target.repo.name='sandbox/oci-limits-dashboard'}}"])).data)

domain=inv['domains'][0]['url'].removesuffix(':443');state['identity_domain_url']=domain
idc=oci.identity_domains.IdentityDomainsClient(config,service_endpoint=domain,timeout=(10,30))
audience='https://oci-limits-dashboard.sandbox/'
state['oauth_scope']=audience+'read';state['oauth_audience']=audience
resource_id=ensure('oauth_resource_id',lambda:idc.create_app(app=oci.identity_domains.models.App(
    schemas=['urn:ietf:params:scim:schemas:oracle:idcs:App'],display_name='OCI Limits Dashboard API',
    description='Resource audience for the private Sandbox utilization dashboard',is_o_auth_resource=True,
    audience=audience,access_token_expiry=3600,based_on_template=oci.identity_domains.models.AppBasedOnTemplate(value='CustomWebAppTemplateId'),
    scopes=[oci.identity_domains.models.AppScopes(value='read',description='Read the OCI limits and usage snapshot',requires_consent=True)])).data)
if not state.get('oauth_client_id'):
    app=idc.create_app(app=oci.identity_domains.models.App(schemas=['urn:ietf:params:scim:schemas:oracle:idcs:App'],
        display_name='OCI Limits Dashboard',description='Public PKCE browser sign-in. No client secret.',is_o_auth_client=True,
        client_type='public',allowed_grants=['authorization_code'],redirect_uris=[state['app_url']],
        allow_access_control=True,show_in_my_apps=True,landing_page_url=state['app_url'],
        based_on_template=oci.identity_domains.models.AppBasedOnTemplate(value='CustomWebAppTemplateId'),
        allowed_scopes=[oci.identity_domains.models.AppAllowedScopes(fqs=state['oauth_scope'],id_of_defining_app=resource_id)] )).data
    state['oauth_app_id']=app.id;state['oauth_client_id']=app.name;save();print('Public OAuth client registered',app.id,flush=True)
for key in ['oauth_resource_id','oauth_app_id']:
    idc.put_app_status_changer(state[key],app_status_changer=oci.identity_domains.models.AppStatusChanger(schemas=['urn:ietf:params:scim:schemas:oracle:idcs:AppStatusChanger'],active=True))
if not state.get('oauth_user_granted'):
    user=idc.list_users(filter='userName eq '+json.dumps(inv['dashboard_user_name']),attributes='id,userName').data.resources[0]
    grant=idc.create_grant(grant=oci.identity_domains.models.Grant(schemas=['urn:ietf:params:scim:schemas:oracle:idcs:Grant'],
        grant_mechanism='ADMINISTRATOR_TO_USER',app=oci.identity_domains.models.GrantApp(value=state['oauth_app_id']),
        grantee=oci.identity_domains.models.GrantGrantee(value=user.id,type='User'))).data
    state['oauth_user_granted']=True;save();print('Dashboard access assigned to your account',flush=True)
save()
print('Infrastructure and browser identity configuration complete.',flush=True)
for item in client(oci.apigateway.DeploymentClient).list_deployments(compartment_id=COMP,gateway_id=state['gateway_id']).data.items:
    print('existing_deployment',item.display_name,item.path_prefix,flush=True)
print('Browser URL:',state['app_url'],flush=True)
