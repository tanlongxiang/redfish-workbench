"""Three-module capability registry and validated input contracts, not arbitrary HTTP."""
import copy,hashlib,json,re
from redfish_catalog_data import OPERATIONS
from bmc_client import BmcError
BY_ID={r['id']:r for r in OPERATIONS}
TOP_KEYS={'3.3.22':['PasswordName'],'3.3.24':['AssetTag','PowerRestorePolicyTypes','IndicatorLED','HostName','Boot','Oem'],'3.4.3':['IndicatorLED','Oem'],'3.4.5':['Oem'],'3.4.20':['PowerControl','Redundancy']}
SAMPLES={
 '3.3.14':{'VolumeName':'','Drives':[],'VolumeRaidLevel':'','SpanNumber':1,'CapacityBytes':0,'DefaultReadPolicy':'','DefaultWritePolicy':'','DefaultCachePolicy':'','AccessPolicy':'','DriveCachePolicy':'','InitializationMode':'','OptimumIOSizeBytes':0},
 '3.3.16':{'Type':''},'3.3.21':{'Attributes':{}},'3.3.22':{'PasswordName':''},
 '3.3.23':{'ResetType':''},'3.3.24':{'AssetTag':''},'3.3.26':{'BondingEnable':False},'3.3.37':{},
 '3.3.39':{'NCSIMode':'','PackageID':0,'NCSIInterface':'','ChannelNumber':0},'3.4.3':{'IndicatorLED':''},
 '3.4.5':{'Oem':{'BMC':{'FanSpeedAdjustmentMode':'','FanSpeedLevelPercents':[]}}},
 '3.4.20':{'PowerControl':[{'PowerLimit':{'LimitInWatts':0,'LimitException':''}}]},'3.7.4':{'ConfigList':[]},
 '3.7.6':{'Targets':[],'SaveConfig':True}}
IMPACTS={'3.3.14':'创建逻辑盘，可能影响数据布局','3.3.15':'删除逻辑盘，可能导致数据丢失','3.3.16':'初始化逻辑盘，可能破坏数据','3.3.21':'修改BIOS待生效设置，不自动重启','3.3.22':'修改BIOS密码','3.3.23':'改变主机电源状态或触发NMI','3.3.26':'修改网络绑定，可能失联','3.3.32':'清除SEL日志，不可恢复','3.3.39':'修改NCSI，可能失联','3.4.5':'改变散热策略','3.4.20':'改变功率或电源冗余策略','3.7.4':'保存固件配置；空请求体默认全部保存','3.7.5':'上传固件，不激活','3.7.6':'上传、激活并跟踪固件任务；不会自动关机/上电/回滚'}
# Capability checks still run against the device; catalog entries are not field validation claims.
BLOCKED={}
def resource_parameters(op):
 # Non-System writes still need an explicit System on multi-System devices
 # for model/power checks. This context ID is not part of the operation URL.
 parameters=re.findall(r'\{(\w+)\}',op['path'])
 if op['method']!='GET' and 'system_id' not in parameters:parameters.append('system_id')
 return parameters

def catalog():
 rows=[]
 for raw in OPERATIONS:
  r=copy.deepcopy(raw);r['write']=r['method']!='GET';r['parameters']=resource_parameters(r)
  r['body_keys']=TOP_KEYS.get(r['id'],r['body_keys']);r['sample']=SAMPLES.get(r['id'],{});r['blocked']=BLOCKED.get(r['id'],'')
  r['impact']=IMPACTS.get(r['id'],'修改设备状态或配置' if r['write'] else '仅读取此资源')
  r['verification']='文档适配；未做真实设备写验证';rows.append(r)
 return rows

