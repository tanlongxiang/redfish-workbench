"""Three-module API uses the existing single worker; secrets stay in memory only."""
from dataclasses import dataclass,field
from pathlib import Path
import copy,hmac,json,secrets,time,uuid,threading
from pydantic import Field,SecretStr
from .models import StrictModel,Node,ConfirmRequest
from fastapi import UploadFile
from .bridge import Target,normalize_target,BmcError
from redfish_ops import catalog
from redfish_workflow import prepare,run_tasks,Approval
from redfish_session import ManagedSession

class Credentials(StrictModel):
    source:str=Field(default='manual',pattern='^(manual|configured)$')
    username:str=Field(default='',max_length=200)
    password:SecretStr=Field(default=SecretStr(''),repr=False)
class BiosPasswords(StrictModel):
    old:SecretStr=Field(repr=False)
    new:SecretStr=Field(repr=False)

class RedfishRequest(StrictModel):
    operation:str=Field(pattern=r'^3\.[347]\.\d+$')
    targets:list[Node]=Field(min_length=1,max_length=2000)
    params:dict[str,str]=Field(default_factory=dict)
    payload:dict=Field(default_factory=dict)
    bios_passwords:BiosPasswords|None=Field(default=None,exclude=True,repr=False)
    credentials:Credentials=Field(default_factory=Credentials,exclude=True,repr=False)
    expected_model:str=Field(default='',max_length=200)
    expected_version:str=Field(default='',max_length=100)
    image_id:str=Field(default='',max_length=60)
    canary_confirmed:bool=False
    insecure:bool=False
    workers:int=Field(default=2,ge=1,le=4)
    timeout:int=Field(default=20,ge=1,le=120)
    task_timeout:int=Field(default=900,ge=1,le=3600)

@dataclass(repr=False)
class RedfishWork:
    id:str
    owner:str
    service:object=field(repr=False)
    targets:list=field(repr=False)
    request:dict=field(repr=False)
    credentials:tuple=field(repr=False)
    approval:object=field(default=None,repr=False)
    def run(self,runtime):
        stop=runtime.stops[self.id]
        with runtime.lock:runtime.active=self.id;runtime.prechecks.clear()
        runtime.store.update_job(self.id,state='running',started=time.time())
        try:
            def callback(target,row):
                if runtime.settings.test_simulation:row['mode']='本机HTTPS模拟'
                runtime.store.event(self.id,{'node':target.name,'address':target.address,'event':'Redfish节点结束','data':row})
            results,folder=run_tasks(self.targets,self.request,self.credentials,runtime.store.root/'reports',approval=self.approval,stop=stop,callback=callback,client_factory=self.service.client_factory)
            state='needs_review' if any(r['state']=='needs_review' for r in results) else 'stopped' if stop.is_set() else 'failed' if any(r['state']!='completed' for r in results) else 'completed'
            runtime.store.update_job(self.id,state=state,finished=time.time(),results=results,report=str(folder))
            runtime.store.event(self.id,{'event':'任务结束','data':{'state':state}})
        except Exception as exc:
            reason=str(exc) if isinstance(exc,(BmcError,ValueError)) else '内部错误：'+type(exc).__name__
            for secret in self.credentials:
                if secret:reason=reason.replace(secret,'[REDACTED]')
            runtime.store.update_job(self.id,state='needs_review',finished=time.time(),error=reason)
        finally:
            self.credentials=();self.request.pop('bios_passwords',None);runtime.store.audit(self.owner,'redfish_finished',job=self.id)

