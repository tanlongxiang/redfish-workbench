"""BMC CLI: explicit read scope, separate writes, standard library only."""
from pathlib import Path
from dataclasses import replace
import argparse
import copy
import getpass
import hashlib
import json
import os
import secrets
import signal
import sys
import tempfile
import threading
import time

import firmware_batch as engine
from firmware_batch import COMPONENTS, Image, Options, WriteApproval, Target, UpgradeClient, run_batch, validate_targets
from firmware_demo import DemoClient, DEMO_MARKER
from firmware_readiness import ReadinessGate
from asset_inventory import run_inventory
from asset_catalog import Catalog, import_ledger, FIELDS
from bmc_client import BmcError, normalize_target
from local_config import read_env
from . import __version__
from .targets import load_targets, parse_text
from .store import Store
from .reports import json_text,write_json,export_ledger

POWER={'off':'GracefulShutdown','force-off':'ForceOff','on':'On'}

class Parser(argparse.ArgumentParser):
    def error(self,message):
        # argparse normally echoes unknown argument values, including --password.
        self.print_usage(sys.stderr)
        self.exit(2,'参数无效；请使用 --help 查看用法。不会回显参数值；密码请使用交互输入或环境变量。\n')

def default_state():
    if os.name=='nt': return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'RedfishWorkbench'/'cli'
    return Path.home()/'.local/state/redfish-workbench/cli'

def connection_flags(p):
    p.add_argument('--live',action='store_true',help='显式允许连接下列目标；不会自动扩大范围')
    p.add_argument('--username',help='BMC账号；也可通过 BMC_USERNAME 环境变量指定')
    p.add_argument('--env-file',type=Path,help='仅显式指定时读取凭据文件；从不自动加载工作区 .env')
    p.add_argument('--password-env',default='BMC_PASSWORD',help='密码所在的环境变量名（不是密码值）')
    p.add_argument('--auth',choices=('basic','session'),default='basic')
    p.add_argument('--insecure',action='store_true',help='HTTPS仍加密，但跳过TLS证书校验')
    p.add_argument('--timeout',type=int,default=20)
    p.add_argument('--workers',type=int,choices=range(1,5),default=2,metavar='1..4')
    p.add_argument('--output',type=Path,default=Path('bmc_reports'),help='新增报告所在目录，不覆盖旧报告')
    p.add_argument('--model',default='',help='电源写操作/CPLD操作要求的精确机型')
    p.add_argument('--flash-timeout',type=int,default=900)
    p.add_argument('--canary-confirmed',action='store_true',help='操作者确认同机型同镜像已完成单台现场验证（多台CPLD写入必需）')

def target_flags(p):
    p.add_argument('--target',action='append',default=[],help='IP、名称=IP 或“名称 IP”，可重复；支持IPv6')
    p.add_argument('--targets',type=Path,help='明确列出的 TXT/CSV/XLSX 节点清单，不支持网段扫描')
    p.add_argument('--site-mapping',action='store_true',help='显式启用原项目节点名到IP的映射；默认不推断')
    p.add_argument('--state-dir',type=Path,default=default_state(),help='本机台账/CLI互斥锁目录，不含密码或许可')

def filter_flags(p):
    p.add_argument('--mode',choices=('live','demo'),default='live')
    for name in ('q','model','power','status','sn','lifecycle'):
        p.add_argument('--'+name,default='')
    p.add_argument('--field',default='BMC_Active',choices=FIELDS)
    p.add_argument('--version-filter',default='')
    p.add_argument('--version-source',choices=('observed','reference'),default='observed')
    p.add_argument('--page',type=int,default=1)
    p.add_argument('--page-size',type=int,default=50)