def contract(operation,params=None,payload=None):
 if operation not in BY_ID:raise BmcError('操作不在三个模块的允许目录中')
 op=BY_ID[operation]
 if operation in BLOCKED:raise BmcError(BLOCKED[operation])
 params={} if params is None else params;payload={} if payload is None else payload
 if not isinstance(params,dict) or not isinstance(payload,dict):raise BmcError('资源参数与请求体必须为对象')
 allowed=set(resource_parameters(op))
 if set(params)-allowed:raise BmcError('包含此操作未声明的资源参数')
 for key,value in params.items():
  if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):raise BmcError('资源ID只允许字母、数字、下划线与连字符')
 for key in allowed-{'system_id','chassis_id'}:
  if not params.get(key):raise BmcError('缺少资源ID参数：'+key)
 keys=set(TOP_KEYS.get(operation,op['body_keys']))
 if set(payload)-keys:raise BmcError('请求体含文档未声明的顶层字段')
 if op['method']=='GET' and payload:raise BmcError('只读操作不接受请求体')
 if len(json.dumps(payload,ensure_ascii=False,allow_nan=False).encode())>64*1024:raise BmcError('请求体超过64KiB')
 def walk(v):
  if isinstance(v,dict):
   for k,x in v.items():
    if re.search(r'(?i)(password|token|cookie|authorization|authkey|secret)',k) and not (operation=='3.3.22' and k=='PasswordName'):raise BmcError('通用参数不允许凭据字段')
    walk(x)
  elif isinstance(v,list):
   for x in v:walk(x)
  elif isinstance(v,str) and re.search(r'[A-Za-z]+://[^/\s]*@',v):raise BmcError('参数不允许内嵌URL凭据')
  elif type(v) not in (str,int,float,bool,type(None)):raise BmcError('请求体类型不支持')
 walk(payload)
 required={'3.3.14':{'VolumeName','Drives','VolumeRaidLevel','CapacityBytes'},'3.3.16':{'Type'},'3.3.21':{'Attributes'},'3.3.22':{'PasswordName'},'3.3.23':{'ResetType'},'3.3.26':{'BondingEnable'},'3.3.39':{'NCSIMode','PackageID','NCSIInterface','ChannelNumber'},'3.4.5':{'Oem'},'3.4.20':{'PowerControl'},'3.7.6':{'Targets'}}.get(operation,set())
 if operation=='3.4.20' and 'Redundancy' in payload:required=set()
 if not required<=set(payload):raise BmcError('缺少必要字段：'+','.join(sorted(required-set(payload))))
 if op['method']=='PATCH' and not payload:raise BmcError('配置修改不能为空')
 if operation=='3.3.23' and payload.get('ResetType') not in {'On','ForceOff','ForceRestart','GracefulShutdown','ForcePowerCycle','Nmi'}:raise BmcError('未选择受支持的ResetType')
 if operation=='3.3.21' and (not isinstance(payload['Attributes'],dict) or not payload['Attributes']):raise BmcError('BIOS Attributes不能为空')
 if operation=='3.3.22' and (not isinstance(payload['PasswordName'],str) or not payload['PasswordName'].strip()):raise BmcError('需要明确BIOS密码属性名称，不猜测管理员/用户密码')
 if operation=='3.3.26' and type(payload['BondingEnable']) is not bool:raise BmcError('BondingEnable必须是布尔值')
 if operation=='3.3.14' and (not isinstance(payload.get('Drives'),list) or not payload['Drives'] or type(payload.get('CapacityBytes')) is not int or payload['CapacityBytes']<=0):raise BmcError('必须明确磁盘列表及正整数容量')
 if operation=='3.7.6':
  targets=payload['Targets']
  if not isinstance(targets,list) or len(targets)!=1 or not re.fullmatch(r'/redfish/v1/UpdateService/FirmwareInventory/(?:ActiveBMC|BackupBMC|Bios|Cpld|PSU[1-9]?)',targets[0]):raise BmcError('一次升级必须明确一个受支持的固件目标')
  if 'ImageURI' in payload or 'TransferProtocol' in payload:raise BmcError('首版只支持绑定SHA256的本地镜像，不接受远程镜像URI或凭据')
  if 'SaveConfig' in payload and type(payload['SaveConfig']) is not bool:raise BmcError('SaveConfig必须是布尔值')
 # Reject undocumented nested properties instead of exposing an OEM request tunnel.
 trees={
  '3.3.24':{'Boot':{'BootSourceOverrideMode','BootSourceOverrideEnabled','BootSourceOverrideTarget'},'Oem':{'Public'},'Oem.Public':{'ConfigurationModel'}},
  '3.4.3':{'Oem':{'BMC'},'Oem.BMC':{'PowerButtonEnabled'}},
  '3.4.5':{'Oem':{'BMC'},'Oem.BMC':{'FanSpeedAdjustmentMode','FanSpeedLevelPercents'}},
  '3.4.20':{'PowerControl':{'PowerLimit','Oem'},'PowerControl.PowerLimit':{'LimitInWatts','LimitException'},'PowerControl.Oem':{'BMC'},'PowerControl.Oem.BMC':{'ExpectedRedundancy','ExpectedActivePSU'},'PowerControl.Oem.BMC.ExpectedRedundancy':{'@odata.id'},'PowerControl.Oem.BMC.ExpectedActivePSU':{'@odata.id'},'Redundancy':{'Mode','RedundancySet'},'Redundancy.RedundancySet':{'@odata.id'}}}
 def nested(value,prefix=''):
  if isinstance(value,list):
   for v in value:nested(v,prefix)
  elif isinstance(value,dict):
   if prefix and prefix in trees.get(operation,{}) and not set(value)<=trees[operation][prefix]:raise BmcError('嵌套参数包含文档未声明的字段')
   for k,v in value.items():nested(v,prefix+'.'+k if prefix else k)
 nested(payload)
 return copy.deepcopy(op),copy.deepcopy(params),copy.deepcopy(payload)

def fingerprint(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