class RedfishService:
    def __init__(self,runtime):
        self.runtime=runtime;self.plans={};self.timers={};self.client_factory=ManagedSession
    def expire(self,pid):
        with self.runtime.lock:
            self.plans.pop(pid,None)
            timer=self.timers.pop(pid,None)
            if timer:timer.cancel()
    def close(self):
        with self.runtime.lock:
            for pid in list(self.plans):self.expire(pid)
    def _prune(self):
        for key,value in list(self.plans.items()):
            if value['expires']<time.time():self.expire(key)
    def inputs(self,body):
        if self.runtime.settings.test_simulation and self.client_factory is ManagedSession:raise ValueError('测试服务禁止真实Redfish连接')
        targets=[Target(n.name,normalize_target(n.address)) for n in body.targets]
        r=body.model_dump(exclude={'targets','credentials','image_id'})
        if body.bios_passwords is not None:r['bios_passwords']={key:getattr(body.bios_passwords,key).get_secret_value() for key in ('old','new')}
        if body.operation=='3.3.22' and body.bios_passwords is None:raise ValueError('BIOS密码变更需要专用旧/新密码字段')
        if body.image_id:
            with self.runtime.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS redfish_images(id TEXT PRIMARY KEY,path TEXT NOT NULL,name TEXT NOT NULL,sha256 TEXT NOT NULL)')
                image=db.execute('SELECT * FROM redfish_images WHERE id=?',(body.image_id,)).fetchone()
            if not image:raise ValueError('本机镜像不存在，请重新导入')
            r['image']=image['path']
        if body.credentials.source=='configured':credentials=self.runtime.settings.credential_provider()
        else:credentials=(body.credentials.username,body.credentials.password.get_secret_value())
        if not all(credentials):raise ValueError('需要本次BMC账号密码，或显式选择后端已配置凭据')
        op,r,public,scope=prepare(targets,r)
        # Never let a user accidentally copy a password into persisted payload/text.
        serialized=json.dumps(public,ensure_ascii=False)
        if credentials[1] in serialized:raise ValueError('计划参数中包含本次密码，拒绝保存')
        return targets,r,public,scope,credentials,op
    def submit(self,owner,body):
        targets,r,public,_,credentials,op=self.inputs(body)
        if op['method']!='GET':raise ValueError('变更操作必须先创建并确认本次计划')
        return self._enqueue(owner,targets,r,public,credentials,None)
    def plan(self,owner,body):
        targets,r,public,scope,credentials,op=self.inputs(body)
        if op['method']=='GET':raise ValueError('只读操作无需变更计划')
        with self.runtime.lock:
            self._prune()
            if len(self.plans)>=30:raise ValueError('待确认计划过多')
            pid=uuid.uuid4().hex;expires=time.time()+120;phrase='确认本次 '+secrets.token_hex(4)
            self.plans[pid]=dict(owner=owner,targets=targets,request=r,public=public,scope=scope,credentials=credentials,expires=expires,phrase=phrase)
            timer=threading.Timer(120,self.expire,args=(pid,));timer.daemon=True;self.timers[pid]=timer;timer.start()
        self.runtime.store.audit(owner,'redfish_plan_created',plan=pid,scope=scope,operation=body.operation)
        return dict(id=pid,expires=expires,phrase=phrase,scope=scope,plan=public)
    def confirm(self,owner,pid,phrase):
        with self.runtime.lock:
            self._prune();p=self.plans.get(pid)
            if not p or p['owner']!=owner:raise ValueError('计划不存在、过期或不属于当前会话')
            if not hmac.compare_digest(phrase.encode(),p['phrase'].encode()):raise ValueError('确认短语不匹配')
            _,_,public,scope=prepare(p['targets'],p['request'])
            if scope!=p['scope']:self.expire(pid);raise ValueError('镜像或计划改变，禁止使用旧确认')
            job=self._enqueue(owner,p['targets'],p['request'],public,p['credentials'],Approval(scope,p['expires']))
            self.expire(pid)
        self.runtime.store.audit(owner,'redfish_plan_confirmed',plan=pid,job=job['id'],scope=scope)
        return job
    def cancel(self,owner,pid):
        with self.runtime.lock:
            p=self.plans.get(pid)
            if p and p['owner']==owner:self.expire(pid)
        return {'ok':True}
    def forget(self,owner):
        with self.runtime.lock:
            for pid,p in list(self.plans.items()):
                if p['owner']==owner:self.expire(pid)
    def _enqueue(self,owner,targets,request,public,credentials,approval):
        runtime=self.runtime;job=uuid.uuid4().hex
        with runtime.lock:
            if runtime.shutting_down or len(runtime.stops)>=30:raise ValueError('任务队列繁忙或正在停止')
            with runtime.store.connect() as db:runtime.store.insert_job(db,job,owner,'redfish',{'mode':'demo' if runtime.settings.test_simulation else 'live',**public})
            runtime._queue(RedfishWork(job,owner,self,targets,request,credentials,approval))
        runtime.store.audit(owner,'redfish_submitted',job=job,operation=request['operation'])
        return runtime.public_job(job)

