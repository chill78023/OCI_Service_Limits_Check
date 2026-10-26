import importlib.util, io, json, os, sys, types, unittest, zipfile, datetime as dt
from unittest.mock import patch, Mock
from xml.etree import ElementTree

class Response:
    def __init__(self, ctx, response_data='', status_code=200, headers=None):
        self.data, self.status, self.headers = response_data, status_code, headers
sys.modules['fdk'] = types.SimpleNamespace(response=types.SimpleNamespace(Response=Response))
spec=importlib.util.spec_from_file_location('limits_func','function/func.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class DashboardTests(unittest.TestCase):
    def test_threshold_and_missing_usage(self):
        for limit,used,ratio,alert in [(100,79,.79,False),(100,80,.8,True),(100,100,1,True),(100,120,1.2,True),(0,0,None,False),(0,1,None,True),(100,None,None,False)]:
            self.assertEqual(app.utilization(limit,used),(ratio,alert))
    def test_large_limit_precision(self):
        self.assertEqual(app.safe_limit(9223372036854775807),'9223372036854775807')
    def test_public_web_cannot_read_snapshot(self):
        with patch.dict(os.environ,APP_ROLE='web'):
            result=app.handler(None,io.BytesIO(b'{"action":"snapshot"}'))
        self.assertEqual(result.status,403)
    def test_invalid_role_fails_closed(self):
        with patch.dict(os.environ,APP_ROLE='unknown'):
            result=app.handler(None,io.BytesIO(b'{}'))
        self.assertEqual(result.status,503)
    def test_pkce_validation(self):
        with self.assertRaises(ValueError):app.exchange_code({'code':'code','verifier':'short'})
    def test_public_config_excludes_secrets(self):
        bait='TEST_ONLY_DO_NOT_EXPOSE'
        with patch.dict(os.environ,APP_ROLE='web',OCI_API_KEY=bait,PRIVATE_KEY=bait,CLIENT_SECRET=bait):
            config=app.public_config()
            result=app.handler(None,io.BytesIO(b'{}'))
        self.assertEqual(set(config),{'clientId','domain','appUrl','apiUrl','scope'})
        self.assertEqual(result.status,200)
        self.assertNotIn(bait,result.data)
    def test_runtime_never_falls_back_to_workstation_key(self):
        with patch.object(app.oci.auth.signers,'get_resource_principals_signer',side_effect=RuntimeError('No cloud identity')),patch.object(app.oci.config,'from_file') as load_local_config:
            with self.assertRaises(RuntimeError):app.cloud_client(app.oci.limits.LimitsClient)
            load_local_config.assert_not_called()
    def test_failed_refresh_does_not_overwrite_snapshot(self):
        store=Mock();store.put_object.return_value.headers={'etag':'claimed'}
        with patch.dict(os.environ,STORAGE_NAMESPACE='namespace',SNAPSHOT_BUCKET='bucket'),patch.object(app,'object_store',return_value=store),patch.object(app,'refresh_state',return_value=({'status':'Idle'},'previous')),patch.object(app,'collect_snapshot',side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):app.refresh()
        self.assertEqual(store.put_object.call_count,2)
        self.assertTrue(all(c.args[2]=='refresh-status.json' for c in store.put_object.call_args_list))
        self.assertEqual(json.loads(store.put_object.call_args.args[3])['status'],'Failed')
    def test_refresh_invokes_only_configured_collector_detached(self):
        invoke=Mock()
        with patch.dict(os.environ,COLLECTOR_FUNCTION_ID='configured-collector',COLLECTOR_INVOKE_ENDPOINT='https://functions.example'),patch.object(app,'refresh_state',return_value=({'status':'Idle'},None)),patch.object(app.oci.auth.signers,'get_resource_principals_signer',return_value=Mock()),patch.object(app.oci.functions,'FunctionsInvokeClient',return_value=invoke):
            result=app.start_refresh()
        self.assertEqual(result['status'],'Accepted')
        invoke.invoke_function.assert_called_once_with('configured-collector',invoke_function_body=b'{}',fn_invoke_type='detached')
    def test_running_refresh_does_not_start_another_pull(self):
        state={'status':'Running','started_at':app.now()}
        with patch.object(app,'refresh_state',return_value=(state,'etag')),patch.object(app,'collect_snapshot') as collect,patch.object(app,'object_store',return_value=Mock()),patch.object(app.oci.functions,'FunctionsInvokeClient') as invoke:
            self.assertEqual(app.start_refresh()['status'],'Running')
            self.assertEqual(app.refresh(),state)
        collect.assert_not_called();invoke.assert_not_called()
    def test_concurrent_claim_loser_does_not_collect(self):
        store=Mock();store.put_object.side_effect=app.oci.exceptions.ServiceError(412,'PreconditionFailed',{},'test')
        with patch.dict(os.environ,STORAGE_NAMESPACE='namespace',SNAPSHOT_BUCKET='bucket'),patch.object(app,'object_store',return_value=store),patch.object(app,'refresh_state',side_effect=[({'status':'Idle'},'old'),({'status':'Running'},'new')]),patch.object(app,'collect_snapshot') as collect:
            self.assertEqual(app.refresh()['status'],'Running')
        collect.assert_not_called()
    def test_expired_refresh_lease_can_be_recovered(self):
        old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=21)).isoformat()
        self.assertFalse(app.refresh_running({'status':'Running','started_at':old}))
        self.assertTrue(app.refresh_running({'status':'Running','started_at':app.now()}))
    def test_success_publishes_snapshot_before_completion(self):
        store=Mock();store.put_object.return_value.headers={'etag':'claim'}
        snapshot={'last_pull_at':app.now(),'record_count':1,'rows':[]}
        with patch.dict(os.environ,STORAGE_NAMESPACE='namespace',SNAPSHOT_BUCKET='bucket'),patch.object(app,'object_store',return_value=store),patch.object(app,'refresh_state',return_value=({'status':'Idle'},None)),patch.object(app,'collect_snapshot',return_value=snapshot):
            self.assertEqual(app.refresh()['status'],'Succeeded')
        self.assertEqual([c.args[2] for c in store.put_object.call_args_list],['refresh-status.json','snapshot.json','refresh-status.json'])
        self.assertEqual(store.put_object.call_args_list[0].kwargs['if_none_match'],'*')
        self.assertEqual(store.put_object.call_args.kwargs['if_match'],'claim')
    def test_public_web_cannot_start_refresh(self):
        with patch.dict(os.environ,APP_ROLE='web'),patch.object(app,'start_refresh') as start:
            result=app.handler(None,io.BytesIO(b'{"action":"refresh"}'))
        self.assertEqual(result.status,403);start.assert_not_called()
    def test_export_preserves_text_and_precision(self):
        rows=[{'region':'us-ashburn-1','service':'compute','name':'=not-a-formula','limit':'9223372036854775807','current_use':0,'scope':'REGION','description':'A & B'}]
        with zipfile.ZipFile(io.BytesIO(app.make_xlsx(rows))) as archive:
            for name in archive.namelist():ElementTree.fromstring(archive.read(name))
            xml=archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('9223372036854775807',xml)
        self.assertIn('=not-a-formula',xml)
        self.assertIn('F2&gt;=0.8',xml)
        self.assertEqual(xml.count('<f>'),1)
    def test_export_over_limit_cached_value(self):
        rows=[{'limit':0,'current_use':1}]
        with zipfile.ZipFile(io.BytesIO(app.make_xlsx(rows))) as archive:
            xml=archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('Over limit',xml)