def parser():
    p=Parser(description='BMC通用命令行工具：资产采集/台账统计、独立电源、按组CPLD维护。启动和help不连接设备。')
    p.add_argument('--version',action='version',version='bmc-cli '+__version__)
    sub=p.add_subparsers(dest='command',required=True,parser_class=Parser)
    sub.add_parser('doctor',help='离线检查Python/SQLite/TLS环境，不读取凭据或连接设备')
    plan=sub.add_parser('targets',help='离线检查清单并显示精确节点范围');target_flags(plan)
    collect=sub.add_parser('collect',help='只读采集SN、机型、电源、CPU/内存；--firmware额外读取38项固件')
    target_flags(collect);connection_flags(collect);collect.add_argument('--firmware',action='store_true');collect.add_argument('--memory',action='store_true',help='逐根读取DIMM容量/插槽，另输出内存汇总和明细')
    power=sub.add_parser('power',help='独立电源；status只读，其他动作默认只展示计划')
    power.add_argument('action',choices=('status','off','force-off','on'));target_flags(power);connection_flags(power)
    power.add_argument('--execute',action='store_true',help='真实写入还需 --live 与本次终端交互确认；无 --yes')
    cpld=sub.add_parser('cpld',help='Lattice四组DCU专用适配，不代表任意厂商CPLD通用')
    cpld.add_argument('action',choices=('precheck','upgrade','verify'));target_flags(cpld);connection_flags(cpld)
    cpld.add_argument('--image',action='append',default=[],metavar='02=FILE',help='02/13/46/57或完整组件名=镜像，可重复；不会默认全选')
    cpld.add_argument('--execute',action='store_true')
    asset=sub.add_parser('asset',help='离线台账导入、筛选、编辑、导出，不连接设备')
    ass=asset.add_subparsers(dest='asset_action',required=True,parser_class=Parser)
    for verb in ('import','list','set','export'):
        child=ass.add_parser(verb);child.add_argument('--state-dir',type=Path,default=default_state())
        if verb in ('list','export'): filter_flags(child)
        else: child.add_argument('--mode',choices=('live','demo'),default='live')
        if verb=='import':
            child.add_argument('--file',type=Path,required=True);child.add_argument('--site-mapping',action='store_true')
        if verb=='export': child.add_argument('--output',type=Path,required=True)
        if verb=='set':
            child.add_argument('--address',required=True)
            for field in ('reference-sn','reference-model','location','tags','notes'): child.add_argument('--'+field)
            child.add_argument('--lifecycle',choices=('active','spare','repair','retired'))
    stats=sub.add_parser('stats',help='离线统计采集results.json或资产CSV/XLSX，不连接设备')
    stats.add_argument('--input',type=Path,required=True);stats.add_argument('--output',type=Path)
    stats.add_argument('--field',choices=FIELDS,default='BMC_Active')
    stats.add_argument('--site-mapping',action='store_true')
    demo=sub.add_parser('demo',help='完整内存演示：采集、电源、02/46升级、读回、台账；零设备访问')
    demo.add_argument('--output',type=Path,default=Path('bmc_reports'))
    from .redfish import configure
    configure(sub,target_flags,connection_flags)
    return p

def credentials(args):
    values=read_env(args.env_file) if args.env_file else {}
    user=args.username if args.username is not None else os.environ.get('BMC_USERNAME',values.get('BMC_USERNAME',''))
    password=os.environ.get(args.password_env,values.get('BMC_PASSWORD',''))
    if not user.strip():
        if not sys.stdin.isatty(): raise ValueError('缺少账号；指定 --username 或 BMC_USERNAME')
        user=input('BMC账号：').strip()
    if not password:
        if not sys.stdin.isatty(): raise ValueError('非交互终端缺少密码；请设置密码环境变量或显式 --env-file')
        password=getpass.getpass('BMC密码（不回显）：')
    return user.strip(),password

def options(args):
    user,password=credentials(args)
    return Options(user,password,model=args.model,auth=args.auth,self_signed=args.insecure,
                   timeout=args.timeout,workers=args.workers,flash_timeout=args.flash_timeout,
                   canary_confirmed=args.canary_confirmed)

