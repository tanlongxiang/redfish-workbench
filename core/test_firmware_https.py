"""Real updater transport -> localhost HTTPS BMC fixture. No real device access."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import base64
import http.server
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest

from firmware_auth import confirmation_key
from firmware_batch import Options, Target, Image, COMPONENTS, CPLD, INVENTORY, run_batch, WriteApproval

@unittest.skipUnless(shutil.which('openssl'), 'openssl unavailable')
class HTTPSUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temp.name)
        cert, key = cls.folder/'cert.pem', cls.folder/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost','-days','1'],
                       check=True, stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        owner=cls
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def send(self,status,data,headers=None):
                body=json.dumps(data).encode()
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                for k,v in (headers or {}).items(): self.send_header(k,v)
                self.end_headers(); self.wfile.write(body)
            def read(self):
                return self.rfile.read(int(self.headers.get('Content-Length','0')))
            def authorized(self):
                return self.headers.get('Authorization')=='Basic '+base64.b64encode(b'fixture-user:fixture-password').decode() or self.headers.get('X-Auth-Token')=='fixture-session'
            def do_GET(self):
                if not self.authorized(): return self.send(401,{})
                if self.path=='/redfish/v1/': return self.send(200,{'Systems':{'@odata.id':'/redfish/v1/Systems'}})
                if self.path=='/redfish/v1/Systems': return self.send(200,{'Members':[{'@odata.id':'/redfish/v1/Systems/host42'}],'Members@odata.count':1})
                if self.path=='/redfish/v1/Systems/host42':
                    action={'target':self.path+'/Actions/ComputerSystem.Reset','ResetType@Redfish.AllowableValues':['On','GracefulShutdown','ForceOff']}
                    return self.send(200,{'Model':'CY62-T58','PowerState':owner.power,'Actions':{'#ComputerSystem.Reset':action}})
                if self.path.startswith(INVENTORY): return self.send(200,{'Version':owner.versions[self.path.removeprefix(INVENTORY)],'Updateable':True,'Status':{'State':'Enabled'}})
                if self.path==CPLD:
                    busy=owner.ticks>0
                    if busy:
                        owner.ticks-=1
                        if owner.ticks==0: owner.versions[owner.target]='0.5'
                    return self.send(200,{'data':{'Activation':'Activating' if busy else 'Active','Progress':50 if busy else 100,
                         'ExtendedVersion':owner.target,'VersionList':owner.versions,'VendorList':dict.fromkeys(COMPONENTS,'Lattice'),
                         'EffectiveList':[[c,'gpio','poweroff'] for c in COMPONENTS]},'status':'ok'})
                return self.send(404,{})
            def do_POST(self):
                raw=self.read()
                if self.path=='/redfish/v1/SessionService/Sessions':
                    if json.loads(raw)!={'UserName':'fixture-user','Password':'fixture-password'}: return self.send(401,{})
                    return self.send(201,{}, {'X-Auth-Token':'fixture-session','Location':'/redfish/v1/SessionService/Sessions/ours'})
                return self.write_(raw)
            def do_PUT(self): return self.write_(self.read())
            def write_(self,raw):
                if not self.authorized() or self.headers.get('AUTHKEY')!=confirmation_key('fixture-password'): return self.send(403,{})
                owner.seen.append((self.command,self.path))
                if owner.reject and self.command=='PUT': return self.send(403,{'error':'denied'})
                if self.path.startswith('/upload/image/'):
                    if raw!=b'FIXTURE-INERT-SVF': return self.send(400,{})
                elif self.path.endswith('ExtendedVersion'): owner.target=json.loads(raw)['data']
                elif self.path.endswith('RequestedActivation'): owner.ticks=2
                elif self.path.endswith('/Actions/ComputerSystem.Reset'):
                    owner.power='On' if json.loads(raw)['ResetType']=='On' else 'Off'
                else: return self.send(404,{})
                self.send(200,{'status':'ok'})
            def do_DELETE(self):
                owner.deleted.append(self.path)
                self.send(200,{})
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key)
        cls.server.socket=ctx.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.address=f'127.0.0.1:{cls.server.server_port}'
        cls.imagepath=cls.folder/'FIXTURE_LT.svf';cls.imagepath.write_bytes(b'FIXTURE-INERT-SVF')
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def setUp(self):
        type(self).versions=dict.fromkeys(COMPONENTS,'0.3');type(self).target='Dcu57Cpld';type(self).ticks=0
        type(self).seen=[];type(self).deleted=[];type(self).power='Off';type(self).reject=False
        self.images=[Image.prepare(c,self.imagepath,'0.5') for c in COMPONENTS]
        self.targets=[Target('localhost-fixture',self.address)]
    def run_(self, action, auth='basic'):
        approval=WriteApproval(self.targets,self.images,'CY62-T58',action) if action != 'verify' else None
        opts=Options('fixture-user','fixture-password',model='CY62-T58',self_signed=True,poll=.01,auth=auth,approval=approval)
        return run_batch(self.targets,self.images,opts,action,folder=self.folder)
    def test_basic_upload_four_and_readback(self):
        results,folder=self.run_('upgrade')
        self.assertEqual(results[0]['status'],'写入完成，待生效核验')
        self.assertEqual(len(self.seen),12)
        results,_=self.run_('verify')
        self.assertIn('读回一致',results[0]['status'])
        self.assertNotIn('fixture-password',(folder/'events.jsonl').read_text())
    def test_selected_groups_only_over_https(self):
        self.images=[self.images[1],self.images[3]]
        results,_=self.run_('upgrade')
        self.assertEqual([i['component'] for i in results[0]['components']],[COMPONENTS[1],COMPONENTS[3]])
        self.assertEqual(len(self.seen),6)
        self.assertEqual(type(self).versions,{c:'0.5' if c in (COMPONENTS[1],COMPONENTS[3]) else '0.3' for c in COMPONENTS})
    def test_session_upload_and_logout(self):
        results,_=self.run_('upgrade','session')
        self.assertEqual(results[0]['status'],'写入完成，待生效核验')
        self.assertEqual(self.deleted,['/redfish/v1/SessionService/Sessions/ours'])
    def test_403_not_retried(self):
        type(self).reject=True
        results,_=self.run_('upgrade')
        self.assertEqual(results[0]['status'],'停止并待核验')
        self.assertEqual(len(self.seen),2)  # one upload + one target PUT, no repeat
    def test_power_action_discovered_not_guessed(self):
        results,_=self.run_('On');self.assertEqual(results[0]['status'],'已确认 On')
        results,_=self.run_('GracefulShutdown');self.assertEqual(results[0]['status'],'已确认 Off')

if __name__=='__main__': unittest.main(verbosity=2)
