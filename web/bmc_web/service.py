"""Serialized background task runner. Device writes require a confirmed exact-scope plan.
The worker is independent of HTTP requests/browser lifetime, not a distributed queue.
Queued/in-flight jobs are never replayed automatically after service interruption.
"""
from __future__ import annotations
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from .bridge import (COMPONENTS, Image, Options, Target, WriteApproval, run_batch,
                     parse_targets, DemoClient, DEMO_MARKER, ReadinessGate, BmcError, load_credentials, version, UpgradeClient)
from .models import Draft
from .store import Store
from .security import ServiceLock
from .asset_catalog import Catalog
from .inventory import run_inventory

WRITES={'upgrade','GracefulShutdown','ForceOff','On'}
LABELS={'power-read':'读取电源', 'asset-collect':'资产采集', 'read':'读取状态','precheck':'升级预检查','verify':'版本核验','upgrade':'升级',
        'GracefulShutdown':'正常关机','ForceOff':'强制关机','On':'上电'}

@dataclass
class Settings:
    data_dir: Path
    origin: str = 'http://127.0.0.1:38080'
    # Only the offline test fixture opts in; the production launcher never does.
    test_simulation: bool = False
    test_loopback: bool = False
    credential_provider: object = field(default=load_credentials,repr=False)

@dataclass
class Work:
    id: str
    owner: str
    action: str
    draft: Draft
    options: Options = field(repr=False)
    scope: str = ''
    expires: float = 0
    proof: object = field(default=None,repr=False)

