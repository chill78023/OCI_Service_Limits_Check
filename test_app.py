import importlib.util, io, json, os, sys, types, unittest, zipfile
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
        store=Mock()
        with patch.dict(os.environ,STORAGE_NAMESPACE='namespace',SNAPSHOT_BUCKET='bucket'),patch.object(app,'object_store',return_value=store),patch.object(app,'collect_snapshot',side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):app.refresh()
        self.assertEqual(store.put_object.call_count,1)
        self.assertEqual(store.put_object.call_args.args[2],'refresh-status.json')
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

if __name__=='__main__':unittest.main()
