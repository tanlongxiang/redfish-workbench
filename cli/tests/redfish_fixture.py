"""Inert HTTPS BMC; binds only loopback. Never imports site credentials."""
from pathlib import Path
import copy,http.server,json,ssl,subprocess,tempfile,threading,uuid

class Fixture:
 def __init__(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  cert,key=self.root/'cert.pem',self.root/'key.pem'
  subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost','-days','1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  self.reset();owner=self
  class Handler(http.server.BaseHTTPRequestHandler):
   def log_message(self,*_):pass
   def send(self,status,data,headers=None):
    body=json.dumps(data).encode();self.send_response(status)
    self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
    for k,v in (headers or {}).items():self.send_header(k,v)
    self.end_headers();self.wfile.write(body)
   def request_(self):
    raw=self.rfile.read(int(self.headers.get('Content-Length',0)));owner.calls.append((self.command,self.path))
    if self.command=='POST' and self.path=='/redfish/v1/SessionService/Sessions':
     if owner.login_reject:return self.send(401,{})
     if json.loads(raw)!={'UserName':'fixture-user','Password':'fixture-password'}:return self.send(401,{})
     identifier='s_'+uuid.uuid4().hex;token='t_'+uuid.uuid4().hex;owner.sessions[identifier]=token
     h={}
     if not owner.no_token:h['X-Auth-Token']=token
     if not owner.no_location:h['Location']=owner.bad_location or '/redfish/v1/SessionService/Sessions/'+identifier
     if owner.malformed_login:
      self.send_response(201)
      for k,v in h.items():self.send_header(k,v)
      self.send_header('Content-Length','8');self.end_headers();self.wfile.write(b'not-json');return
     return self.send(201,{},h)
    token=self.headers.get('X-Auth-Token');session=next((k for k,v in owner.sessions.items() if v==token),None)
    if not session:return self.send(401,{})
    if self.command=='DELETE' and self.path.startswith('/redfish/v1/SessionService/Sessions/'):
     if owner.delete_fail:return self.send(503,{})
     identifier=self.path.rsplit('/',1)[-1]
     if owner.delete_absent:owner.sessions.pop(identifier,None);return self.send(404,{})
     if identifier!=session:return self.send(403,{})
     owner.sessions.pop(identifier,None);return self.send(204,{})
    if self.path==owner.fail_path:return self.send(401,{})
    if self.command=='GET':
     if self.path=='/redfish/v1/SessionService':return self.send(200,{'SessionTimeout':600})
     if self.path=='/redfish/v1/':return self.send(200,{'Systems':{'@odata.id':'/redfish/v1/Systems'},'Chassis':{'@odata.id':'/redfish/v1/Chassis'}})
     if self.path=='/redfish/v1/Systems':return self.send(200,{'Members':[{'@odata.id':'/redfish/v1/Systems/host42'}],'Members@odata.count':1})
     if self.path=='/redfish/v1/Chassis':return self.send(200,{'Members':[{'@odata.id':'/redfish/v1/Chassis/rack42'}],'Members@odata.count':1})
     if self.path.split('?',1)[0].endswith('/Memory'):
      base=self.path.split('?',1)[0];second='$skip=1' in self.path
      result={'Members':[{'@odata.id':base+('/DIMMA1' if second else '/DIMMA0')}],'Members@odata.count':2 if owner.paged else 1}
      if owner.paged and not second:result['Members@odata.nextLink']=base+'?$skip=1'
      return self.send(200,result)
     if self.path.endswith(('/DIMMA0','/DIMMA1')):return self.send(200,{'Id':'DIMMA0','CapacityMiB':32768,'Status':{'State':'Enabled','Health':'OK'}})
     if self.path.endswith('/Tasks/UpdateFirmware'):
      if owner.stale:return self.send(200,{'TaskState':'Completed','PercentComplete':100,'StartTime':'old'})
      return self.send(404,{})
     if '/TaskService/Tasks/' in self.path:return self.send(200,{'TaskState':owner.task_state,'StartTime':'new','PercentComplete':100})
     if '/FirmwareInventory/' in self.path:return self.send(200,{'Version':owner.version,'Updateable':True})
     if self.path=='/redfish/v1/Systems/host42':
      actions={'#ComputerSystem.Reset':{'target':self.path+'/Actions/ComputerSystem.Reset','ResetType@Redfish.AllowableValues':['On','ForceOff','GracefulShutdown']}}
      return self.send(200,{'Model':owner.model,'PowerState':owner.power,'Actions':actions}, {'ETag':'"one"'})
     if self.path.endswith('/Bios'):return self.send(200,{'Attributes':{},'Actions':{'#Bios.ChangePassword':{'target':self.path+'/Actions/Bios.ChangePassword'}}})
     if self.path=='/redfish/v1/UpdateService':return self.send(200,{'Actions':{'#UpdateService.SimpleUpdate':{'target':self.path+'/Actions/UpdateService.SimpleUpdate'},'#UpdateService.SaveConfiguration':{'target':self.path+'/Actions/UpdateService.SaveConfiguration'}}})
     value=copy.deepcopy(owner.resources.get(self.path,{'Id':'fixture','Status':{'Health':'OK'}}))
     if owner.echo:value.update({'X-Auth-Token':token,'Description':f'echo {token} fixture-password'})
     return self.send(200,value,{} if owner.no_etag else {'ETag':'"one"'})
    owner.writes.append((self.command,self.path))
    if owner.write_fail:return self.send(503,{})
    if self.command=='PATCH':
     if self.headers.get('If-Match')!='"one"':return self.send(412,{})
     owner.resources.setdefault(self.path,{}).update(json.loads(raw));return self.send(200,{})
    if self.path.endswith('/Actions/ComputerSystem.Reset'):
     owner.power='On' if json.loads(raw)['ResetType']=='On' else 'Off';return self.send(200,{})
    if self.path.endswith('/Actions/Bios.ChangePassword'):
     owner.password_changes.append(json.loads(raw));return self.send(200,{'NewPassword':json.loads(raw)['NewPassword'],'Description':'echo '+json.loads(raw)['NewPassword']})
    if self.path.endswith('/FirmwareInventory'):
     if b'name="fwimage"' not in raw:return self.send(400,{})
     return self.send(200,{'Result':'uploaded'})
    if self.path.endswith('/Actions/UpdateService.SimpleUpdate'):
     owner.version='2.0';uri='/redfish/v1/TaskService/Tasks/'+('UpdateFirmware' if owner.stale else 'new42')
     return self.send(202,{'@odata.id':uri},{'Location':uri})
    return self.send(200,{})
   do_GET=request_;do_POST=request_;do_PATCH=request_;do_DELETE=request_
  self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
  ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);self.server.socket=ctx.wrap_socket(self.server.socket,server_side=True)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.address=f'https://127.0.0.1:{self.server.server_port}'
 def reset(self):
  self.calls=[];self.writes=[];self.sessions={};self.resources={};self.version='1.0';self.model='R6250A0';self.power='Off';self.task_state='Completed'
  self.login_reject=False;self.no_token=False;self.no_location=False;self.bad_location='';self.delete_fail=False;self.delete_absent=False;self.fail_path='';self.no_etag=False;self.echo=False;self.write_fail=False;self.stale=False;self.malformed_login=False;self.paged=False;self.password_changes=[]
 def close(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