class Runtime:
    def __init__(self, settings):
        self.settings=settings;self.store=Store(settings.data_dir);self.catalog=Catalog(self.store)
        self.service_lock=ServiceLock(settings.data_dir)
        self.lock=threading.RLock();self.queue=queue.Queue();self.stops={};self.prechecks={}
        self.image_signatures={};self.shutting_down=False;self.active=None
        self.secret=secrets.token_bytes(32)
        self.thread=None
    def start(self):
        self.service_lock.acquire()
        try:
            self.store.recover()
            self.thread=threading.Thread(target=self._loop,name='bmc-task-worker',daemon=False)
            self.thread.start()
        except Exception:self.service_lock.release();raise
    def close(self):
        with self.lock:
            self.shutting_down=True
            for stop in self.stops.values():stop.set()
        self.queue.put(None)
        if self.thread:self.thread.join()  # Do not interrupt a started firmware flash.
        self.service_lock.release()
    def validate_mode(self, mode, *, device=False):
        if mode not in ('live','demo'):raise ValueError('模式无效')
        if mode=='demo' and not self.settings.test_simulation:
            raise ValueError('正式工作台不接受旧演示配置，请加载真实节点清单')
        if device and mode=='live' and self.settings.test_simulation:
            raise ValueError('自动化测试服务禁止真实 BMC 连接')
    def targets(self, draft):
        self.validate_mode(draft.mode,device=True)
        targets=[]
        for n in draft.targets:
            targets.extend(parse_targets(n.name+' '+n.address))
        # Validates duplicates across separate rows too.
        from firmware_batch import validate_targets
        targets=validate_targets(targets)
        if draft.mode=='live':
            for t in targets:
                if t.name.startswith('DEMO-'):
                    raise ValueError('演示节点不能用于真实模式，请加载真实节点清单')
                from urllib.parse import urlsplit
                ip=ipaddress.ip_address(urlsplit(t.address).hostname)
                if ip.is_link_local or ip.is_multicast or ip.is_unspecified or (ip.is_loopback and not self.settings.test_loopback):
                    raise ValueError('真实模式不允许回环、链路本地、组播或未指定地址')
        return targets
    def options(self, draft):
        if draft.mode=='demo':user,password='demo-user','demo-password'
        else:user,password=self.settings.credential_provider()
        return Options(user,password,model='',auth=draft.auth,self_signed=draft.self_signed,
                       workers=draft.workers,canary_confirmed=draft.mode=='demo' or draft.canary_confirmed,poll=.12 if draft.mode=='demo' else 3)
    def _fingerprint(self,path):
        s=path.stat();return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns
    def images(self, draft):
        if len({i.component for i in draft.images})!=len(draft.images):raise ValueError('重复升级组件')
        result=[]
        for choice in draft.images:
            row=self.store.image(choice.image_id)
            path=Path(row['path']).resolve()
            if not path.is_relative_to((self.store.root/'images').resolve()):raise ValueError('非法镜像路径')
            if choice.expected.strip():version(choice.expected)
            if path.stat().st_size!=row['size'] or path.stat().st_size>64*1024*1024:
                raise ValueError('镜像大小已改变，请重新上传')
            sig=self._fingerprint(path)
            with self.lock:
                known=self.image_signatures.get(row['id'])
                if known is None:
                    if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('镜像内容已改变，请重新上传')
                    self.image_signatures[row['id']]=sig
                elif sig!=known:raise ValueError('镜像文件已改变，请重新上传并预检查')
            if draft.mode=='live' and row['demo']:raise ValueError('演示镜像禁止用于真实 BMC')
            result.append(Image(choice.component,path,choice.expected.strip(),row['sha256'],'Lattice'))
        return tuple(result)
    def scope(self, draft, options):
        credentials=hmac.new(self.secret,(options.username+'\0'+options.password).encode(),hashlib.sha256).hexdigest()
        data=draft.model_dump();data.pop('canary_confirmed',None);data.pop('workers',None);data.pop('model',None)
        data['credential_context']=credentials
        data['image_hashes']=[self.store.image(i.image_id)['sha256'] for i in draft.images]
        return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    def readiness(self, owner, draft):
        try:
            targets=self.targets(draft);images=self.images(draft);options=self.options(draft);scope=self.scope(draft,options)
            with self.lock:
                proof=self.prechecks.get((owner,scope))
                gate=copy.deepcopy(proof) if proof else ReadinessGate(ttl=120)
            if not proof:
                assets={a['address']:a for a in self.store.assets(draft.mode)}
                for target in targets:
                    a=assets.get(target.address)
                    if a:
                        age=max(0,time.time()-a['observed'])
                        gate.observe(target.address,a['snapshot'],time.monotonic()-age)
            enabled,reason=gate.evaluate(scope,targets,images,options,demo=draft.mode=='demo')
            with self.lock:
                if self.active or self.stops:enabled,reason=False,'已有任务执行中或排队；完成后请重新预检查'
            return {'ready':enabled,'reason':reason,'scope':scope}
        except (ValueError,OSError,BmcError) as exc:return {'ready':False,'reason':str(exc)}
    def add_image(self,path,name,demo=False):
        image=Image.prepare(COMPONENTS[0],path,'0')
        with path.open('rb') as f:demo=demo or f.read(len(DEMO_MARKER))==DEMO_MARKER
        row={'id':uuid.uuid4().hex,'name':name,'sha256':image.sha256,'size':path.stat().st_size,'path':str(path),'demo':demo}
        self.store.add_image(row)
        with self.lock:self.image_signatures[row['id']]=self._fingerprint(path)
        return {k:v for k,v in row.items() if k!='path'}
    def demo(self):
        if not self.settings.test_simulation:raise ValueError('仅自动化测试夹具可创建模拟数据')
        with self.lock:
            if self.active or self.stops:raise ValueError('有任务未结束，请先完成后再重新加载演示')
            targets=[Target('DEMO-01','192.0.2.11'),Target('DEMO-02','192.0.2.12'),Target('DEMO-03','192.0.2.13')]
            options=Options('demo-user','demo-password',model='CY62-T58')
            for target in targets:
                DemoClient.states.pop(target.address,None);DemoClient(target.address,options)
            DemoClient.states[targets[0].address]['power']='On'
            self.prechecks.clear()
            with self.store.connect() as db:
                for target in targets:
                    db.execute("DELETE FROM assets WHERE mode='demo' AND address=?",(target.address,))
                    db.execute("UPDATE asset_catalog SET data='{}' WHERE mode='demo' AND address=?",(target.address,))
            self.catalog.register('demo',[{'name':t.name,'address':t.address} for t in targets])
        folder=self.store.root/'images'/uuid.uuid4().hex;folder.mkdir(parents=True,mode=0o700)
        path=folder/'DEMO_LT_NOT_FOR_HARDWARE.svf';path.write_bytes(DEMO_MARKER+b'0.5\n');os.chmod(path,0o600)
        image=self.add_image(path,path.name,True)
        return {'targets':[{'name':t.name,'address':t.address} for t in targets],'image':image,'model':'CY62-T58'}
    def _job_input(self,draft,action):
        targets=self.targets(draft);options=self.options(draft)
        images=self.images(draft) if action not in ('read','power-read','asset-collect','GracefulShutdown','ForceOff','On') else ()
        if action in ('precheck','verify','upgrade') and not images:raise ValueError('请至少勾选并配置一个 DCU 组')
        return targets,images,options
    def _queue(self, work):
        with self.lock:
            if self.shutting_down:raise ValueError('服务正在停止')
            stop=threading.Event();self.stops[work.id]=stop
            self.queue.put(work)
    def submit(self, owner, action, draft):
        if action not in ('read','power-read','asset-collect','precheck','verify'):raise ValueError('写操作必须先创建并确认执行计划')
        draft=draft.model_copy(update={'model':''})  # Ignore legacy expected-model inputs.
        if action in ('read','power-read','asset-collect'):draft=draft.model_copy(update={'images':[]})
        _,_,options=self._job_input(draft,action)
        job=uuid.uuid4().hex
        with self.lock:
            if self.shutting_down or len(self.stops)>=30:raise ValueError('任务队列繁忙或正在停止')
            with self.store.connect() as db:self.store.insert_job(db,job,owner,action,draft.model_dump())
            self._queue(Work(job,owner,action,draft,options,self.scope(draft,options)))
        self.store.audit(owner,'job_submitted',job=job,action=action,mode=draft.mode)
        return self.public_job(job)
    def plan(self, owner, action, draft):
        if action not in WRITES:raise ValueError('无效执行动作')
        draft=draft.model_copy(update={'model':''})
        if action!='upgrade':draft=draft.model_copy(update={'images':[]})
        self._job_input(draft,action)
        if action=='upgrade':
            ready=self.readiness(owner,draft)
            if not ready['ready']:raise ValueError('禁止升级：'+ready['reason'])
        targets,images,options=self._job_input(draft,action)
        scope=self.scope(draft,options);pid=uuid.uuid4().hex
        phrase=f'确认{LABELS[action]} {len(targets)} 台 {scope[:6]}'
        cached={a['address']:a for a in self.store.assets(draft.mode)}
        summary={'id':pid,'action':action,'label':LABELS[action],'mode':draft.mode,
                 'targets':[{'name':t.name,'address':t.address,
                             'model':cached.get(t.address,{}).get('snapshot',{}).get('model','')} for t in targets],
                 'images':[{'component':i.component,'filename':i.path.name,'sha256':i.sha256,'expected':i.expected} for i in images],
                 'excluded':[c for c in COMPONENTS if c not in {i.component for i in images}],
                 'phrase':phrase,'expires':time.time()+120,'scope':scope,
                 'power_policy':('仅升级所选组件；不自动关机、不自动上电' if action=='upgrade' else
                                 '仅执行本次'+LABELS[action]+'；不自动升级或执行其他电源动作')}
        with self.store.connect() as db:db.execute('INSERT INTO plans(id,owner,action,draft,scope,phrase,created,expires) VALUES(?,?,?,?,?,?,?,?)',
                  (pid,owner,action,draft.model_dump_json(),scope,phrase,time.time(),summary['expires']))
        self.store.audit(owner,'plan_created',plan=pid,action=action,scope=scope,mode=draft.mode)
        return summary
    def confirm(self, owner, pid, phrase):
        with self.lock:
            if self.shutting_down or len(self.stops)>=30:raise ValueError('任务队列繁忙或正在停止')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                p=db.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
                if not p or p['owner']!=owner:raise ValueError('计划不存在或不属于当前会话')
                if p['state']!='pending' or p['expires']<time.time():raise ValueError('计划已使用或过期，请重新预检查和确认')
                if not hmac.compare_digest(phrase.encode('utf-8'),p['phrase'].encode('utf-8')):raise ValueError('确认短语不匹配')
                draft=Draft.model_validate_json(p['draft']);action=p['action']
                _,_,options=self._job_input(draft,action)
                scope=self.scope(draft,options)
                if scope!=p['scope']:raise ValueError('凭据或计划已改变，请重新预检查')
                proof=None
                if action=='upgrade':
                    ready=self.readiness(owner,draft)
                    if not ready['ready']:raise ValueError('禁止升级：'+ready['reason'])
                    proof=copy.deepcopy(self.prechecks.get((owner,scope)))
                job=uuid.uuid4().hex
                self.store.insert_job(db,job,owner,action,draft.model_dump())
                db.execute("UPDATE plans SET state='consumed',job=? WHERE id=?",(job,pid))
            # Clear all UI-ready records before queueing any write; no reuse across tasks.
            self.prechecks.clear()
            self._queue(Work(job,owner,action,draft,options,scope,p['expires'],proof))
        self.store.audit(owner,'plan_confirmed',plan=pid,job=job,scope=scope,mode=draft.mode)
        return self.public_job(job)
    def stop(self, owner, job):
        with self.lock:
            event=self.stops.get(job)
            if not event:raise ValueError('任务已结束或不在当前执行队列')
            event.set();self.prechecks.clear()
        self.store.update_job(job,stop_requested=1)
        self.store.audit(owner,'stop_requested',job=job)
        return {'ok':True,'message':'已停止后续任务；不打断正在烧写的组件，不自动切电或上电'}
    def public_job(self,job):
        value=self.store.job(job)
        value.pop('owner',None);value.pop('report',None)
        value['has_report']=bool(self.store.job(job)['report'])
        return value
    def _loop(self):
        while True:
            work=self.queue.get()
            if work is None:self.queue.task_done();return
            try:self._run(work)
            except Exception:
                # Failed durable audit/storage must fail closed, not silently kill the worker.
                with self.lock:
                    self.shutting_down=True
                    for event in self.stops.values():event.set()
            finally:
                with self.lock:self.stops.pop(work.id,None);self.active=None
                self.queue.task_done()
    def _run(self,work):
        from .redfish_service import RedfishWork
        if isinstance(work,RedfishWork):return work.run(self)
        with self.lock:
            stop=self.stops[work.id];self.active=work.id
            # A fresh read may contradict a previous Off/model/version observation.
            # Only this task's completed precheck can grant a new readiness proof.
            self.prechecks.clear()
        self.store.update_job(work.id,state='running',started=time.time())
        gate=ReadinessGate(ttl=120)
        try:
            if stop.is_set() or self.shutting_down:
                self.store.update_job(work.id,state='stopped',finished=time.time());return
            targets,images,options=self._job_input(work.draft,work.action)
            if self.scope(work.draft,options)!=work.scope:raise ValueError('排队期间凭据或计划变化，任务未执行')
            if work.action in WRITES:
                if work.expires<time.time():raise ValueError('排队期间确认已过期，任务未执行；请重新确认')
                if work.action=='upgrade':
                    if work.proof is None:raise ValueError('缺少本次预检查证明')
                    ok,reason=work.proof.evaluate(work.scope,targets,images,options,demo=work.draft.mode=='demo')
                    if not ok:raise ValueError('执行前门禁未通过：'+reason)
                if work.draft.mode=='live':
                    # Only a persisted, consumed operator-confirmed plan can reach here.
                    options=replace(options,approval=WriteApproval(targets,images,options.model,work.action))
            def callback(target,event,data):
                try:
                    self.store.event(work.id,{'node':target.name,'address':target.address,'event':event,'data':data})
                    if event=='电源状态确认' and data.get('power') in ('On','Off'):
                        self.catalog.observe(work.draft.mode,target,{'power':data['power']})
                        cached=next((a for a in self.store.assets(work.draft.mode) if a['address']==target.address),None)
                        if cached:
                            snap=dict(cached['snapshot']);snap['power']=data['power'];snap['power_observed_at']=time.time()
                            self.store.asset(work.draft.mode,target,snap,time.time())
                    if data.get('inventory'):
                        self.catalog.collected(work.draft.mode,target,data['inventory'])
                    if data.get('snapshot'):
                        snap=data['snapshot'];duration=snap.get('collection_duration_seconds',0)
                        with self.lock:gate.observe(target.address,snap,time.monotonic()-duration)
                        self.store.asset(work.draft.mode,target,snap,time.time()-duration)
                        self.catalog.observe(work.draft.mode,target,snap)
                except Exception:
                    stop.set();raise
            if work.action=='asset-collect':
                results,report=run_inventory(targets,options,stop,callback,self.store.root/'reports',demo=work.draft.mode=='demo')
            else:
                results,report=run_batch(targets,images,options,'precheck' if work.action=='read' else work.action,stop,callback,
                                         folder=self.store.root/'reports',client_class=DemoClient if work.draft.mode=='demo' else UpgradeClient)
            failed=any(r.get('error') for r in results)
            state='stopped' if stop.is_set() else 'failed' if failed else 'completed'
            if work.action=='precheck' and not stop.is_set():
                gate.commit(work.scope,results)
                with self.lock:self.prechecks[(work.owner,work.scope)]=gate
            self.store.update_job(work.id,state=state,finished=time.time(),results=results,report=str(report))
            self.store.event(work.id,{'event':'任务结束','data':{'state':state}})
        except Exception as exc:
            reason=str(exc) if isinstance(exc,(ValueError,BmcError)) else '内部错误：'+type(exc).__name__
            if work.options.password:reason=reason.replace(work.options.password,'[REDACTED]')
            self.store.update_job(work.id,state='needs_review',finished=time.time(),error=reason)
            self.store.event(work.id,{'event':'任务停止并待核验','data':{'reason':reason}})
        finally:
            self.store.audit(work.owner,'job_finished',job=work.id,mode=work.draft.mode)
