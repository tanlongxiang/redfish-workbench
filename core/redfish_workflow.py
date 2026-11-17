"""Bounded multi-node tasks. Only operator-confirmed plans may mutate a BMC."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from dataclasses import dataclass
import copy,hashlib,hmac,json,re,secrets,tempfile,threading,time,os
from redfish_session import ManagedSession,RedfishHTTPError
from redfish_ops import contract,fingerprint,IMPACTS
from bmc_client import BmcError
from xlsx_io import write_xlsx

MODELS={'R6250A0','X7450A0'}
_SECRET_SCOPE_KEY=secrets.token_bytes(32)
@dataclass(repr=False)
class Approval:
    scope:str
    expires:float
    consumed:bool=False
    def consume(self,scope):
        if self.consumed or self.expires<time.time() or self.scope!=scope:raise BmcError('本次确认已使用、过期或范围改变')
        self.consumed=True

def prepare(targets,request):
    r=copy.deepcopy(request);op,params,payload=contract(r['operation'],r.get('params'),r.get('payload'))
    r.update(params=params,payload=payload)
    if not targets or len(targets)>2000:raise BmcError('任务范围必须为1至2000个明确节点')
    if len({t.address for t in targets})!=len(targets) or len({t.name for t in targets})!=len(targets):raise BmcError('节点名或地址重复')
    if not 1<=r.get('workers',2)<=4 or not 1<=r.get('timeout',20)<=120:raise BmcError('并发或超时不在允许范围')
    if not 1<=r.get('task_timeout',900)<=3600:raise BmcError('任务观察期限须为1至3600秒')
    image=r.get('image')
    if op['id'] in {'3.7.5','3.7.6'}:
        if not image:raise BmcError('需要本次本地镜像')
        p=Path(image)
        if p.is_symlink() or not p.is_file() or not 0<p.stat().st_size<=64*1024*1024:raise BmcError('镜像必须是1字节至64MiB普通文件')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',p.name) or p.suffix.lower() not in {'.ima','.bin','.rom','.jed'}:raise BmcError('镜像需使用ASCII文件名及.ima/.bin/.rom/.jed后缀')
        r['image']=str(p.resolve());r['image_name']=p.name;r['image_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    elif image:raise BmcError('此操作不接受镜像')
    if op['id']=='3.7.6':
        component=r['payload']['Targets'][0].rsplit('/',1)[-1]
        suffix=Path(r['image']).suffix.lower()
        accepted={'.ima'} if component in {'ActiveBMC','BackupBMC'} else {'.jed'} if component=='Cpld' else {'.bin','.rom'} if component=='Bios' else {'.bin'}
        if suffix not in accepted:raise BmcError('镜像后缀与本次固件组件不匹配；后缀正确也不代表兼容性已确认')
    if op['method']!='GET' and len(targets)>1 and not r.get('canary_confirmed',False):raise BmcError('多台变更须明确已完成同机型同参数单台验证；不默认批量执行')
    if op['method']!='GET' and not r.get('expected_model'):raise BmcError('变更计划必须填写准确机型')
    public={k:v for k,v in r.items() if k not in {'image','bios_passwords'}}
    public.update(title=op['title'],method=op['method'],path=op['path'],impact=IMPACTS.get(op['id'],'修改设备状态或配置' if op['method']!='GET' else '仅读取'),targets=[{'name':t.name,'address':t.address} for t in targets],power_process='不自动切换电源；固件上传/升级要求读取到Off' if op['id'] in {'3.7.5','3.7.6'} else '不执行未列出的电源动作',expected_version=r.get('expected_version') or '未提供：只保留实际读回版本，不声称目标版本一致')
    seal=''
    if op['id']=='3.3.22':
        public['sensitive_parameters']='旧/新BIOS密码仅在本次任务内存，不进入计划、日志或报告'
        values=r.get('bios_passwords')
        if values is not None:
            if not isinstance(values,dict) or set(values)!={'old','new'} or not all(isinstance(v,str) and len(v)<=256 for v in values.values()) or not values['new']:raise BmcError('BIOS密码需使用专用秘密字段，新密码不能为空')
            seal=hmac.new(_SECRET_SCOPE_KEY,json.dumps(values,sort_keys=True).encode(),hashlib.sha256).hexdigest()
    elif r.get('bios_passwords') is not None:raise BmcError('此操作不接受BIOS密码字段')
    return op,r,public,fingerprint({'plan':public,'private_binding':seal})

def _object(client,path):
    data,headers,status=client.get(path)
    if not isinstance(data,dict):raise BmcError('资源未返回对象')
    return data,headers,status

def _member(client,collection,identifier):
    body=read_resource(client,collection)
    if not isinstance(body,dict):raise BmcError('资源集合格式不正确')
    members=body.get('Members',[])
    if not isinstance(members,list):raise BmcError('资源集合格式不正确')
    paths=[]
    for member in members:
        if isinstance(member,dict) and isinstance(member.get('@odata.id'),str):
            path=client.path(member['@odata.id'])
            if not path.startswith(collection.rstrip('/')+'/') or '?' in path:raise BmcError('资源成员离开当前集合')
            paths.append(path)
    if identifier:
        wanted=collection.rstrip('/')+'/'+identifier
        if wanted not in paths:raise BmcError('所选资源ID不在本次集合返回中')
        return wanted
    if len(paths)!=1 or body.get('Members@odata.nextLink') or body.get('@odata.nextLink'):raise BmcError('系统/机箱不唯一或集合有分页，请明确选择资源ID')
    return paths[0]

def resolve(client,op,params):
    root,_,_=_object(client,'/redfish/v1/');path=op['path'];system=None
    for name,collection in [('system_id','Systems'),('chassis_id','Chassis')]:
        if '{'+name+'}' not in path:continue
        link=root.get(collection,{})
        collection_path=client.path(link.get('@odata.id','/redfish/v1/'+collection))
        if collection_path!='/redfish/v1/'+collection:raise BmcError('非本适配器的资源根路径，请单独适配')
        base=_member(client,collection_path,params.get(name))
        path=path.replace('/redfish/v1/'+collection+'/{'+name+'}',base)
        if name=='system_id':system=base
    for key,value in params.items():path=path.replace('{'+key+'}',value)
    if '{' in path:raise BmcError('未填写全部资源ID')
    if params.get('raid_storage_id'):
        _member(client,system+'/LogServices',params['raid_storage_id'])
    return client.path(path),system

def read_resource(client,path):
    body,_,_=client.get(path)
    if not isinstance(body,dict) or 'Members' not in body:return body
    if not isinstance(body['Members'],list):raise BmcError('集合成员格式不正确')
    result=copy.deepcopy(body);members=[];seen_pages=set();seen_members=set();current=path
    for _ in range(64):
        if current in seen_pages:raise BmcError('分页循环，读取不完整')
        seen_pages.add(current)
        for member in body.get('Members',[]):
            if not isinstance(member,dict) or not isinstance(member.get('@odata.id'),str):raise BmcError('集合成员缺少资源链接')
            uri=client.path(member['@odata.id'])
            if not uri.startswith(path.split('?',1)[0].rstrip('/')+'/') or '?' in uri:raise BmcError('成员离开当前资源集合')
            if uri in seen_members:raise BmcError('重复集合成员，读取不完整')
            seen_members.add(uri)
            if '$expand=.' in path and set(member)=={'@odata.id'}:member,_,_=_object(client,uri)
            members.append(member)
            if len(members)>4096:raise BmcError('集合超过4096成员，读取已停止')
        next_link=body.get('Members@odata.nextLink') or body.get('@odata.nextLink')
        if not next_link:break
        current=client.path(next_link)
        if current.split('?',1)[0]!=path.split('?',1)[0]:raise BmcError('分页离开当前资源集合')
        body,_,_=_object(client,current)
        if not isinstance(body.get('Members'),list):raise BmcError('分页缺少成员列表')
    else:raise BmcError('集合超过64页，读取不完整')
    expected=result.get('Members@odata.count')
    # G50 documents string counts for Systems/Chassis. Accept only bounded
    # ASCII digits; retain strict completeness checks and reject bool/float.
    if isinstance(expected,str) and re.fullmatch(r'[0-9]{1,10}',expected):expected=int(expected)
    if expected is not None and (type(expected) is not int or expected!=len(members)):raise BmcError('集合声明成员数与读取数量不一致')
    result['Members']=members;result.pop('Members@odata.nextLink',None);result.pop('@odata.nextLink',None)
    return result

def _mutate(client,method,path,payload=None,**kwargs):
    try:return client._http(method,path,payload,**kwargs)
    except BmcError:raise BmcError('写请求失败或结果未知；禁止重发、续刷、回滚或自动切电，需人工核验') from None

def _upload(client,path,request):
    p=Path(request['image']);body=p.read_bytes()
    if hashlib.sha256(body).hexdigest()!=request['image_sha256']:raise BmcError('镜像在确认后发生变化，未上传')
    boundary='bmc_'+secrets.token_hex(16)
    data=(f'--{boundary}\r\nContent-Disposition: form-data; name="fwimage"; filename="{p.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+body+f'\r\n--{boundary}--\r\n'.encode())
    return _mutate(client,'POST',path,data,headers={'Content-Type':'multipart/form-data; boundary='+boundary})

def _subset(expected,actual):
    if isinstance(expected,dict):return isinstance(actual,dict) and all(k in actual and _subset(v,actual[k]) for k,v in expected.items())
    if isinstance(expected,list):return expected==actual
    return type(expected) is type(actual) and expected==actual

def run_node(target,op,request,credentials,stop,*,client_factory=ManagedSession,attachment_dir=None,write_deadline=None):
    client=client_factory(target.address,request.get('timeout',20),request.get('insecure',False))
    row={'node':target.name,'address':target.address,'operation':op['id'],'title':op['title'],'mode':'真实只读' if op['method']=='GET' else '已确认变更','state':'failed','observed':time.time(),'status':'未执行','session_cleanup':'not_created'}
    try:
        if stop.is_set():row.update(state='stopped',status='已停止，未创建会话');return row
        client.login(*credentials)
        row['session_timeout_seconds']=client.session_timeout
        path,system_uri=resolve(client,op,request['params']);row['resource']=path
        if stop.is_set():row.update(state='stopped',status='已停止，未执行业务请求');return row
        if op['method']=='GET':
            data=read_resource(client,path);row.update(state='completed',status='只读完成',data=client.safe(data));return row
        system_uri=system_uri or _member(client,'/redfish/v1/Systems',request['params'].get('system_id'))
        system,_,_=_object(client,system_uri)
        if system.get('Model')!=request['expected_model'] or system.get('Model') not in MODELS:raise BmcError('机型不匹配或不属于已建文档适配的R6250A0/X7450A0；未写入')
        row['model']=system.get('Model');row['power']=system.get('PowerState')
        if op['id'] in {'3.7.5','3.7.6'} and system.get('PowerState')!='Off':raise BmcError('固件写入前未确认主机Off；不会自动关机')
        headers={};before=None
        if op['method'] in {'PATCH','DELETE'}:
            before,h,_=_object(client,path);etag=next((v for k,v in h.items() if k.lower()=='etag'),None)
            if not etag:raise BmcError('未读取到ETag，拒绝无条件覆盖或删除')
            headers['If-Match']=etag
        if '/Actions/' in path:
            parent,action=path.split('/Actions/',1);resource,_,_=_object(client,parent)
            advertised=resource.get('Actions',{}).get('#'+action,{})
            if not isinstance(advertised,dict) or client.path(advertised.get('target',''))!=path:raise BmcError('设备未公布本次Action，拒绝猜测执行')
            if op['id']=='3.3.23':
                allowed=advertised.get('ResetType@Redfish.AllowableValues')
                if not isinstance(allowed,list) or request['payload']['ResetType'] not in allowed:raise BmcError('设备未声明允许本次电源动作')
        if stop.is_set():row.update(state='stopped',status='已停止，未发写请求');return row
        if write_deadline is None or time.time()>write_deadline:raise BmcError('本次确认在首个写请求前已过期，未写入')
        if op['id']=='3.7.5':
            data,_,_=_upload(client,path,request);row.update(state='completed',status='上传请求已接受；未激活',data=client.safe(data));return row
        if op['id']=='3.7.6':
            component=request['payload']['Targets'][0]
            inventory,_,_=_object(client,component)
            row['previous_version']=inventory.get('Version')
            baseline=None
            try:baseline,_,_=_object(client,'/redfish/v1/TaskService/Tasks/UpdateFirmware')
            except RedfishHTTPError as exc:
                if exc.status!=404:raise
            if baseline and baseline.get('TaskState') not in {'Completed','Exception','Killed','Cancelled'}:raise BmcError('设备存在未结束的升级任务，未上传')
            _upload(client,'/redfish/v1/UpdateService/FirmwareInventory',request)
            power,_,_=_object(client,system_uri)
            if power.get('PowerState')!='Off':raise BmcError('上传后主机非Off；已停止激活，需核验已上传文件')
            if stop.is_set():raise BmcError('上传后请求停止；未激活，不自动清除或续刷')
            payload={**request['payload'],'ImageURI':'/tmp/'+request['image_name']}
            data,h,_=_mutate(client,'POST',path,payload)
            uri=(next((v for k,v in h.items() if k.lower()=='location'),None) or (data.get('@odata.id') if isinstance(data,dict) else None))
            if not uri:raise BmcError('升级请求已发送但未取得任务URI；结果待核验')
            uri=client.path(uri)
            if not re.fullmatch(r'/redfish/v1/TaskService/Tasks/[A-Za-z0-9_-]+',uri):raise BmcError('返回的任务URI不在允许范围；结果待核验')
            row['bmc_task']=uri;deadline=time.monotonic()+request.get('task_timeout',900)
            while True:
                task,_,_=_object(client,uri)
                if uri.endswith('/UpdateFirmware') and (not task.get('StartTime') or (baseline and task.get('StartTime')==baseline.get('StartTime'))):raise BmcError('固定任务URI缺少本次开始时间或仍为旧任务，不能认定本次成功')
                row['task']=client.safe(task)
                if task.get('TaskState')=='Completed':break
                if task.get('TaskState') in {'Exception','Killed','Cancelled','Interrupted'}:raise BmcError('BMC任务异常结束；未重试或回滚')
                if time.monotonic()>=deadline:raise BmcError('任务观察超时，设备任务可能仍在运行；未重发或切电')
                # Stop never cancels a flash that has already been accepted.
                threading.Event().wait(min(2,max(0,deadline-time.monotonic())))
            actual,_,_=_object(client,component);row['actual_version']=actual.get('Version')
            expected=request.get('expected_version')
            if expected and actual.get('Version')!=expected:raise BmcError('任务结束但实际版本不匹配，需人工核验')
            row.update(state='completed',status='BMC任务完成，版本已读回'+('并匹配预期' if expected else '；未提供预期版本，不判定一致'),data=client.safe(actual));return row
        payload=request['payload'] or None
        if op['id']=='3.3.22':
            private=request['bios_passwords'];client.secrets.extend([private['old'],private['new']])
            payload={**request['payload'],'OldPassword':private['old'],'NewPassword':private['new']}
        data,_,_=_mutate(client,op['method'],path,payload,headers=headers,binary=op['id'] in {'3.3.31','3.3.37'})
        if isinstance(data,bytes):
            if attachment_dir:
                attachment=Path(attachment_dir)/(hashlib.sha256(target.address.encode()).hexdigest()[:16]+'.bin')
                with attachment.open('xb') as f:f.write(data)
                row['attachment']=attachment.name
            row.update(state='completed',status='导出响应已接收，文件包含内部信息');return row
        row['data']=client.safe(data)
        if op['method']=='PATCH':
            actual,_,_=_object(client,path);row['readback']=client.safe(actual)
            if not _subset(request['payload'],actual):raise BmcError('写请求已接受，但读回与计划不一致，需核验生效时机')
            row.update(state='completed',status='设置已提交并读回，不自动重启或切电')
        elif op['id']=='3.3.23':
            expected={'On':'On','ForceOff':'Off','GracefulShutdown':'Off'}.get(request['payload']['ResetType'])
            actual,_,_=_object(client,system_uri);row['readback']=client.safe(actual)
            if not expected or actual.get('PowerState')!=expected:raise BmcError('电源请求已受理，实际效果尚未确认；不重复操作')
            row.update(state='completed',status='电源动作已提交并读回目标状态')
        elif op['method']=='DELETE':
            try:client.get(path)
            except RedfishHTTPError as exc:
                if exc.status==404:row.update(state='completed',status='删除后已确认资源不存在');return row
                raise
            raise BmcError('删除后资源仍存在，需人工核验')
        else:row.update(state='needs_review',status='请求已受理，操作效果需单独核验')
    except Exception as exc:
        row.update(state='needs_review' if op['method']!='GET' else 'failed',status='未完成/待核验',error=client.safe(str(exc)) if isinstance(exc,(BmcError,ValueError)) else '内部错误：'+type(exc).__name__)
    finally:
        # Redact before dropping secret references; do not return raw headers.
        clean=client.safe(row);row.clear();row.update(clean)
        row['session_cleanup']=client.close()
        if row['session_cleanup']=='unconfirmed':row.update(state='needs_review',cleanup_warning='Session注销未确认；不能声称已安全退出')
    return row

def _locked_node(target,*args,**kwargs):
    from firmware_batch import NodeLock
    try:
        with NodeLock(target.address):return run_node(target,*args,**kwargs)
    except BmcError:
        return {'node':target.name,'address':target.address,'operation':args[0]['id'],'title':args[0]['title'],'mode':'未连接','observed':time.time(),'state':'failed','status':'节点已锁定，未连接或写入','session_cleanup':'not_created','error':'已有维护任务或遗留锁，需核验；未抢占锁'}

def run_tasks(targets,request,credentials,output,*,approval=None,stop=None,callback=None,client_factory=ManagedSession):
    op,request,public,scope=prepare(targets,request)
    if op['id']=='3.3.22' and not request.get('bios_passwords'):raise BmcError('BIOS密码变更缺少专用秘密字段，未连接设备')
    if op['method']!='GET':
        if not isinstance(approval,Approval):raise BmcError('变更必须持有本次范围的一次性确认')
        approval.consume(scope)
    stop=stop or threading.Event();output=Path(output);output.mkdir(parents=True,exist_ok=True)
    folder=Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d_%H%M%S')+'_redfish_',dir=output))
    with (folder/'plan.json').open('x',encoding='utf-8') as f:json.dump(public,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    results=[None]*len(targets)
    journal=(folder/'events.jsonl').open('x',encoding='utf-8')
    with journal,ThreadPoolExecutor(max_workers=request.get('workers',2)) as pool:
        futures={pool.submit(_locked_node,t,op,request,credentials,stop,client_factory=client_factory,attachment_dir=folder,write_deadline=approval.expires if approval else None):i for i,t in enumerate(targets)}
        for future in as_completed(futures):
            i=futures[future];row=future.result();results[i]=row
            journal.write(json.dumps(row,ensure_ascii=False)+'\n');journal.flush();os.fsync(journal.fileno())
            if callback:callback(targets[i],row)
    for name,value in [('results.json',results)]:
        with (folder/name).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')
    write_xlsx(str(folder/'维护结果.xlsx'),['节点名','BMC_IP','操作','结果','状态','Session清理','说明'],[[r['node'],r['address'],r['title'],r['status'],r['state'],r['session_cleanup'],r.get('error') or r.get('cleanup_warning','')] for r in results])
    return results,folder
