"""Actual CLI -> loopback HTTPS fixture; never connects to a physical BMC.
PTY confirmation is driven only for 127.0.0.1 and inert fixture images.
"""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
CORE=ROOT/'_engine' if (ROOT/'_engine').is_dir() else ROOT.parent/'core'
sys.path.insert(0,str(CORE))
import base64
import http.server
import json
import os
import re
import selectors
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from firmware_auth import confirmation_key
from node_verify import FIRMWARE
from firmware_batch import COMPONENTS,CPLD,INVENTORY

@unittest.skipUnless(shutil.which('openssl'),'openssl command unavailable: TLS fixture skipped')
class CLIHTTPS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cert,key=cls.root/'cert.pem',cls.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost','-days','1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        owner=cls
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def send(self,code,value):
                raw=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def authorized(self):return self.headers.get('Authorization')=='Basic '+base64.b64encode(b'fixture-user:fixture-password').decode()
            def do_GET(self):
                owner.gets.append(self.path)
                if not self.authorized():return self.send(401,{})
                if self.path=='/redfish/v1/':return self.send(200,{'Systems':{'@odata.id':'/redfish/v1/Systems'}})
                if self.path=='/redfish/v1/Systems':return self.send(200,{'Members':[{'@odata.id':'/redfish/v1/Systems/only'}],'Members@odata.count':1})
                if self.path=='/redfish/v1/Systems/only':
                    return self.send(200,{'Model':'fixture-model','Manufacturer':'SIMULATED','SerialNumber':'001234567890123456789','PowerState':owner.power,'ProcessorSummary':{'Count':2,'Model':'fixture CPU'},'MemorySummary':{'TotalSystemMemoryGiB':sum(owner.sizes)/1024},'Memory':{'@odata.id':'/redfish/v1/Systems/only/Memory'},'Actions':{'#ComputerSystem.Reset':{'target':self.path+'/Actions/ComputerSystem.Reset','ResetType@Redfish.AllowableValues':['On','GracefulShutdown','ForceOff']}}})
                memory='/redfish/v1/Systems/only/Memory'
                if self.path==memory:return self.send(200,{'Members':[{'@odata.id':memory+'/'+str(i)} for i in range(len(owner.sizes))],'Members@odata.count':len(owner.sizes)})
                if self.path.startswith(memory+'/'):
                    i=int(self.path.rsplit('/',1)[-1]);return self.send(200,{'Id':str(i),'DeviceLocator':'DIMM_'+str(i),'CapacityMiB':owner.sizes[i],'Manufacturer':'SIMULATED','PartNumber':'FIXTURE','Status':{'State':'Enabled','Health':'OK'}})
                if self.path.startswith(INVENTORY):
                    if owner.missing and self.path.endswith('Vbis0'):return self.send(404,{})
                    return self.send(200,{'Version':owner.versions[self.path.removeprefix(INVENTORY)],'Updateable':True,'Status':{'State':'Enabled'}})
                if self.path==CPLD:
                    busy=owner.ticks>0
                    if busy:
                        owner.ticks-=1
                        if not owner.ticks:owner.versions[owner.target]='0.5'
                    return self.send(200,{'data':{'Activation':'Activating' if busy else 'Active','Progress':50 if busy else 100,'ExtendedVersion':owner.target,'VersionList':owner.versions,'VendorList':dict.fromkeys(COMPONENTS,'Lattice'),'EffectiveList':[[c,'gpio','poweroff'] for c in COMPONENTS]}})
                self.send(404,{})
            def write_(self):
                data=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if not self.authorized() or self.headers.get('AUTHKEY')!=confirmation_key('fixture-password'):return self.send(403,{})
                owner.writes.append((self.command,self.path))
                if self.path.startswith('/upload/image/'):
                    if data!=b'INERT-CLI-FIXTURE':return self.send(400,{})
                elif self.path.endswith('ExtendedVersion'):
                    if owner.reject:return self.send(403,{})
                    owner.target=json.loads(data)['data']
                elif self.path.endswith('RequestedActivation'):owner.ticks=2
                elif self.path.endswith('/Actions/ComputerSystem.Reset'):owner.power='On' if json.loads(data)['ResetType']=='On' else 'Off'
                else:return self.send(404,{})
                self.send(200,{'status':'ok'})
            do_POST=write_
            do_PUT=write_
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);cls.server.socket=ctx.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.address='127.0.0.1:'+str(cls.server.server_port)
        chosen=os.environ.get('cli_APP')
        cls.entry=Path(chosen) if chosen else ROOT/'bmc_tool.py'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def setUp(self):
        self.temp_case=tempfile.TemporaryDirectory(dir=self.root);self.addCleanup(self.temp_case.cleanup);self.folder=Path(self.temp_case.name)
        cls=type(self);cls.versions={endpoint:'0.3' for _,endpoint in FIRMWARE};cls.target='Dcu57Cpld';cls.ticks=0;cls.power='Off';cls.writes=[];cls.gets=[];cls.missing=False;cls.reject=False;cls.sizes=[65536,65536]
        self.image=self.folder/'FIXTURE_LT.svf';self.image.write_bytes(b'INERT-CLI-FIXTURE')
        self.env={**os.environ,'BMC_USERNAME':'fixture-user','BMC_PASSWORD':'fixture-password'}
    def cmd(self,*args):
        return [sys.executable,'-I','-S','-B','-u',str(self.entry),*map(str,args),'--target','fixture='+self.address,'--insecure','--state-dir',str(self.folder/'state'),'--output',str(self.folder/'reports')]
    def normal(self,*args):
        return subprocess.run(self.cmd(*args),cwd=self.folder,env=self.env,input='',capture_output=True,text=True,timeout=40)
    def tty(self,*args,cancel=False):
        if os.name!='posix':self.skipTest('PTY interaction fixture requires POSIX; normal commands remain portable')
        master,slave=os.openpty();process=None
        try:
            process=subprocess.Popen(self.cmd(*args),cwd=self.folder,env=self.env,stdin=slave,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            os.close(slave);slave=None
            selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
            output=bytearray();sent=False;deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                events=selector.select(timeout=.5)
                for key,_ in events:
                    chunk=os.read(key.fileobj.fileno(),65536)
                    if not chunk:
                        selector.unregister(key.fileobj);continue
                    output.extend(chunk)
                text=output.decode(errors='replace')
                match=re.search('原样输入：([^\r\n]+)',text)
                if match and not sent:
                    os.write(master,(('取消' if cancel else match[1])+'\n').encode());sent=True
                if process.poll() is not None and not selector.get_map():break
            else:self.fail('CLI/fixture process timed out')
            self.assertNotIn('fixture-password',output.decode(errors='replace'))
            return process.returncode,output.decode(errors='replace'),sent
        finally:
            if slave is not None:os.close(slave)
            os.close(master)
            if process is not None:
                if process.poll() is None:process.kill();process.wait()
                if process.stdout:process.stdout.close()
    def test_single_file_collect_and_ledger(self):
        result=self.normal('collect','--live','--firmware')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(type(self).writes,[])
        values=list((self.folder/'reports').glob('*/results.json'));self.assertEqual(len(values),1)
        records=json.loads(values[0].read_text());self.assertEqual(len(records[0]['firmware']),38)
        self.assertEqual(records[0]['system_sn'],'001234567890123456789')
        self.assertNotIn('fixture-password',result.stdout+result.stderr)
    def test_memory_dimms_and_three_sheet_report(self):
        result=self.normal('collect','--live','--memory');self.assertEqual(result.returncode,0,result.stderr)
        files=list((self.folder/'reports').glob('*/results.json'));r=json.loads(files[0].read_text())[0]
        self.assertEqual(r['memory']['summary']['count_64gb'],2)
        from xlsx_io import InputBook
        book=InputBook(str(files[0].with_name('内存配置统计.xlsx')))
        self.assertEqual(len(book.read_sheet('内存条明细')),3)
        self.assertEqual(type(self).writes,[])
    def test_memory_mixed_32_64(self):
        type(self).sizes=[32768,65536]
        result=self.normal('collect','--live','--memory');self.assertEqual(result.returncode,0,result.stderr)
        file=next((self.folder/'reports').glob('*/results.json'));r=json.loads(file.read_text())[0]
        self.assertIn('混插',r['memory']['summary']['conclusion'])
        self.assertEqual(r['memory']['summary']['count_32gb'],1)
    def test_basic_collect_has_no_firmware_requests(self):
        result=self.normal('collect','--live');self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(any(path.startswith(INVENTORY) for path in type(self).gets))
    def test_partial_firmware_returns_distinct_exit_code(self):
        type(self).missing=True
        result=self.normal('collect','--live','--firmware');self.assertEqual(result.returncode,4,result.stderr)
    def test_power_only_status_without_cpld_or_image(self):
        result=self.normal('power','status','--live');self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(any(path.startswith(CPLD) or path.startswith(INVENTORY) for path in type(self).gets))
        self.assertEqual(type(self).writes,[])
    def test_power_write_pipe_rejected_before_any_connection(self):
        result=self.normal('power','on','--live','--execute','--model','fixture-model')
        self.assertEqual(result.returncode,2);self.assertEqual(type(self).gets,[]);self.assertEqual(type(self).writes,[])
    def test_cancelled_tty_confirmation_never_writes(self):
        code,output,sent=self.tty('power','on','--live','--execute','--model','fixture-model',cancel=True)
        self.assertEqual(code,2,output);self.assertTrue(sent);self.assertEqual(type(self).writes,[])
    def test_tty_power_on_uses_only_discovered_reset_action(self):
        code,output,sent=self.tty('power','on','--live','--execute','--model','fixture-model')
        self.assertEqual(code,0,output);self.assertTrue(sent);self.assertEqual(type(self).power,'On')
        self.assertEqual(type(self).writes,[('POST','/redfish/v1/Systems/only/Actions/ComputerSystem.Reset')])
        self.assertFalse(any(path.startswith(CPLD) for path in type(self).gets))
    def test_tty_cpld_selected_group_only(self):
        code,output,sent=self.tty('cpld','upgrade','--live','--execute','--model','fixture-model','--image','02='+str(self.image))
        self.assertEqual(code,0,output);self.assertTrue(sent)
        self.assertEqual(len(type(self).writes),3)
        self.assertEqual(type(self).versions['Dcu02Cpld'],'0.5');self.assertEqual(type(self).versions['Dcu13Cpld'],'0.3')
    def test_tty_cpld_on_blocks_before_upload_or_confirmation(self):
        type(self).power='On'
        code,output,sent=self.tty('cpld','upgrade','--live','--execute','--model','fixture-model','--image','02='+str(self.image))
        self.assertEqual(code,2,output);self.assertFalse(sent);self.assertEqual(type(self).writes,[])
    def test_failed_write_not_retried(self):
        type(self).reject=True
        code,output,sent=self.tty('cpld','upgrade','--live','--execute','--model','fixture-model','--image','02='+str(self.image))
        self.assertEqual(code,3,output);self.assertTrue(sent);self.assertEqual(len(type(self).writes),2)

if __name__=='__main__':unittest.main(verbosity=2)