def install_routes(app,runtime,authenticated):
    from fastapi import Depends,UploadFile,File,HTTPException
    from fastapi.responses import FileResponse
    import hashlib,os
    from .models import ConfirmRequest
    service=RedfishService(runtime);app.state.redfish=service
    @app.get('/api/redfish/catalog')
    def operations(session=Depends(authenticated)):return {'operations':catalog(),'auth':'session','models':['R6250A0','X7450A0'],'writes_live_verified':False,'test_fixture':runtime.settings.test_simulation}
    @app.post('/api/redfish/jobs')
    def submit(body:RedfishRequest,session=Depends(authenticated)):return service.submit(session['id'],body)
    @app.post('/api/redfish/plans')
    def plan(body:RedfishRequest,session=Depends(authenticated)):return service.plan(session['id'],body)
    @app.post('/api/redfish/plans/{pid}/confirm')
    def confirm(pid:str,body:ConfirmRequest,session=Depends(authenticated)):return service.confirm(session['id'],pid,body.phrase)
    @app.delete('/api/redfish/plans/{pid}')
    def cancel(pid:str,session=Depends(authenticated)):return service.cancel(session['id'],pid)
    @app.post('/api/redfish/images')
    async def image(file:UploadFile=File(...),session=Depends(authenticated)):
        import re
        name=file.filename or ''
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',name) or Path(name).suffix.lower() not in {'.ima','.bin','.rom','.jed'}:raise ValueError('G50镜像需ASCII文件名及.ima/.bin/.rom/.jed后缀')
        identifier=uuid.uuid4().hex;folder=runtime.store.root/'redfish_images'/identifier;folder.mkdir(parents=True,mode=0o700)
        path=folder/name;size=0;digest=hashlib.sha256()
        try:
            with path.open('xb') as f:
                os.chmod(path,0o600)
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk)
                    if size>64*1024*1024:raise ValueError('镜像超过64MiB')
                    digest.update(chunk);f.write(chunk)
            if not size:raise ValueError('镜像为空')
            with runtime.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS redfish_images(id TEXT PRIMARY KEY,path TEXT NOT NULL,name TEXT NOT NULL,sha256 TEXT NOT NULL)')
                db.execute('INSERT INTO redfish_images VALUES(?,?,?,?)',(identifier,str(path),name,digest.hexdigest()))
            return {'id':identifier,'name':name,'sha256':digest.hexdigest(),'size':size,'uploaded_to_bmc':False}
        except Exception:
            path.unlink(missing_ok=True);folder.rmdir();raise
        finally:await file.close()
    @app.get('/api/redfish/jobs/{job}/attachments/{name}')
    def attachment(job:str,name:str,session=Depends(authenticated)):
        import re
        row=runtime.store.job(job)
        if row['action']!='redfish' or not row['report'] or not re.fullmatch(r'[a-f0-9]{16}\.bin',name):raise HTTPException(404)
        path=(Path(row['report'])/name).resolve()
        if not path.is_relative_to((runtime.store.root/'reports').resolve()) or not path.is_file():raise HTTPException(404)
        return FileResponse(path,filename=name,media_type='application/octet-stream')
    return service
