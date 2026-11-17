"""Actual new transport against localhost HTTPS, plus offline scope/permit tests."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];CORE=ROOT/'_engine' if (ROOT/'_engine').is_dir() else ROOT.parent/'core'
sys.path.insert(0,str(CORE));sys.path.insert(0,str(Path(__file__).parent))
import json,os,shutil,subprocess,tempfile,threading,time,unittest
from redfish_fixture import Fixture
from redfish_session import ManagedSession
from redfish_ops import catalog,contract
from redfish_workflow import prepare,run_tasks,Approval
from firmware_batch import Target
from bmc_client import BmcError

@unittest.skipUnless(shutil.which('openssl'),'openssl unavailable')
class RedfishTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.fixture=Fixture()
 @classmethod
 def tearDownClass(cls):cls.fixture.close()
 def setUp(self):
  self.fixture.reset();self.targets=[Target('loopback',self.fixture.address)]
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
 def request(self,operation='3.3.7',**kwargs):return {'operation':operation,'params':{'memory_id':'DIMMA0'} if operation=='3.3.7' else {},'payload':{},'insecure':True,**kwargs}
 def run_(self,r,write=False):
  _,r,_,scope=prepare(self.targets,r)
  approval=Approval(scope,time.time()+120) if write else None
  return run_tasks(self.targets,r,('fixture-user','fixture-password'),self.folder,approval=approval)
 def test_69_catalog(self):
  c=catalog();self.assertEqual(len(c),69);self.assertEqual([sum(o['module']==m for o in c) for m in ['Systems','Chassis','UpdateService']],[40,22,7])
 def test_read_session_created_and_deleted(self):
  rows,folder=self.run_(self.request());r=rows[0]
  self.assertEqual(r['data']['CapacityMiB'],32768);self.assertEqual(r['session_cleanup'],'deleted');self.assertFalse(self.fixture.sessions)
  self.assertEqual(r['session_timeout_seconds'],600)
  self.assertEqual(sum(c[0]=='POST' for c in self.fixture.calls),1)
  self.assertTrue((folder/'维护结果.xlsx').is_file())
 def test_chassis_discovery(self):
  rows,_=self.run_(self.request('3.4.6'));self.assertEqual(rows[0]['resource'],'/redfish/v1/Chassis/rack42/Thermal')
 def test_read_update_service(self):
  rows,_=self.run_(self.request('3.7.1'));self.assertEqual(rows[0]['state'],'completed')
 def test_no_location_fails_closed(self):
  self.fixture.no_location=True;rows,_=self.run_(self.request());self.assertEqual(rows[0]['session_cleanup'],'unconfirmed')
  self.assertEqual(len(self.fixture.calls),1)
 def test_cross_origin_location_rejected(self):
  self.fixture.bad_location='https://192.0.2.1/redfish/v1/SessionService/Sessions/stolen';rows,_=self.run_(self.request());self.assertEqual(len(self.fixture.calls),1);self.assertEqual(rows[0]['state'],'needs_review')
 def test_no_token_fails_closed(self):
  self.fixture.no_token=True;rows,_=self.run_(self.request());self.assertNotEqual(rows[0]['state'],'completed');self.assertEqual(len(self.fixture.calls),1)
 def test_business_failure_still_logout(self):
  self.fixture.fail_path='/redfish/v1/Systems/host42/Memory/DIMMA0';rows,_=self.run_(self.request());self.assertEqual(rows[0]['state'],'failed');self.assertFalse(self.fixture.sessions)
 def test_delete_failure_not_success(self):
  self.fixture.delete_fail=True;rows,_=self.run_(self.request());self.assertEqual(rows[0]['session_cleanup'],'unconfirmed');self.assertEqual(rows[0]['state'],'needs_review')
 def test_delete_already_absent(self):
  self.fixture.delete_absent=True;rows,_=self.run_(self.request());self.assertEqual(rows[0]['session_cleanup'],'already_absent')
 def test_secrets_redacted(self):
  self.fixture.echo=True;rows,folder=self.run_(self.request('3.4.6'))
  self.assertNotIn('fixture-password',(folder/'results.json').read_text());self.assertIn('[REDACTED]',str(rows))
 def test_invalid_id_offline(self):
  for value in ['../Managers','a/b','x?token=bad','https://192.0.2.1/a']:
   with self.assertRaises(BmcError):prepare(self.targets,self.request(params={'memory_id':value}))
  self.assertFalse(self.fixture.calls)
 def test_requires_approval_before_login(self):
  with self.assertRaises(BmcError):self.run_(self.request('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0'))
  self.assertFalse(self.fixture.calls)
 def test_expired_approval_before_login(self):
  r=self.request('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0');_,r,_,scope=prepare(self.targets,r)
  with self.assertRaises(BmcError):run_tasks(self.targets,r,('fixture-user','fixture-password'),self.folder,approval=Approval(scope,time.time()-1))
  self.assertFalse(self.fixture.calls)
 def test_approval_scope_and_single_use(self):
  r=self.request('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0');_,r,_,scope=prepare(self.targets,r);a=Approval(scope,time.time()+120)
  rows,_=run_tasks(self.targets,r,('fixture-user','fixture-password'),self.folder,approval=a);self.assertEqual(rows[0]['state'],'completed')
  with self.assertRaises(BmcError):run_tasks(self.targets,r,('fixture-user','fixture-password'),self.folder,approval=a)
 def test_model_mismatch_no_write(self):
  self.fixture.model='OTHER';rows,_=self.run_(self.request('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0'),True);self.assertFalse(self.fixture.writes);self.assertFalse(self.fixture.sessions)
 def test_patch_uses_etag_and_reads_back(self):
  rows,_=self.run_(self.request('3.4.3',payload={'IndicatorLED':'Blinking'},expected_model='R6250A0'),True)
  self.assertEqual(rows[0]['state'],'completed');self.assertEqual(rows[0]['readback']['IndicatorLED'],'Blinking')
 def test_missing_etag_no_patch(self):
  self.fixture.no_etag=True;rows,_=self.run_(self.request('3.4.3',payload={'IndicatorLED':'Blinking'},expected_model='R6250A0'),True);self.assertFalse(self.fixture.writes)
 def firmware(self):
  p=self.folder/'INERT.bin';p.write_bytes(b'FIXTURE-NOT-FIRMWARE')
  return self.request('3.7.6',payload={'Targets':['/redfish/v1/UpdateService/FirmwareInventory/Bios'],'SaveConfig':True},expected_model='R6250A0',image=str(p),expected_version='2.0')
 def test_upgrade_fixture_end_to_end(self):
  rows,_=self.run_(self.firmware(),True);self.assertEqual(rows[0]['state'],'completed',rows);self.assertEqual(rows[0]['actual_version'],'2.0');self.assertFalse(self.fixture.sessions);self.assertEqual(len(self.fixture.writes),2)
 def test_on_blocks_upload(self):
  self.fixture.power='On';rows,_=self.run_(self.firmware(),True);self.assertFalse(self.fixture.writes)
 def test_stale_100_not_success(self):
  self.fixture.stale=True;rows,_=self.run_(self.firmware(),True);self.assertEqual(rows[0]['state'],'needs_review');self.assertIn('旧任务',rows[0]['error'])
 def test_image_changed_invalidates_scope(self):
  r=self.firmware();_,r,_,scope=prepare(self.targets,r);Path(r['image']).write_bytes(b'changed')
  with self.assertRaises(BmcError):run_tasks(self.targets,r,('fixture-user','fixture-password'),self.folder,approval=Approval(scope,time.time()+120))
  self.assertFalse(self.fixture.calls)
 def test_no_retry_failed_write(self):
  self.fixture.write_fail=True;rows,_=self.run_(self.request('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0'),True);self.assertEqual(len(self.fixture.writes),1);self.assertEqual(rows[0]['state'],'needs_review');self.assertFalse(self.fixture.sessions)
 def test_cancel_before_login(self):
  stop=threading.Event();stop.set();rows,_=run_tasks(self.targets,self.request(),('fixture-user','fixture-password'),self.folder,stop=stop);self.assertEqual(rows[0]['state'],'stopped');self.assertFalse(self.fixture.calls)
 def test_cli_read_and_default_offline_plan(self):
  app=os.environ.get('cli_APP',str(ROOT/'bmc_tool.py'))
  common=[sys.executable,'-I','-S','-B',app,'redfish','run','--operation','3.3.1','--target','loopback='+self.fixture.address,'--output',str(self.folder),'--state-dir',str(self.folder/'state')]
  env={**os.environ,'BMC_USERNAME':'fixture-user','BMC_PASSWORD':'fixture-password'}
  offline=subprocess.run(common,capture_output=True,text=True,env=env);self.assertEqual(offline.returncode,0,offline.stderr);self.assertFalse(self.fixture.calls)
  live=subprocess.run(common+['--live','--insecure'],capture_output=True,text=True,env=env)
  self.assertEqual(live.returncode,0,live.stderr);self.assertFalse(self.fixture.sessions);self.assertNotIn('fixture-password',live.stdout+live.stderr)
 def test_malformed_login_still_deletes_created_session(self):
  self.fixture.malformed_login=True;rows,_=self.run_(self.request())
  self.assertEqual(rows[0]['session_cleanup'],'deleted');self.assertFalse(self.fixture.sessions)
  self.assertEqual(len(self.fixture.calls),2)
 def test_pagination_and_expand_fallback(self):
  self.fixture.paged=True;rows,_=self.run_(self.request('3.3.8'))
  self.assertEqual(rows[0]['state'],'completed',rows);self.assertEqual(len(rows[0]['data']['Members']),2)
  self.assertTrue(all(d['CapacityMiB']==32768 for d in rows[0]['data']['Members']))
 def test_multi_target_sessions_independent(self):
  second=Fixture();self.addCleanup(second.close)
  self.targets.append(Target('second-loopback',second.address))
  rows,_=self.run_(self.request())
  self.assertEqual(len(rows),2);self.assertTrue(all(r['session_cleanup']=='deleted' for r in rows))
  self.assertFalse(self.fixture.sessions);self.assertFalse(second.sessions)
 def test_batch_write_requires_canary(self):
  self.targets.append(Target('other-loopback','https://127.0.0.1:1'))
  with self.assertRaises(BmcError):self.run_(self.request('3.3.23',payload={'ResetType':'On'},expected_model='R6250A0'),True)
  self.assertFalse(self.fixture.calls)
 def test_nested_oem_tunnel_rejected(self):
  with self.assertRaises(BmcError):prepare(self.targets,self.request('3.4.3',payload={'Oem':{'BMC':{'UnspecifiedAction':'x'}}},expected_model='R6250A0'))
 def test_list_payload_not_object(self):
  with self.assertRaises(BmcError):prepare(self.targets,self.request('3.3.1',payload=[]))
 def test_bios_password_dedicated_secret_scope(self):
  r=self.request('3.3.22',payload={'PasswordName':'SetupAttribute'},bios_passwords={'old':'old-bios-private','new':'new-bios-private'},expected_model='R6250A0')
  _,r,public,scope=prepare(self.targets,r)
  self.assertNotIn('new-bios-private',json.dumps(public));self.assertNotIn('old-bios-private',json.dumps(public))
  rows,folder=self.run_(r,True)
  self.assertEqual(len(self.fixture.password_changes),1);self.assertFalse(self.fixture.sessions)
  for name in ['results.json','plan.json','events.jsonl']:
   text=(folder/name).read_text();self.assertNotIn('new-bios-private',text);self.assertNotIn('old-bios-private',text)
  changed={**r,'bios_passwords':{'old':'old-bios-private','new':'changed-private'}}
  with self.assertRaises(BmcError):run_tasks(self.targets,changed,('fixture-user','fixture-password'),self.folder,approval=Approval(scope,time.time()+120))
 def test_bios_password_rejects_generic_json(self):
  with self.assertRaises(BmcError):prepare(self.targets,self.request('3.3.22',payload={'PasswordName':'SetupAttribute','NewPassword':'do-not-accept'},expected_model='R6250A0'))
 def test_cli_write_pipe_rejected(self):
  app=os.environ.get('cli_APP',str(ROOT/'bmc_tool.py'));body=self.folder/'body.json';body.write_text('{"ResetType":"On"}')
  p=subprocess.run([sys.executable,'-I','-S','-B',app,'redfish','run','--operation','3.3.23','--body',str(body),'--model','R6250A0','--target','loopback='+self.fixture.address,'--live','--execute'],input='yes\n',capture_output=True,text=True)
  self.assertEqual(p.returncode,2);self.assertFalse(self.fixture.calls)

if __name__=='__main__':unittest.main()
