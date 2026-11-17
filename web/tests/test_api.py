"""Local API/security/state tests. Live BMC transport is forbidden by a test guard."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import copy
import json
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from bmc_web.app import create_app
from bmc_web.service import Settings
from bmc_web.bridge import DemoClient,UpgradeClient,COMPONENTS
from bmc_web.store import Store

ORIGIN='http://127.0.0.1:38080'

class APITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.block=patch.object(UpgradeClient,'request',side_effect=AssertionError('real BMC forbidden'))
        self.network=self.block.start();self.addCleanup(self.block.stop)
        self.app=create_app(Settings(self.root,test_simulation=True,credential_provider=lambda:('fixture-user','fixture-password')))
        self.client=TestClient(self.app,base_url=ORIGIN);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.origin={'Origin':ORIGIN}
        login=self.client.post('/api/session',json={'key':self.app.state.sessions.access_key},headers=self.origin)
        self.assertEqual(login.status_code,200)
        self.headers={**self.origin,'X-CSRF-Token':login.json()['csrf']}
        self.owner=self.app.state.sessions.get(self.client.cookies.get('bmc_session'))['id']
    def post(self,path,body,status=200):
        response=self.client.post(path,json=body,headers=self.headers)
        self.assertEqual(response.status_code,status,(path,response.status_code,response.text))
        return response.json()
    def demo(self,groups=('Dcu02Cpld',)):
        d=self.post('/api/demo',{})
        return {'revision':uuid.uuid4().hex,'mode':'demo','targets':d['targets'],'images':[{'component':c,'image_id':d['image']['id'],'expected':'0.5'} for c in groups],
                'auth':'basic','self_signed':False,'workers':2,'canary_confirmed':False}
    def wait(self,job):
        for _ in range(400):
            value=self.client.get('/api/jobs/'+job['id']).json()
            if value['state'] not in ('queued','running'):
                for _ in range(100):
                    if not self.app.state.runtime.active:break
                    time.sleep(.005)
                return value
            time.sleep(.01)
        self.fail('task timed out')
    def run_(self,action,draft):return self.wait(self.post('/api/jobs',{'action':action,'draft':draft}))
    def off(self,draft):
        for target in draft['targets']:DemoClient.states[target['address']]['power']='Off'
    def checked(self,draft):
        self.off(draft);result=self.run_('precheck',draft);self.assertEqual(result['state'],'completed',result.get('error'))
        self.assertTrue(self.post('/api/readiness',draft)['ready'])
    def execute(self,action,draft):
        d=copy.deepcopy(draft)
        if action!='upgrade':d['images']=[]
        plan=self.post('/api/plans',{'action':action,'draft':d})
        return self.wait(self.post('/api/plans/'+plan['id']+'/confirm',{'phrase':plan['phrase']}))
    def test_auth_required_for_all_records(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/jobs').status_code,401)
        self.assertEqual(self.client.get('/api/images').status_code,401)
        self.assertEqual(self.client.get('/api/assets').status_code,401)
    def test_origin_and_csrf_enforced(self):
        for headers in ({},{'Origin':'https://evil.example','X-CSRF-Token':self.headers['X-CSRF-Token']},self.origin):
            self.assertEqual(self.client.post('/api/demo',json={},headers=headers).status_code,403)
    def test_host_and_security_headers(self):
        self.assertEqual(self.client.get('/api/config',headers={'Host':'evil.example'}).status_code,400)
        response=self.client.get('/api/config')
        self.assertEqual(response.headers['x-frame-options'],'DENY')
        self.assertIn("script-src 'self'",response.headers['content-security-policy'])
        self.assertIn('no-store',response.headers['cache-control'])
    def test_cookies_httponly_strict(self):
        response=self.client.post('/api/session',json={'key':self.app.state.sessions.access_key},headers=self.origin)
        cookie=response.headers['set-cookie'].lower()
        self.assertIn('httponly',cookie);self.assertIn('samesite=strict',cookie)
    def test_validation_never_echoes_secret(self):
        secret='not-a-real-password-'+('x'*300)
        response=self.client.post('/api/session',json={'key':secret},headers=self.origin)
        self.assertEqual(response.status_code,422);self.assertNotIn(secret,response.text)
    def test_unicode_login_error_is_handled(self):
        response=self.client.post('/api/session',json={'key':'错误口令'},headers=self.origin)
        self.assertEqual(response.status_code,400)
    def test_one_use_bootstrap(self):
        body={'key':self.app.state.sessions.bootstrap,'bootstrap':True}
        first=self.client.post('/api/session',json=body,headers=self.origin)
        second=self.client.post('/api/session',json=body,headers=self.origin)
        self.assertEqual(first.status_code,200);self.assertEqual(second.status_code,400)
    def test_config_no_password(self):
        response=self.client.get('/api/config')
        self.assertNotIn('fixture-password',response.text)
        self.assertTrue(response.json()['confirmation_required']);self.assertNotIn('live_writes_enabled',response.json())
    def test_read_does_not_require_images_or_off(self):
        draft=self.demo();draft['images']=[];draft['model']=''
        job=self.run_('read',draft)
        self.assertEqual(job['state'],'completed')
        self.assertEqual(job['results'][0]['snapshot']['power'],'On')
        self.assertEqual(self.network.call_count,0)
    def test_on_blocks_upgrade_before_confirm(self):
        draft=self.demo();job=self.run_('precheck',draft)
        self.assertEqual(job['state'],'failed')
        ready=self.post('/api/readiness',draft);self.assertFalse(ready['ready']);self.assertIn('On',ready['reason'])
        self.post('/api/plans',{'action':'upgrade','draft':draft},400)
    def test_off_read_not_enough(self):
        draft=self.demo();self.off(draft)
        self.run_('read',{**draft,'images':[]})
        ready=self.post('/api/readiness',draft)
        self.assertFalse(ready['ready']);self.assertIn('尚未',ready['reason'])
    def test_new_read_invalidates_previous_off_proof(self):
        draft=self.demo();self.checked(draft)
        DemoClient.states[draft['targets'][0]['address']]['power']='On'
        self.run_('read',{**draft,'images':[]})
        ready=self.post('/api/readiness',draft)
        self.assertFalse(ready['ready']);self.assertIn('On',ready['reason'])
        self.post('/api/plans',{'action':'upgrade','draft':draft},400)
        self.off(draft);self.run_('read',{**draft,'images':[]})
        ready=self.post('/api/readiness',draft)
        self.assertFalse(ready['ready']);self.assertIn('尚未',ready['reason'])
    def test_revision_scope_change_invalidates(self):
        draft=self.demo();self.checked(draft)
        changed={**draft,'revision':'changed'}
        self.assertFalse(self.post('/api/readiness',changed)['ready'])
        changed={**draft,'targets':draft['targets'][:1]}
        self.assertFalse(self.post('/api/readiness',changed)['ready'])
    def test_precheck_expires(self):
        draft=self.demo();self.checked(draft)
        runtime=self.app.state.runtime
        for proof in runtime.prechecks.values():
            for address,(snap,_) in proof.observations.items():proof.observations[address]=(snap,time.monotonic()-121)
        ready=self.post('/api/readiness',draft)
        self.assertFalse(ready['ready']);self.assertIn('过期',ready['reason'])
    def test_wrong_confirmation_and_double_confirm(self):
        draft=self.demo();self.checked(draft)
        p=self.post('/api/plans',{'action':'upgrade','draft':draft})
        self.post('/api/plans/'+p['id']+'/confirm',{'phrase':'错误确认'},400)
        job=self.post('/api/plans/'+p['id']+'/confirm',{'phrase':p['phrase']})
        self.post('/api/plans/'+p['id']+'/confirm',{'phrase':p['phrase']},400)
        self.assertEqual(self.wait(job)['state'],'completed')
    def test_plan_expiry_and_other_session(self):
        draft=self.demo();self.checked(draft);p=self.post('/api/plans',{'action':'upgrade','draft':draft})
        with self.app.state.runtime.store.connect() as db:db.execute('UPDATE plans SET expires=? WHERE id=?',(time.time()-1,p['id']))
        self.post('/api/plans/'+p['id']+'/confirm',{'phrase':p['phrase']},400)
        p=self.post('/api/plans',{'action':'upgrade','draft':draft})
        login=self.client.post('/api/session',json={'key':self.app.state.sessions.access_key},headers=self.origin)
        self.headers={**self.origin,'X-CSRF-Token':login.json()['csrf']}
        self.post('/api/plans/'+p['id']+'/confirm',{'phrase':p['phrase']},400)
    def test_subset_upgrade_verify_and_reports(self):
        draft=self.demo(('Dcu02Cpld','Dcu46Cpld'));self.checked(draft)
        job=self.execute('upgrade',draft);self.assertEqual(job['state'],'completed',job.get('error'))
        for result in job['results']:self.assertEqual([i['component'] for i in result['components']],['Dcu02Cpld','Dcu46Cpld'])
        for target in draft['targets']:
            self.assertEqual(DemoClient.states[target['address']]['versions'],{c:'0.5' if c in ('Dcu02Cpld','Dcu46Cpld') else '0.3' for c in COMPONENTS})
        verify=self.run_('verify',draft);self.assertEqual(verify['state'],'completed')
        self.assertIn('无需升级',self.post('/api/readiness',draft)['reason'])
        for kind in ('report','evidence','results'):
            response=self.client.get('/api/jobs/'+job['id']+'/files/'+kind)
            self.assertEqual(response.status_code,200);self.assertIn('attachment',response.headers['content-disposition'])
        raw=self.client.get('/api/jobs/'+job['id']+'/files/evidence').text
        self.assertNotIn('fixture-password',raw);self.assertNotIn('demo-password',raw)
    def test_power_independent_plan_no_auto_upgrade(self):
        draft=self.demo();job=self.execute('GracefulShutdown',draft)
        self.assertEqual(job['state'],'completed')
        self.assertFalse(self.post('/api/readiness',draft)['ready'])
        for target in draft['targets']:self.assertEqual(DemoClient.states[target['address']]['versions']['Dcu02Cpld'],'0.3')
        cached=self.client.get('/api/assets?mode=demo').json()
        self.assertTrue(all(a['snapshot']['power']=='Off' for a in cached))
    def test_test_fixture_forbids_live_write_plan(self):
        draft=self.demo();draft['mode']='live';draft['targets']=[{'name':'fixture-node','address':'20.1.1.1'}];draft['images']=[]
        response=self.post('/api/plans',{'action':'ForceOff','draft':draft},400)
        self.assertIn('测试服务',response['detail']);self.assertEqual(self.network.call_count,0)
    def test_fixture_service_forbids_even_live_reads(self):
        self.app.state.settings.test_simulation=True
        draft=self.demo();draft['mode']='live';draft['images']=[]
        draft['targets']=[{'name':'node','address':'20.1.1.1'}]
        response=self.post('/api/jobs',{'action':'read','draft':draft},400)
        self.assertIn('测试服务',response['detail']);self.assertEqual(self.network.call_count,0)
    def test_demo_targets_never_go_live(self):
        draft=self.demo();self.app.state.settings.test_simulation=False;draft['mode']='live';draft['images']=[]
        self.post('/api/jobs',{'action':'read','draft':draft},400)
        self.assertEqual(self.network.call_count,0)
    def test_special_address_live_refused(self):
        draft=self.demo();self.app.state.settings.test_simulation=False;draft['mode']='live';draft['images']=[]
        for address in ('127.0.0.1','169.254.169.254','0.0.0.0','224.0.0.1'):
            draft['targets']=[{'name':'test','address':address}]
            self.post('/api/jobs',{'action':'read','draft':draft},400)
        self.assertEqual(self.network.call_count,0)
    def test_upload_rejects_paths_and_bios(self):
        for name in ('../LT.svf','BIOS.rom.tar','file.txt'):
            response=self.client.post('/api/images',files={'file':(name,b'inert')},headers=self.headers)
            self.assertEqual(response.status_code,400)
    def test_local_upload_is_not_a_device_upload(self):
        response=self.client.post('/api/images',files={'file':('FIXTURE_LT.svf',b'inert file')},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.network.call_count,0)
        self.assertNotIn('path',response.json())
        self.assertEqual(len(response.json()['sha256']),64)
    def test_changed_image_rejected(self):
        draft=self.demo();row=self.app.state.runtime.store.image(draft['images'][0]['image_id'])
        Path(row['path']).write_bytes(b'changed')
        self.post('/api/jobs',{'action':'precheck','draft':draft},400)
        self.assertFalse(self.post('/api/readiness',draft)['ready'])
    def test_import_csv_and_mapping(self):
        response=self.client.post('/api/targets/import',files={'file':('nodes.csv','节点名,BMC_IP\na04r2n15,20.1.4.35\n'.encode())},headers=self.headers)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['targets'][0]['address'],'https://20.1.4.35')
        self.post('/api/targets/parse',{'text':'a04r2n15 20.1.4.36'},400)
        self.post('/api/targets/parse',{'text':'a 20.1.1.1\nb 20.1.1.1'},400)
    def test_workspace_does_not_include_approval(self):
        draft=self.demo();self.checked(draft)
        workspace=self.post('/api/workspaces/export',draft)
        self.assertNotIn('fixture-password',json.dumps(workspace))
        self.assertFalse(workspace['draft']['canary_confirmed']);self.assertNotIn('model',workspace['draft'])
        restored=self.post('/api/workspaces/restore',workspace)['draft']
        self.assertNotEqual(restored['revision'],draft['revision'])
        self.assertFalse(self.post('/api/readiness',restored)['ready'])
    def test_stop_only_affects_remaining_work(self):
        draft=self.demo();self.checked(draft)
        p=self.post('/api/plans',{'action':'upgrade','draft':draft})
        job=self.post('/api/plans/'+p['id']+'/confirm',{'phrase':p['phrase']})
        response=self.client.post('/api/jobs/'+job['id']+'/stop',json={},headers=self.headers)
        self.assertEqual(response.status_code,200)
        result=self.wait(job);self.assertIn(result['state'],('stopped','completed'))
        self.assertTrue(all(s['power']=='Off' for s in DemoClient.states.values()))
    def test_events_and_audit_no_secrets(self):
        draft=self.demo();job=self.run_('read',{**draft,'images':[]})
        response=self.client.get('/api/jobs/'+job['id']+'/events')
        self.assertEqual(response.status_code,200);self.assertIn('event: record',response.text);self.assertIn('event: done',response.text)
        audit=self.client.get('/api/audit')
        for text in (response.text,audit.text,self.client.get('/api/jobs').text):
            self.assertNotIn('fixture-password',text);self.assertNotIn('demo-password',text)
    def test_unknown_write_method_not_available(self):
        self.post('/api/jobs',{'action':'upgrade','draft':self.demo()},422)
        self.assertEqual(self.client.put('/api/device/reset',json={},headers=self.headers).status_code,404)
    def test_access_key_and_env_not_served(self):
        for path in ('/data/access-key','/.env','/../core/.env'):
            self.assertEqual(self.client.get(path).status_code,404)
    def test_single_service_instance(self):
        from bmc_web.service import Runtime
        other=Runtime(Settings(self.root))
        with self.assertRaises(RuntimeError):other.start()

class ProductionDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.block=patch.object(UpgradeClient,'request',side_effect=AssertionError('device request forbidden'))
        self.network=self.block.start();self.addCleanup(self.block.stop)
        self.app=create_app(Settings(Path(self.temp.name),credential_provider=lambda:('fixture-user','fixture-password')))
        self.client=TestClient(self.app,base_url=ORIGIN);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        response=self.client.post('/api/session',json={'key':self.app.state.sessions.access_key},headers={'Origin':ORIGIN})
        self.headers={'Origin':ORIGIN,'X-CSRF-Token':response.json()['csrf']}
        self.draft={'targets':[{'name':'fixture-node','address':'192.0.2.21'}],'images':[]}
    def test_production_defaults_live_without_global_unlock(self):
        config=self.client.get('/api/config').json()
        self.assertEqual(config['mode'],'live');self.assertTrue(config['confirmation_required'])
        self.assertNotIn('live_writes_enabled',config)
        for action in ('GracefulShutdown','ForceOff','On'):
            response=self.client.post('/api/plans',json={'action':action,'draft':self.draft},headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['mode'],'live')
        self.assertEqual(self.client.get('/api/jobs').json(),[])
        self.assertEqual(self.network.call_count,0)
    def test_legacy_model_is_not_an_execution_requirement(self):
        payload={**self.draft,'model':'OLD-WRONG-MODEL'}
        response=self.client.post('/api/plans',json={'action':'GracefulShutdown','draft':payload},headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        plan=response.json();self.assertNotIn('model',plan)
        self.assertEqual(plan['targets'][0]['model'],'')  # No BMC read was triggered.
        with self.app.state.runtime.store.connect() as db:
            stored=db.execute('SELECT draft FROM plans WHERE id=?',(plan['id'],)).fetchone()[0]
        self.assertNotIn('model',json.loads(stored))
        self.assertEqual(self.client.get('/api/jobs').json(),[])
        self.assertEqual(self.network.call_count,0)
    def test_production_has_no_fixture_creation_endpoint(self):
        self.assertEqual(self.client.post('/api/demo',json={},headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/api/images').json(),[])
        with self.assertRaises(ValueError):self.app.state.runtime.demo()
    def test_old_demo_work_cannot_enter_production(self):
        for action in ('read','power-read','asset-collect'):
            response=self.client.post('/api/jobs',json={'action':action,'draft':{**self.draft,'mode':'demo'}},headers=self.headers)
            self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.network.call_count,0)
    def test_wrong_confirmation_does_not_create_device_approval(self):
        with patch('bmc_web.service.WriteApproval',side_effect=AssertionError('No approval without exact confirmation')) as approval:
            response=self.client.post('/api/plans',json={'action':'ForceOff','draft':self.draft},headers=self.headers)
            self.assertEqual(response.status_code,200)
            pid=response.json()['id']
            result=self.client.post('/api/plans/'+pid+'/confirm',json={'phrase':'wrong'},headers=self.headers)
            self.assertEqual(result.status_code,400)
            self.assertEqual(self.client.get('/api/jobs').json(),[])
            self.assertEqual(approval.call_count,0);self.assertEqual(self.network.call_count,0)

class RecoveryTests(unittest.TestCase):
    def test_restart_marks_jobs_review_never_replays(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store=Store(root)
            draft={'mode':'demo','targets':[],'images':[]}
            with store.connect() as db:
                for state in ('queued','running'):
                    store.insert_job(db,state,'old-session','upgrade',draft)
                    db.execute('UPDATE jobs SET state=? WHERE id=?',(state,state))
            app=create_app(Settings(root,credential_provider=lambda:('u','p')))
            with TestClient(app,base_url=ORIGIN):
                self.assertTrue(all(j['state']=='needs_review' for j in app.state.runtime.store.jobs()))
                self.assertTrue(app.state.runtime.queue.empty())
                self.assertEqual(app.state.runtime.prechecks,{})

if __name__=='__main__':unittest.main(verbosity=2)
