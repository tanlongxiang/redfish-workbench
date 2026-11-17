"""Real new web routes -> shared worker -> loopback HTTPS BMC, no site access."""
from pathlib import Path
import sys,json,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));sys.path.insert(0,str(Path(__file__).parent));sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'cli/tests'))
import test_api as support
from redfish_fixture import Fixture
from redfish_session import ManagedSession

class RedfishAPITests(unittest.TestCase):
 post=support.APITests.post;wait=support.APITests.wait
 @classmethod
 def setUpClass(cls):cls.fixture=Fixture()
 @classmethod
 def tearDownClass(cls):cls.fixture.close()
 def setUp(self):
  self.fixture.reset();support.APITests.setUp(self)
  fixture_address=self.fixture.address
  def loopback(address,*args):
   if address!=fixture_address:raise AssertionError('test forbids any non-fixture destination')
   return ManagedSession(address,*args)
  self.app.state.redfish.client_factory=loopback
 def body(self,operation='3.3.7',**kwargs):
  return {'operation':operation,'targets':[{'name':'localhost-only','address':self.fixture.address}],'params':{'memory_id':'DIMMA0'} if operation=='3.3.7' else {},'insecure':True,'credentials':{'source':'manual','username':'fixture-user','password':'fixture-password'},**kwargs}
 def test_catalog_no_connection(self):
  r=self.client.get('/api/redfish/catalog');self.assertEqual(r.status_code,200);self.assertEqual(len(r.json()['operations']),69);self.assertFalse(self.fixture.calls)
 def test_manual_credentials_task_and_cleanup(self):
  job=self.wait(self.post('/api/redfish/jobs',self.body()));self.assertEqual(job['state'],'completed',job)
  self.assertEqual(job['results'][0]['session_cleanup'],'deleted');self.assertFalse(self.fixture.sessions)
  self.assertNotIn('credentials',job['draft']);self.assertNotIn('fixture-password',json.dumps(job))
 def test_configured_credentials_are_explicit(self):
  job=self.wait(self.post('/api/redfish/jobs',self.body(credentials={'source':'configured'})));self.assertEqual(job['state'],'completed')
 def test_no_credentials_no_connection(self):
  self.post('/api/redfish/jobs',self.body(credentials={'source':'manual'}),400);self.assertFalse(self.fixture.calls)
 def test_write_requires_plan(self):
  self.post('/api/redfish/jobs',self.body('3.4.3',payload={'IndicatorLED':'Blinking'},expected_model='R6250A0'),400);self.assertFalse(self.fixture.calls)
 def test_plan_offline_and_single_use(self):
  body=self.body('3.4.3',payload={'IndicatorLED':'Blinking'},expected_model='R6250A0')
  plan=self.post('/api/redfish/plans',body);self.assertFalse(self.fixture.calls);self.assertNotIn('fixture-password',json.dumps(plan))
  job=self.wait(self.post('/api/redfish/plans/'+plan['id']+'/confirm',{'phrase':plan['phrase']}));self.assertEqual(job['state'],'completed',job)
  self.post('/api/redfish/plans/'+plan['id']+'/confirm',{'phrase':plan['phrase']},400)
  self.assertEqual(len(self.fixture.writes),1);self.assertFalse(self.fixture.sessions)
 def test_cancel_erases_pending_credentials(self):
  p=self.post('/api/redfish/plans',self.body('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0'))
  r=self.client.delete('/api/redfish/plans/'+p['id'],headers=self.headers);self.assertEqual(r.status_code,200)
  self.assertNotIn(p['id'],self.app.state.redfish.plans);self.assertFalse(self.fixture.calls)
 def test_expiry_before_confirm(self):
  p=self.post('/api/redfish/plans',self.body('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0'))
  self.app.state.redfish.plans[p['id']]['expires']=time.time()-1
  self.post('/api/redfish/plans/'+p['id']+'/confirm',{'phrase':p['phrase']},400);self.assertFalse(self.fixture.calls)
 def test_other_browser_cannot_confirm(self):
  p=self.post('/api/redfish/plans',self.body('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0'))
  r=self.client.post('/api/session',json={'key':self.app.state.sessions.access_key},headers=self.origin);headers={**self.origin,'X-CSRF-Token':r.json()['csrf']}
  r=self.client.post('/api/redfish/plans/'+p['id']+'/confirm',json={'phrase':p['phrase']},headers=headers)
  self.assertEqual(r.status_code,400);self.assertFalse(self.fixture.calls)
 def test_csrf_and_login_required(self):
  r=self.client.post('/api/redfish/jobs',json=self.body(),headers=self.origin);self.assertEqual(r.status_code,403)
  self.client.cookies.clear();self.assertEqual(self.client.get('/api/redfish/catalog').status_code,401)
 def test_cleanup_failure_visible(self):
  self.fixture.delete_fail=True;job=self.wait(self.post('/api/redfish/jobs',self.body()));self.assertEqual(job['state'],'needs_review');self.assertEqual(job['results'][0]['session_cleanup'],'unconfirmed')
 def test_image_is_local_only_and_change_invalidates_plan(self):
  r=self.client.post('/api/redfish/images',files={'file':('INERT.bin',b'FIXTURE-NOT-FIRMWARE')},headers=self.headers)
  self.assertEqual(r.status_code,200,r.text);image=r.json();self.assertFalse(image['uploaded_to_bmc']);self.assertFalse(self.fixture.calls)
  p=self.post('/api/redfish/plans',self.body('3.7.6',payload={'Targets':['/redfish/v1/UpdateService/FirmwareInventory/Bios']},image_id=image['id'],expected_model='R6250A0'))
  Path(self.app.state.redfish.plans[p['id']]['request']['image']).write_bytes(b'changed')
  self.post('/api/redfish/plans/'+p['id']+'/confirm',{'phrase':p['phrase']},400);self.assertFalse(self.fixture.calls)
 def test_bios_password_private_fields_not_persisted(self):
  body=self.body('3.3.22',payload={'PasswordName':'SetupAttribute'},bios_passwords={'old':'old-bios-private','new':'new-bios-private'},expected_model='R6250A0')
  p=self.post('/api/redfish/plans',body)
  self.assertNotIn('new-bios-private',json.dumps(p));self.assertFalse(self.fixture.calls)
  job=self.wait(self.post('/api/redfish/plans/'+p['id']+'/confirm',{'phrase':p['phrase']}))
  self.assertEqual(len(self.fixture.password_changes),1);self.assertNotIn('new-bios-private',json.dumps(job))
  with self.app.state.runtime.store.connect() as db:
   for table in ['jobs','events','audit']:
    for row in db.execute('SELECT * FROM '+table):
     self.assertNotIn('new-bios-private',str(tuple(row)));self.assertNotIn('old-bios-private',str(tuple(row)))
 def test_reports_and_database_no_credentials(self):
  job=self.wait(self.post('/api/redfish/jobs',self.body()))
  for kind in ['report','results','evidence']:
   r=self.client.get('/api/jobs/'+job['id']+'/files/'+kind);self.assertEqual(r.status_code,200);self.assertNotIn(b'fixture-password',r.content)
  with self.app.state.runtime.store.connect() as db:
   for table in ['jobs','events','audit']:
    for row in db.execute('SELECT * FROM '+table):self.assertNotIn('fixture-password',str(tuple(row)))

if __name__=='__main__':unittest.main()