class NotificationTests(unittest.TestCase):
    def row(self,used=80,**values):
        return {'service':'compute','name':'standard-e5-core-count','region':'us-ashburn-1',
                'scope':'REGION','availability_domain':'','limit':100,'current_use':used,**values}
    def run_alerts(self,rows,active=None,confirmed=True,fail=False):
        store=Mock();store.get_object.return_value.data.content=json.dumps({'active':active or {}}).encode()
        ons=Mock();ons.publish_message.return_value.data.message_id='message-id'
        if fail:ons.publish_message.side_effect=RuntimeError('publish unavailable')
        with patch.dict(os.environ,NOTIFICATION_TOPIC_ID='topic',NOTIFICATION_ENDPOINT='https://notifications.example',NOTIFICATION_COMPARTMENT_ID='compartment',STORAGE_NAMESPACE='namespace',SNAPSHOT_BUCKET='bucket',APP_URL='https://dashboard.example'),patch.object(app.oci.auth.signers,'get_resource_principals_signer',return_value=Mock()),patch.object(app.oci.ons,'NotificationDataPlaneClient',return_value=ons),patch.object(app,'pages',return_value=[types.SimpleNamespace(lifecycle_state='ACTIVE' if confirmed else 'PENDING')]):
            result=app.notify_limits({'rows':rows,'last_pull_at':app.now()},store)
        return result,store,ons
    def test_80_percent_alert_names_exact_service_limit(self):
        result,store,ons=self.run_alerts([self.row(80)])
        self.assertEqual(result['published'],1)
        message=ons.publish_message.call_args.args[1]
        self.assertEqual(message.title,'80% of "Services Limit" reached: compute / standard-e5-core-count')
        for text in ['Current use: 80','Service limit maximum: 100','Utilization: 80.0%','us-ashburn-1','https://dashboard.example']:
            self.assertIn(text,message.body)
    def test_below_threshold_and_unknown_usage_do_not_notify(self):
        result,store,ons=self.run_alerts([self.row(79),self.row(None,name='unknown')])
        self.assertEqual(result['published'],0);ons.publish_message.assert_not_called()
    def test_persistent_breach_does_not_repeat(self):
        row=self.row(91);key=app.alert_key(row)
        result,store,ons=self.run_alerts([row],{key:{'notified_at':'earlier'}})
        self.assertEqual(result['published'],0);ons.publish_message.assert_not_called()
    def test_recovery_rearms_next_breach(self):
        row=self.row(50);key=app.alert_key(row)
        result,store,ons=self.run_alerts([row],{key:{'notified_at':'earlier'}})
        active=json.loads(store.put_object.call_args.args[3])['active'];self.assertNotIn(key,active)
        result,_,ons=self.run_alerts([self.row(80)],active)
        self.assertEqual(result['published'],1)
    def test_pending_confirmation_does_not_consume_alert(self):
        result,store,ons=self.run_alerts([self.row()],confirmed=False)
        self.assertEqual(result['status'],'AwaitingConfirmation')
        ons.publish_message.assert_not_called();store.put_object.assert_not_called()
    def test_global_replicas_notify_once_at_highest_utilization(self):
        rows=[self.row(80,scope='GLOBAL'),self.row(90,scope='GLOBAL',region='eu-frankfurt-1')]
        result,_,ons=self.run_alerts(rows)
        self.assertEqual(result['published'],1);self.assertIn('90.0%',ons.publish_message.call_args.args[1].body)
    def test_multiple_breaches_name_each_limit(self):
        result,_,ons=self.run_alerts([self.row(),self.row(85,service='vcn',name='vcn-count')])
        self.assertEqual(result['published'],2)
        body=ons.publish_message.call_args.args[1].body
        self.assertIn('standard-e5-core-count',body);self.assertIn('vcn-count',body)
    def test_delivery_failure_keeps_new_snapshot_successful(self):
        store=Mock();store.put_object.return_value.headers={'etag':'claim'}
        snapshot={'last_pull_at':app.now(),'record_count':1,'rows':[]}
        with patch.dict(os.environ,STORAGE_NAMESPACE='namespace',SNAPSHOT_BUCKET='bucket'),patch.object(app,'object_store',return_value=store),patch.object(app,'refresh_state',return_value=({'status':'Idle'},None)),patch.object(app,'collect_snapshot',return_value=snapshot),patch.object(app,'notify_limits',side_effect=RuntimeError('delivery unavailable')):
            result=app.refresh()
        self.assertEqual(result['status'],'Succeeded');self.assertEqual(result['notifications']['status'],'Failed')
        self.assertIn('snapshot.json',[c.args[2] for c in store.put_object.call_args_list])

if __name__=='__main__':unittest.main()
