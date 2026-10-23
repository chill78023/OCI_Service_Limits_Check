import sys,json,pathlib,oci
from oci_local_access import client
s=json.load(open('deployment.json'));gc=client(oci.apigateway.GatewayClient);net=client(oci.core.VirtualNetworkClient)
dc=client(oci.apigateway.DeploymentClient)
deployments=dc.list_deployments(compartment_id=s['compartment_id'],gateway_id=s['gateway_id']).data.items
if {d.id for d in deployments}!={s['web_deployment_id'],s['api_deployment_id']}:
 raise RuntimeError('Gateway has other deployments; refusing to change its network access')
gateway_response=gc.get_gateway(s['gateway_id']);gateway=gateway_response.data
subnet=net.get_subnet(gateway.subnet_id).data
groups=net.list_network_security_groups(compartment_id=s['compartment_id'],vcn_id=subnet.vcn_id).data
group=next((g for g in groups if g.display_name=='oci-limits-dashboard-https'),None)
if group is None:group=net.create_network_security_group(oci.core.models.CreateNetworkSecurityGroupDetails(compartment_id=s['compartment_id'],vcn_id=subnet.vcn_id,display_name='oci-limits-dashboard-https')).data
rules=net.list_network_security_group_security_rules(group.id).data
if not any(r.direction=='INGRESS' and r.protocol=='6' and r.source=='0.0.0.0/0' and r.tcp_options and r.tcp_options.destination_port_range.min==443 and r.tcp_options.destination_port_range.max==443 for r in rules):
 net.add_network_security_group_security_rules(group.id,oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(security_rules=[oci.core.models.AddSecurityRuleDetails(direction='INGRESS',protocol='6',source='0.0.0.0/0',source_type='CIDR_BLOCK',is_stateless=False,description='HTTPS access to OCI limits browser dashboard; data routes require JWT',tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=443,max=443)))]))
if group.id not in gateway.network_security_group_ids:
 gc.update_gateway(gateway.id,oci.apigateway.models.UpdateGatewayDetails(network_security_group_ids=gateway.network_security_group_ids+[group.id]),if_match=gateway_response.headers['etag'])
pathlib.Path('gateway-network-state.json').write_text(json.dumps({'network_security_group_id':group.id},indent=2))
print('Dedicated HTTPS rule attached to the dashboard gateway',flush=True)
