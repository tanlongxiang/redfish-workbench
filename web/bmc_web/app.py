from __future__ import annotations
import asyncio
import json
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Depends, Request, HTTPException, UploadFile, File, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .models import Draft,JobRequest,PlanRequest,ConfirmRequest,TextRequest,LoginRequest,Workspace,AssetRegister,AssetEdit
from .service import Runtime,Settings
from .security import Sessions
from .asset_catalog import import_ledger
from .bridge import import_targets,parse_targets,COMPONENTS,BmcError,write_xlsx,FIRMWARE,normalize_target

MAX_BODY=66*1024*1024
MAX_IMAGE=64*1024*1024

class WebGuard:
    def __init__(self,app,origin):self.app,self.origin=app,origin
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=dict(scope.get('headers',[]));method=scope['method']
        if method not in ('GET','HEAD') and headers.get(b'origin',b'').decode('ascii',errors='replace')!=self.origin:
            return await JSONResponse({'detail':'请求来源不受信任'},status_code=403)(scope,receive,send)
        try:length=int(headers.get(b'content-length',b'0'))
        except ValueError:length=MAX_BODY+1
        if length>MAX_BODY:
            return await JSONResponse({'detail':'请求超过 66 MiB 上限'},status_code=413)(scope,receive,send)
        received=0
        async def limited_receive():
            nonlocal received
            message=await receive()
            received+=len(message.get('body',b''))
            if received>MAX_BODY:raise HTTPException(413,'请求超过大小上限')
            return message
        async def secure_send(message):
            if message['type']=='http.response.start':
                extra=[(b'x-content-type-options',b'nosniff'),(b'x-frame-options',b'DENY'),
                       (b'referrer-policy',b'no-referrer'),
                       (b'content-security-policy',b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")]
                if scope['path'].startswith('/api') or scope['path']=='/':extra.append((b'cache-control',b'no-store'))
                message['headers']=list(message.get('headers',[]))+extra
            await send(message)
        return await self.app(scope,limited_receive,secure_send)


def create_app(settings:Settings):
    runtime=Runtime(settings);sessions=Sessions(settings.data_dir)
    @asynccontextmanager
    async def lifespan(app):
        runtime.start()
        try:yield
        finally:
            app.state.redfish.close()
            await asyncio.to_thread(runtime.close)
    app=FastAPI(title='BMC Local Workbench',version='0.6.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.runtime=runtime;app.state.sessions=sessions;app.state.settings=settings
    app.add_middleware(WebGuard,origin=settings.origin)
    origin_host=urlsplit(settings.origin).hostname or '127.0.0.1'
    allowed_hosts=['127.0.0.1','localhost','[::1]',origin_host]
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=list(dict.fromkeys(allowed_hosts)))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        # Pydantic's default errors can echo a submitted secret. Never return input values.
        return JSONResponse({'detail':'请求字段不正确','errors':[{'field':'.'.join(map(str,e['loc'])),'message':e['msg']} for e in exc.errors()]},status_code=422)
    @app.exception_handler(ValueError)
    @app.exception_handler(BmcError)
    async def user_error(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=400)

    def authenticated(request:Request):
        session=sessions.get(request.cookies.get('bmc_session'))
        if not session:raise HTTPException(401,'请先登录本地工作台')
        if request.method not in ('GET','HEAD'):
            import hmac
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token','').encode('utf-8'),session['csrf'].encode('utf-8')):raise HTTPException(403,'请求校验失败，请刷新登录')
        return session

    @app.post('/api/session')
    def login(body:LoginRequest,request:Request):
        token,session=sessions.login(body.key,body.bootstrap,request.client.host)
        runtime.store.audit(session['id'],'login')
        response=JSONResponse({'csrf':session['csrf'],'operator':'本机操作员','expires':session['expires']})
        response.set_cookie('bmc_session',token,httponly=True,samesite='strict',secure=False,max_age=8*3600,path='/')
        return response
    @app.get('/api/session')
    def current_session(session=Depends(authenticated)):
        return {'csrf':session['csrf'],'operator':'本机操作员','expires':session['expires']}
    @app.delete('/api/session')
    def logout(request:Request,session=Depends(authenticated)):
        app.state.redfish.forget(session['id'])
        sessions.logout(request.cookies.get('bmc_session'));runtime.store.audit(session['id'],'logout')
        response=JSONResponse({'ok':True});response.delete_cookie('bmc_session');return response
    @app.get('/api/config')
    def config(session=Depends(authenticated)):
        user,password=settings.credential_provider()
        return {'version':'0.6.0','mode':'demo' if settings.test_simulation else 'live','components':COMPONENTS,'confirmation_required':True,
                'credentials_available':bool(user and password),'credential_source':'仓库根目录.env或环境变量（仅后端读取）',
                'precheck_ttl_seconds':120,'limits':{'image_mib':64,'workers':4},'local_only':True}
    @app.post('/api/targets/parse')
    def parse(body:TextRequest,session=Depends(authenticated)):
        return {'targets':[{'name':t.name,'address':t.address} for t in parse_targets(body.text)]}
    @app.post('/api/targets/import')
    async def import_file(file:UploadFile=File(...),session=Depends(authenticated)):
        suffix=Path(file.filename or '').suffix.lower()
        if suffix not in ('.csv','.xlsx'):raise ValueError('仅支持 CSV / XLSX')
        imports=runtime.store.root/'imports';imports.mkdir(exist_ok=True,mode=0o700)
        with tempfile.TemporaryDirectory(dir=imports,prefix='input_') as folder:
            path=Path(folder)/('nodes'+suffix)
            total=0
            with path.open('xb') as f:
                while chunk:=await file.read(1024*1024):
                    total+=len(chunk)
                    if total>40*1024*1024:raise HTTPException(413,'清单超过 40 MiB')
                    f.write(chunk)
            targets=await asyncio.to_thread(import_targets,path)
        runtime.store.audit(session['id'],'targets_imported',count=len(targets))
        return {'targets':[{'name':t.name,'address':t.address} for t in targets]}
    @app.get('/api/images')
    def images(session=Depends(authenticated)):
        return [row for row in runtime.store.images() if settings.test_simulation or not row['demo']]
    @app.post('/api/images')
    async def upload(file:UploadFile=File(...),session=Depends(authenticated)):
        import re
        name=file.filename or ''
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',name) or Path(name).suffix.lower() not in ('.svf','.tar') or 'LT' not in name:
            raise ValueError('CPLD 镜像须为含 LT 的 .svf/.tar，使用 ASCII 文件名；不接受 BIOS/BMC 包')
        folder=runtime.store.root/'images'/uuid.uuid4().hex;folder.mkdir(parents=True,mode=0o700)
        path=folder/name
        size=0
        try:
            with path.open('xb') as f:
                os.chmod(path,0o600)
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk)
                    if size>MAX_IMAGE:raise HTTPException(413,'镜像超过 64 MiB')
                    f.write(chunk)
            row=await asyncio.to_thread(runtime.add_image,path,name)
        except Exception:
            # Only this request's new, partially written file is removed.
            path.unlink(missing_ok=True);folder.rmdir();raise
        runtime.store.audit(session['id'],'image_uploaded_to_local_server',image=row['id'],sha256=row['sha256'],size=size)
        return row
    if settings.test_simulation:
        @app.post('/api/demo')
        def demo(session=Depends(authenticated)):
            runtime.store.audit(session['id'],'test_fixture_loaded');return runtime.demo()
    @app.post('/api/workspaces/export')
    def export_workspace(draft:Draft,session=Depends(authenticated)):
        runtime.targets(draft);images=runtime.images(draft)
        data=draft.model_dump();data['canary_confirmed']=False;data['revision']=''
        return {'format':'bmc-web-workspace-v1','draft':data,
                'images':[{'component':i.component,'sha256':i.sha256,'filename':i.path.name} for i in images]}
    @app.post('/api/workspaces/restore')
    def restore_workspace(body:Workspace,session=Depends(authenticated)):
        available=runtime.store.images();data=body.draft.model_dump()
        metadata={i.component:i for i in body.images}
        for choice in data['images']:
            meta=metadata.get(choice['component'])
            matching=next((i for i in available if meta and i['sha256']==meta.sha256),None)
            if not matching:raise ValueError('配置中的镜像尚未上传或 SHA256 不匹配，请先上传对应文件')
            choice['image_id']=matching['id']
        data['canary_confirmed']=False;data['revision']=uuid.uuid4().hex
        draft=Draft.model_validate(data);runtime.targets(draft);runtime.images(draft)
        return {'draft':data}
    @app.post('/api/readiness')
    def readiness(draft:Draft,session=Depends(authenticated)):
        return runtime.readiness(session['id'],draft)
    @app.post('/api/jobs')
    def submit(body:JobRequest,session=Depends(authenticated)):
        return runtime.submit(session['id'],body.action,body.draft)
    @app.get('/api/jobs')
    def jobs(session=Depends(authenticated)):
        mode='demo' if settings.test_simulation else 'live'
        return [job for job in runtime.store.jobs() if job['mode']==mode]
    @app.get('/api/jobs/{job}')
    def job(job:str,session=Depends(authenticated)):return runtime.public_job(job)
    @app.post('/api/jobs/{job}/stop')
    def stop(job:str,session=Depends(authenticated)):return runtime.stop(session['id'],job)
    @app.post('/api/plans')
    def plan(body:PlanRequest,session=Depends(authenticated)):return runtime.plan(session['id'],body.action,body.draft)
    @app.post('/api/plans/{pid}/confirm')
    def confirm(pid:str,body:ConfirmRequest,session=Depends(authenticated)):
        return runtime.confirm(session['id'],pid,body.phrase)
    @app.get('/api/audit')
    def audit(session=Depends(authenticated)):
        with runtime.store.connect() as db:
            return [{**dict(r),'data':json.loads(r['data'])} for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 300')]
    @app.get('/api/assets')
    def assets(mode:str='live',session=Depends(authenticated)):
        runtime.validate_mode(mode)
        return runtime.store.assets(mode)
    def catalog_filters(mode:str='live',q:str=Query('',max_length=200),model:str=Query('',max_length=200),power:str=Query('',max_length=40),status:str=Query('',max_length=30),sn:str=Query('',max_length=30),lifecycle:str=Query('',max_length=30),firmware_field:str=Query('',max_length=40),firmware_version:str=Query('',max_length=200),version_source:str='observed',page:int=1,page_size:int=50):
        runtime.validate_mode(mode)
        return dict(mode=mode,q=q,model=model,power=power,status=status,sn=sn,lifecycle=lifecycle,firmware_field=firmware_field,firmware_version=firmware_version,version_source=version_source,page=page,page_size=page_size)
    @app.get('/api/asset-records')
    def catalog_query(filters=Depends(catalog_filters),session=Depends(authenticated)):
        return runtime.catalog.query(**filters)
    @app.post('/api/asset-records/register')
    def catalog_register(body:AssetRegister,session=Depends(authenticated)):
        runtime.validate_mode(body.mode)
        from firmware_batch import validate_targets
        targets=validate_targets([t for n in body.targets for t in parse_targets(n.name+' '+n.address)])
        count=runtime.catalog.register(body.mode,[{'name':t.name,'address':t.address} for t in targets])
        runtime.store.audit(session['id'],'asset_records_registered',mode=body.mode,count=count)
        return {'count':count}
    @app.post('/api/asset-records/import')
    async def catalog_import(mode:str='live',file:UploadFile=File(...),session=Depends(authenticated)):
        runtime.validate_mode(mode)
        suffix=Path(file.filename or '').suffix.lower()
        if suffix not in ('.csv','.xlsx'):raise ValueError('仅支持 CSV / XLSX')
        imports=runtime.store.root/'imports';imports.mkdir(exist_ok=True,mode=0o700)
        with tempfile.TemporaryDirectory(dir=imports,prefix='ledger_') as folder:
            path=Path(folder)/('ledger'+suffix);size=0
            with path.open('xb') as f:
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk)
                    if size>40*1024*1024:raise HTTPException(413,'清单超过40 MiB')
                    f.write(chunk)
            rows=await asyncio.to_thread(import_ledger,path)
        count=await asyncio.to_thread(runtime.catalog.register,mode,rows)
        runtime.store.audit(session['id'],'asset_ledger_imported',mode=mode,count=count)
        return {'count':count}
    @app.put('/api/asset-records/metadata')
    def catalog_edit(body:AssetEdit,session=Depends(authenticated)):
        runtime.validate_mode(body.mode)
        value=runtime.catalog.edit(body.mode,normalize_target(body.address),body.model_dump(exclude={'mode','address'}))
        runtime.store.audit(session['id'],'asset_metadata_updated',mode=body.mode,address=value['address'])
        return value
    @app.get('/api/asset-records/export')
    def catalog_export(filters=Depends(catalog_filters),session=Depends(authenticated)):
        from starlette.background import BackgroundTask
        result=runtime.catalog.query(**filters,all_rows=True)
        folder=runtime.store.root/'exports';folder.mkdir(exist_ok=True,mode=0o700)
        path=folder/(uuid.uuid4().hex+'.xlsx')
        headers=['节点名','BMC_IP','模式','机型','电源','参考SN','现场SystemSN','ChassisSN','SN核对','位置','标签','资产状态','备注','最近采集状态','最近尝试时间','CPU数量','CPU型号','内存GiB']+[n+'（现场历史）' for n,_ in FIRMWARE]+[n+'（导入参考）' for n,_ in FIRMWARE]+['本次有效固件数','历史有效固件数','说明']
        rows=[]
        for item in result['items']:
            meta,data=item['metadata'],item['data']
            rows.append([item['name'],item['address'],item['mode'],item['model'],item['power'],meta['declared_sn'],data.get('system_sn',''),data.get('chassis_sn',''),item['sn_state'],meta['location'],meta['tags'],meta['lifecycle'],meta['notes'],item['collection_state'],data.get('last_attempt',''),data.get('cpu_count'),data.get('cpu_model',''),data.get('memory_gib')]+[data.get('firmware',{}).get(n,'') for n,_ in FIRMWARE]+[data.get('reference_firmware',{}).get(n,'') for n,_ in FIRMWARE]+[item['latest_count'],item['firmware_count'],'；'.join(data.get('notes',[])) or data.get('last_error','')])
        write_xlsx(str(path),headers,rows)
        runtime.store.audit(session['id'],'asset_ledger_exported',mode=filters['mode'],count=len(rows))
        return FileResponse(path,filename='资产台账.xlsx',background=BackgroundTask(path.unlink,missing_ok=True))
    @app.get('/api/jobs/{job}/events')
    async def events(job:str,request:Request,after:int=0,session=Depends(authenticated)):
        runtime.store.job(job)
        try:after=max(after,int(request.headers.get('last-event-id','0')))
        except ValueError:pass
        async def stream():
            cursor=after;heartbeat=0
            while not await request.is_disconnected():
                rows=await asyncio.to_thread(runtime.store.events,job,cursor)
                for row in rows:
                    cursor=row['id'];yield f'id: {cursor}\nevent: record\ndata: {json.dumps(row,ensure_ascii=False)}\n\n'
                state=(await asyncio.to_thread(runtime.store.job,job))['state']
                if state not in ('queued','running') and len(rows)<300:
                    yield 'event: done\ndata: '+json.dumps({'state':state})+'\n\n';break
                heartbeat+=1
                if heartbeat%20==0:yield ': keepalive\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
    @app.get('/api/jobs/{job}/files/{kind}')
    def report(job:str,kind:str,session=Depends(authenticated)):
        row=runtime.store.job(job)
        names={'report':'维护结果.xlsx','evidence':'events.jsonl','results':'results.json'}
        if kind not in names or not row['report']:raise HTTPException(404,'报告尚未生成')
        path=(Path(row['report'])/names[kind]).resolve()
        if not path.is_relative_to((runtime.store.root/'reports').resolve()) or not path.is_file():raise HTTPException(404,'报告不存在')
        return FileResponse(path,filename=names[kind],media_type='application/octet-stream')
    from .redfish_service import install_routes
    install_routes(app,runtime,authenticated)

    @app.api_route('/api/{unknown:path}',methods=['GET','POST','PUT','PATCH','DELETE'])
    def unknown(unknown:str):raise HTTPException(404,'接口不存在')
    dist=Path(__file__).resolve().parents[1]/'frontend'/'dist'
    if dist.exists():app.mount('/',StaticFiles(directory=dist,html=True),name='frontend')
    else:
        @app.get('/')
        def missing():return HTMLResponse('<!doctype html><meta charset="utf-8"><h1>前端尚未构建</h1><p>请在 frontend 目录运行 npm ci 和 npm run build。</p>',status_code=503)
    return app