def images(args):
    result=[]
    aliases={c[3:5]:c for c in COMPONENTS}
    for value in args.image:
        name,sep,path=value.partition('=')
        if not sep: raise ValueError('镜像参数使用 --image 02=FILE')
        component=aliases.get(name,name)
        image=Image.prepare(component,path,'')
        if image.read_verified().startswith(DEMO_MARKER): raise ValueError('演示镜像禁止用于真实设备命令')
        result.append(image)
    if not result or len({i.component for i in result})!=len(result): raise ValueError('须显式选择至少一组，且不能重复组件')
    return tuple(result)

def plan(targets,images_,model,action,args):
    return {'format':'bmc-cli-plan-v1','mode':'live','action':action,'model':model,
            'targets':[{'name':t.name,'address':t.address} for t in targets],
            'images':[{'component':i.component,'filename':i.path.name,'sha256':i.sha256,
                       'version':'由镜像决定；未提供预期版本，不自动判定一致'} for i in images_],
            'auth':args.auth,'tls_certificate_validation':not args.insecure,'workers':args.workers,
            'power_policy':'CPLD必须Off；不自动关机/上电；未知结果不重试' if action=='upgrade' else '仅本次电源动作；不自动升级或执行下一步'}

def confirm_write(scope, *, ttl=120):
    if not sys.stdin.isatty(): raise ValueError('真实写操作必须在交互终端逐次确认；不接受管道、--yes或保存的确认')
    print('本次精确写入范围：\n'+json_text(scope))
    digest=hashlib.sha256(json_text(scope).encode()).hexdigest()[:8]
    phrase=f"确认 {scope['action']} {len(scope['targets'])} 台 {digest} {secrets.token_hex(3)}"
    started=time.monotonic()
    value=input('核对本次节点/机型/镜像/电源过程后，原样输入：'+phrase+'\n> ')
    if time.monotonic()-started>ttl: raise ValueError('确认已过期；未执行写入，请重新开始')
    if value!=phrase: raise ValueError('确认不匹配；未执行写入')
    return {'operator_confirmed':True,'confirmed_at':time.time(),'scope_sha256':hashlib.sha256(json_text(scope).encode()).hexdigest()}

def configure_state(path):
    store=Store(path)
    engine.RUNS_DIR=store.root  # Never write into a .pyz archive or its installation directory.
    return store,Catalog(store)

class Operation:
    """Ctrl+C requests stopping later steps; never interrupts an activated flash."""
    def __init__(self): self.stop=threading.Event();self.previous=None;self.confirming=False
    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            self.previous=signal.signal(signal.SIGINT,self.interrupt)
        return self
    def interrupt(self,*_):
        if self.confirming:raise KeyboardInterrupt()
        self.stop.set();print('\n已请求停止后续步骤；不会打断已开始烧写的组件。',file=sys.stderr)
    def __exit__(self,*_):
        if self.previous is not None: signal.signal(signal.SIGINT,self.previous)

def callback_for(catalog,mode,operation,gate=None,writing=False):
    def callback(target,event,data):
        if data.get('inventory'): catalog.collected(mode,target,data['inventory'])
        if data.get('snapshot'):
            snap=data['snapshot'];catalog.observe(mode,target,snap)
            if gate is not None: gate.observe(target.address,snap,time.monotonic()-snap.get('collection_duration_seconds',0))
        if event=='电源状态确认': catalog.observe(mode,target,{'power':data.get('power')})
        if writing and (event=='停止并待核验' or data.get('error')): operation.stop.set()
    return callback

def result_code(results,stop):
    if stop.is_set(): return 130 if not any(r.get('error') for r in results) else 3
    if any(r.get('error') for r in results): return 3
    if any(r.get('collection_state')=='partial' for r in results): return 4
    return 0

