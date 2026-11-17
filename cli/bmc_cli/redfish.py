"""CLI facade for the shared three-module engine. Plans are offline by default."""
import getpass,json,secrets,sys,time
from pathlib import Path
from redfish_ops import catalog
from redfish_workflow import prepare,run_tasks,Approval
from .targets import load_targets

def configure(sub,target_flags,connection_flags):
 p=sub.add_parser('redfish',help='Systems/Chassis/UpdateService；强制任务级Session认证')
 commands=p.add_subparsers(dest='rf_action',required=True)
 c=commands.add_parser('catalog',help='离线列出69项操作与适配状态');c.add_argument('--module',choices=['Systems','Chassis','UpdateService'])
 for action in ['plan','run']:
  c=commands.add_parser(action,help='离线计划' if action=='plan' else '明确范围后执行；写操作还需TTY一次性确认')
  target_flags(c);connection_flags(c);c.set_defaults(auth='session')
  c.add_argument('--operation',required=True,help='文档章节编号，如3.3.7')
  c.add_argument('--params',type=Path,help='资源ID参数JSON对象文件，不是URL')
  c.add_argument('--body',type=Path,help='受限业务请求体JSON文件，不接受凭据字段')
  c.add_argument('--image',type=Path,help='G50本地固件；与原四组DCU镜像适配分开')
  c.add_argument('--expected-version',default='')
  c.add_argument('--execute',action='store_true',help='写操作还需--live及交互确认；不自动重试')

def run(args,credentials,Operation):
 if args.rf_action=='catalog':
  rows=[r for r in catalog() if not args.module or r['module']==args.module]
  print(json.dumps({'operations':rows,'device_connections':0,'authentication':'session'},ensure_ascii=False,indent=2));return 0
 if args.auth!='session':raise ValueError('新Redfish模块仅支持Session，不自动降级Basic')
 def read(path):
  if not path:return {}
  if path.stat().st_size>64*1024:raise ValueError('参数文件超过64KiB')
  return json.loads(path.read_text(encoding='utf-8-sig'))
 targets=load_targets(args)
 request={'operation':args.operation,'params':read(args.params),'payload':read(args.body),'expected_model':args.model,'expected_version':args.expected_version,'workers':args.workers,'timeout':args.timeout,'task_timeout':args.flash_timeout,'insecure':args.insecure,'canary_confirmed':args.canary_confirmed}
 if args.image:request['image']=str(args.image)
 op,request,public,scope=prepare(targets,request)
 writing=op['method']!='GET'
 if args.rf_action=='plan' or not args.live or (writing and not args.execute):
  print(json.dumps({'offline_plan':public,'device_connections':0,'note':'未连接设备；变更需run --live --execute及本次TTY确认'},ensure_ascii=False,indent=2));return 0
 if writing and not sys.stdin.isatty():raise ValueError('变更操作需要交互终端，不接受管道确认')
 user,password=credentials(args)
 if args.operation=='3.3.22':
  old=getpass.getpass('当前BIOS密码（不回显，可按平台情况留空）：')
  new=getpass.getpass('本次新BIOS密码（不回显）：')
  if new!=getpass.getpass('再次输入新BIOS密码：'):raise ValueError('两次新BIOS密码不一致')
  request['bios_passwords']={'old':old,'new':new}
  _,request,public,scope=prepare(targets,request)
 if password in json.dumps(public,ensure_ascii=False):raise ValueError('参数包含本次密码，拒绝输出或保存')
 # Same conservative CLI lock used by existing maintenance commands.
 from .store import Store
 import firmware_batch as engine
 store=Store(args.state_dir);engine.RUNS_DIR=store.root
 with Operation() as operation:
  approval=None
  if writing:
   phrase='确认本次 '+secrets.token_hex(4);expires=time.time()+120
   print(json.dumps(public,ensure_ascii=False,indent=2))
   operation.confirming=True
   try:value=input('核对上述完整范围和影响后输入：'+phrase+'\n> ')
   finally:operation.confirming=False
   if value!=phrase or time.time()>expires:raise ValueError('确认不匹配或已过期；未连接设备')
   _,request,_,fresh=prepare(targets,request)
   if fresh!=scope:raise ValueError('镜像或范围已改变，旧确认作废')
   approval=Approval(scope,expires)
  if operation.stop.is_set():return 130
  rows,folder=run_tasks(targets,request,(user,password),args.output,approval=approval,stop=operation.stop)
  print(json.dumps({'results':[{'node':r['node'],'address':r['address'],'state':r['state'],'status':r['status'],'session_cleanup':r['session_cleanup'],'error':r.get('error','')} for r in rows],'report_directory':str(folder)},ensure_ascii=False,indent=2))
  return 3 if any(r['state'] not in ('completed','stopped') for r in rows) else 130 if operation.stop.is_set() else 0
