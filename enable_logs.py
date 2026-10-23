import sys,json,pathlib,oci
from oci_local_access import client
s=json.load(open('deployment.json'));lm=client(oci.logging.LoggingManagementClient);M=oci.logging.models
groups=lm.list_log_groups(compartment_id=s['compartment_id']).data
group=next((g for g in groups if g.display_name=='oci-limits-dashboard'),None)
if group is None:group=lm.create_log_group(M.CreateLogGroupDetails(compartment_id=s['compartment_id'],display_name='oci-limits-dashboard',description='Runtime diagnostics for the utilization dashboard')).data
pathlib.Path('logging-state.json').write_text(json.dumps({'log_group_id':group.id},indent=2))
logs=lm.list_logs(group.id).data
if not any(x.display_name=='oci-limits-invocations' for x in logs):
 result=lm.create_log(group.id,M.CreateLogDetails(display_name='oci-limits-invocations',log_type='SERVICE',is_enabled=True,retention_duration=30,
  configuration=M.Configuration(compartment_id=s['compartment_id'],source=M.OciService(source_type='OCISERVICE',service='functions',resource=s['application_id'],category='invoke'))))
 print('Log creation requested',result.status,flush=True)
print('Log group ready',group.id,flush=True)