def show_results(results,folder):
    print(json_text({'results':[{'node':r['node'],'address':r['address'],'status':r['status'],'error':r.get('error','')} for r in results],
                     'report_directory':str(folder)}))

def run_device(args):
    targets=load_targets(args)
    image_plan=images(args) if args.command=='cpld' else ()
    action=(POWER.get(args.action,'power-read') if args.command=='power' else args.action) if args.command!='collect' else 'collect'
    writing=args.command=='power' and args.action!='status' or args.command=='cpld' and args.action=='upgrade'
    if writing:
        if not args.model.strip(): raise ValueError('电源/CPLD写操作须明确 --model；可先 power status 只读核对')
        scope=plan(targets,image_plan,args.model,action,args)
        if not args.execute:
            print('仅展示计划：未连接、未写入。实际维护需 --live --execute 和本次终端确认。\n'+json_text(scope));return 0
        if not args.live: raise ValueError('--execute 还需显式 --live；未连接设备')
        if not sys.stdin.isatty(): raise ValueError('真实写操作需要交互终端确认；未连接设备')
        if action=='upgrade' and len(targets)>1 and not args.canary_confirmed: raise ValueError('多台CPLD升级须先完成同机型同镜像单台验证，并显式 --canary-confirmed')
    if not args.live: raise ValueError('设备读取须显式 --live；仅检查范围请用 targets，离线演示用 demo')
    if args.command=='cpld' and not args.model.strip(): raise ValueError('CPLD预检查/读回须指定精确 --model')
    print('本次连接范围：'+json_text([{'name':t.name,'address':t.address} for t in targets]))
    opts=options(args)
    store,catalog=configure_state(args.state_dir)
    if args.command=='collect' and args.targets and args.targets.suffix.lower() in ('.xlsx','.csv'):
        records=import_ledger(args.targets,target_parser=lambda text:parse_text(text,site_mapping=args.site_mapping))
        catalog.register('live',records)
    with Operation() as operation:
        gate=ReadinessGate(ttl=120)
        key=hashlib.sha256(json_text(plan(targets,image_plan,args.model,action,args)).encode()).hexdigest()
        cb=callback_for(catalog,'live',operation,gate,writing=writing)
        if action=='upgrade':
            print('先执行本次只读预检查；失败或未确认 Off 时禁止上传。')
            checked,folder=run_batch(targets,image_plan,opts,'precheck',operation.stop,cb,folder=args.output)
            gate.commit(key,checked)
            ok,reason=gate.evaluate(key,targets,image_plan,opts)
            if not ok or operation.stop.is_set(): show_results(checked,folder);raise ValueError('升级预检查未通过：'+reason)
        approval_record=None
        if writing:
            if operation.stop.is_set(): return 130
            operation.confirming=True
            try: approval_record=confirm_write(scope)
            finally: operation.confirming=False
            for image in image_plan: image.read_verified()
            if action=='upgrade':
                ok,reason=gate.evaluate(key,targets,image_plan,opts)
                if not ok: raise ValueError('确认后状态失效：'+reason)
            if operation.stop.is_set(): return 130
            # Only this interactive, current, exact-scope path constructs a live permit.
            opts=replace(opts,approval=WriteApproval(targets,image_plan,args.model,action))
            store.audit('write_confirmed',action_name=action,**approval_record)
        if args.command=='collect':
            results,folder=run_inventory(targets,opts,operation.stop,cb,args.output,firmware=args.firmware,memory_details=args.memory)
        else:
            results,folder=run_batch(targets,image_plan,opts,action,operation.stop,cb,folder=args.output)
        if approval_record: write_json(folder/'operator_confirmation.json',approval_record)
        store.audit('operation_finished',action_name=action,nodes=len(targets),failed=sum(bool(r.get('error')) for r in results))
        show_results(results,folder)
        if args.command=='collect': export_ledger(folder/'资产台账.xlsx',[catalog.get('live',t.address) for t in targets])
        return result_code(results,operation.stop)

def filters(args):
    return dict(mode=args.mode,q=args.q,model=args.model,power=args.power,status=args.status,sn=args.sn,lifecycle=args.lifecycle,
                firmware_field=args.field,firmware_version=args.version_filter,version_source=args.version_source,page=args.page,page_size=args.page_size)

def run_asset(args):
    store,catalog=configure_state(args.state_dir)
    if args.asset_action=='import':
        rows=import_ledger(args.file,target_parser=lambda text:parse_text(text,site_mapping=args.site_mapping))
        count=catalog.register(args.mode,rows);store.audit('ledger_imported',mode=args.mode,count=count)
        print(json_text({'imported':count,'mode':args.mode,'device_connections':0}));return 0
    if args.asset_action=='set':
        row=catalog.get(args.mode,normalize_target(args.address));meta=dict(row['metadata'])
        for field,dest in (('reference_sn','declared_sn'),('reference_model','declared_model'),('location','location'),('tags','tags'),('notes','notes'),('lifecycle','lifecycle')):
            if getattr(args,field) is not None:meta[dest]=getattr(args,field)
        row=catalog.edit(args.mode,row['address'],meta);store.audit('metadata_edited',mode=args.mode,address=row['address'])
        print(json_text(row));return 0
    result=catalog.query(**filters(args),all_rows=args.asset_action=='export')
    if args.asset_action=='export':
        export_ledger(args.output,result['items']);print(json_text({'exported':len(result['items']),'output':str(args.output)}))
    else: print(json_text(result))
    return 0

def run_stats(args):
    with tempfile.TemporaryDirectory(prefix='bmc_stats_') as temp:
        _,catalog=configure_state(Path(temp)/'state')
        mode='live';source='reference'
        if args.input.suffix.lower()=='.json':
            if args.input.stat().st_size>40*1024*1024: raise ValueError('JSON超过40 MiB')
            values=json.loads(args.input.read_text(encoding='utf-8-sig'),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效JSON数字')))
            if not isinstance(values,list) or not 0<len(values)<=20000: raise ValueError('需要资产采集 results.json 的非空数组')
            for r in values:
                if not isinstance(r,dict) or r.get('collection_state') not in ('basic','complete','partial','failed','stopped') or not isinstance(r.get('firmware'),dict): raise ValueError('仅支持资产采集 results.json；维护报告请使用对应读回结果')
            labeled_demo=any('模拟' in r.get('mode','') for r in values)
            labeled_live=any('真实' in r.get('mode','') for r in values)
            if labeled_demo and labeled_live:raise ValueError('不能混合统计真实与模拟采集结果')
            if labeled_demo: mode='demo'
            for r in values:
                catalog.collected(mode,Target(r['node'],r['address']),r)
            source='observed'
        else:
            rows=import_ledger(args.input,target_parser=lambda text:parse_text(text,site_mapping=args.site_mapping))
            catalog.register(mode,rows)
        result=catalog.query(mode,firmware_field=args.field,version_source=source)
        summary={k:v for k,v in result.items() if k!='items'};summary['device_connections']=0
        summary['input_source']='模拟采集' if mode=='demo' else '离线输入；来源以原文件标识为准'
        print(json_text(summary))
        if args.output: write_json(args.output,summary)
    return 0

def run_demo(args):
    # All clients are memory-only. No network, no real credentials or live approval.
    base=args.output.resolve();base.mkdir(parents=True,exist_ok=True)
    folder=base/('demo_'+time.strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3));folder.mkdir(mode=0o700)
    _,catalog=configure_state(folder/'state')
    targets=[Target('DEMO-01','192.0.2.11'),Target('DEMO-02','192.0.2.12'),Target('DEMO-03','192.0.2.13')]
    opts=Options('demo-user','demo-password',model='CY62-T58',workers=2,poll=.01,canary_confirmed=True)
    for target in targets: DemoClient.states.pop(target.address,None);DemoClient(target.address,opts)
    DemoClient.states[targets[0].address]['power']='On'
    path=folder/'DEMO_LT_NOT_FOR_HARDWARE.svf';path.write_bytes(DEMO_MARKER+b'0.5\n')
    selected=[Image.prepare(c,path,'') for c in (COMPONENTS[0],COMPONENTS[2])]
    with Operation() as operation:
        cb=callback_for(catalog,'demo',operation)
        checked,_=run_batch(targets,selected,opts,'precheck',operation.stop,cb,folder=folder,client_class=DemoClient)
        if not any(r.get('error') for r in checked): raise ValueError('模拟开机门禁未生效')
        off,_=run_batch(targets,[],opts,'GracefulShutdown',operation.stop,cb,folder=folder,client_class=DemoClient)
        if any(r.get('error') for r in off): raise ValueError('模拟独立电源动作失败')
        checked,_=run_batch(targets,selected,opts,'precheck',operation.stop,cb,folder=folder,client_class=DemoClient)
        if any(r.get('error') for r in checked): raise ValueError('模拟Off预检查失败')
        upgraded,_=run_batch(targets,selected,opts,'upgrade',operation.stop,cb,folder=folder,client_class=DemoClient)
        if any(r.get('error') for r in upgraded): raise ValueError('模拟升级失败')
        verified,_=run_batch(targets,selected,opts,'verify',operation.stop,cb,folder=folder,client_class=DemoClient)
        if any(r.get('error') for r in verified): raise ValueError('模拟读回失败')
        results,report=run_inventory(targets,opts,operation.stop,cb,folder,demo=True,memory_details=True)
        for target in targets:
            assert DemoClient.states[target.address]['versions']=={c:'0.5' if c in (COMPONENTS[0],COMPONENTS[2]) else '0.3' for c in COMPONENTS}
        export_ledger(folder/'模拟资产台账.xlsx',catalog.query('demo',all_rows=True)['items'])
        record={'mode':'simulation','real_device_connections':0,'On_blocked':True,'power_without_images':True,'selected_components':['Dcu02Cpld','Dcu46Cpld'],'unselected_unchanged':True,'asset_records':len(results),'report_directory':str(folder),'inventory_results':str(report/'results.json')}
        write_json(folder/'demo_summary.json',record);print(json_text(record))
        return 0

def main(argv=None):
    try:
        args=parser().parse_args(argv)
        if args.command=='doctor':
            import sqlite3,ssl,platform
            with sqlite3.connect(':memory:') as db:json1=db.execute('SELECT json_extract(?,?)',(json.dumps({'ok':1}),'$.ok')).fetchone()[0]==1
            print(json_text({'version':__version__,'python':platform.python_version(),'system':platform.system(),'sqlite':sqlite3.sqlite_version,'sqlite_json':json1,'tls':ssl.OPENSSL_VERSION,'third_party_packages_required':0,'device_connections':0}))
            return 0 if json1 and sys.version_info>=(3,11) else 2
        if args.command=='targets':
            print(json_text({'targets':[{'name':t.name,'address':t.address} for t in load_targets(args)],'device_connections':0}));return 0
        if args.command=='redfish':
            from .redfish import run
            return run(args,credentials,Operation)
        if args.command=='asset': return run_asset(args)
        if args.command=='stats': return run_stats(args)
        if args.command=='demo': return run_demo(args)
        return run_device(args)
    except KeyboardInterrupt:
        print('已取消；未自动重试或恢复任务。',file=sys.stderr);return 130
    except (ValueError,BmcError) as exc:
        print('错误：'+str(exc),file=sys.stderr);return 2
    except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
        # Do not echo arbitrary environment values, request text or exception repr.
        print('文件或数据处理失败：'+type(exc).__name__+'；未自动重试写操作。',file=sys.stderr);return 2
