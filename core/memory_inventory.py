"""Read-only DIMM enumeration and conservative 32/64 GiB classification.
No power actions; no inference from total RAM, model, filename or slot count.
"""
from collections import Counter
import math
from urllib.parse import urljoin,urlsplit
from bmc_client import BmcError,ApiError,serial_text

MAX_MEMBERS=1024
MAX_PAGES=64
FIELDS=('Id','Name','DeviceLocator','Manufacturer','PartNumber','SerialNumber','MemoryDeviceType',
        'OperatingSpeedMhz','DataWidthBits','RankCount','BaseModuleType','FirmwareRevision')

def capacity(value):
    if isinstance(value,bool):return None
    if isinstance(value,str):
        if not value.strip().isdigit():return None
        value=int(value.strip())
    if not isinstance(value,(int,float)) or not math.isfinite(value) or int(value)!=value or not 0<value<=2**30:return None
    return int(value)

def summary(dimms,*,complete,expected=None,total_gib=None):
    known=[d for d in dimms if d.get('presence')=='present' and d.get('capacity_mib')]
    sizes=Counter(d['capacity_mib'] for d in known)
    empty=sum(d.get('presence')=='absent' for d in dimms)
    unknown=sum(d.get('presence') not in ('present','absent') for d in dimms)
    missing=max(0,expected-len(dimms)) if expected is not None else 0
    unknown+=missing
    total_mib=sum(d['capacity_mib'] for d in known)
    match='无法核对'
    if isinstance(total_gib,(int,float)) and not isinstance(total_gib,bool) and math.isfinite(total_gib) and known:
        match='一致' if total_mib==total_gib*1024 else '不一致（需核验禁用/缺失/缓存等原因）'
    if not known:conclusion='待核验：未取得有效在位DIMM容量'
    elif len(sizes)>1:
        conclusion='混插：'+ ' + '.join(f'{n}×{size/1024:g}GB' for size,n in sorted(sizes.items()))
        if not complete or unknown:conclusion+='（信息未完整）'
    elif not complete or unknown:
        size=next(iter(sizes));conclusion=f'已读{size/1024:g}GB条；信息不完整，不能判定全机配置'
    else:
        size=next(iter(sizes));conclusion=f'{len(known)}×{size/1024:g}GB'
    return {'complete':complete,'members_expected':expected,'members_discovered':len(dimms),
            'present_known':len(known),'empty_slots':empty,'unknown_capacity':unknown,
            'unbounded_unknown':not complete and expected is None,
            'count_32gb':sizes.get(32768,0) if dimms else None,
            'count_64gb':sizes.get(65536,0) if dimms else None,
            'count_other':sum(n for size,n in sizes.items() if size not in (32768,65536)) if dimms else None,
            'dimm_total_gib':total_mib/1024 if known else None,'system_total_gib':total_gib,
            'total_comparison':match,'capacity_groups':{f'{size/1024:g}GB':n for size,n in sorted(sizes.items())},
            'conclusion':conclusion}

def read_memory(client,system,system_uri,stop):
    result={'state':'failed','collection_path':'','dimms':[],'notes':[]}
    total=system.get('MemorySummary',{}).get('TotalSystemMemoryGiB') if isinstance(system.get('MemorySummary'),dict) else None
    expected=None;complete=True;members=[];seen_members=set();seen_pages=set()
    link=system.get('Memory',{})
    link=link.get('@odata.id') if isinstance(link,dict) else None
    link=link or system_uri.rstrip('/')+'/Memory'
    def resolve(link,context):
        if not isinstance(link,str) or not link:raise BmcError('内存资源缺少有效链接')
        return client.safe_path(urljoin(client.base+context,link))
    def error_text(exc):
        return 'HTTP '+str(exc.status) if isinstance(exc,ApiError) else type(exc).__name__+'（连接/响应/链接失败）'
    try:
        path=resolve(link,system_uri);base_path=urlsplit(path).path.rstrip('/')
        result['collection_path']=base_path
        for page in range(MAX_PAGES):
            if stop.is_set():raise InterruptedError()
            if path in seen_pages:raise BmcError('Memory分页循环')
            if urlsplit(path).path.rstrip('/')!=base_path:raise BmcError('Memory分页离开当前集合')
            seen_pages.add(path)
            body,_=client.request('GET',path)
            if not isinstance(body,dict) or not isinstance(body.get('Members'),list):raise BmcError('Memory集合不是有效Members对象')
            count=body.get('Members@odata.count')
            if isinstance(count,str) and count.isdigit():count=int(count)
            if type(count) is int and 0<=count<=MAX_MEMBERS:
                if expected is None:expected=count
                elif expected!=count:complete=False;result['notes'].append('分页成员总数不一致')
            elif count is not None:complete=False;result['notes'].append('Memory成员数无效或超过限制')
            for member in body['Members']:
                raw=member.get('@odata.id') if isinstance(member,dict) else None
                try:
                    uri=resolve(raw,base_path+'/')
                    if not urlsplit(uri).path.startswith(base_path+'/'):raise BmcError('DIMM链接离开当前Memory集合')
                    if uri in seen_members:complete=False;result['notes'].append('重复DIMM链接未重复计数');continue
                    seen_members.add(uri);members.append(uri)
                    if len(members)>MAX_MEMBERS:raise BmcError('DIMM成员超过限制')
                except BmcError:
                    complete=False;result['notes'].append('Memory成员链接无效/越界；未访问该链接')
            next_link=body.get('Members@odata.nextLink') or body.get('@odata.nextLink')
            if not next_link:break
            path=resolve(next_link,path)
        else:raise BmcError('Memory分页超过限制')
    except InterruptedError:
        complete=False;result['notes'].append('已停止后续内存读取')
    except (BmcError,ValueError,TypeError) as exc:
        complete=False;result['notes'].append('Memory集合读取失败：'+error_text(exc))
        if isinstance(exc,ApiError) and exc.status in (404,405):result['state']='unsupported'
    if expected is not None and len(members)!=expected:
        complete=False;result['notes'].append(f'集合声明{expected}项，实际枚举{len(members)}项')
    halted=False;locators=set()
    for uri in members[:MAX_MEMBERS]:
        row={'path':urlsplit(uri).path,'presence':'unknown','capacity_mib':None,'capacity_gib':None}
        try:
            if halted or stop.is_set():raise InterruptedError()
            body,_=client.request('GET',uri)
            if not isinstance(body,dict):raise BmcError('DIMM不是JSON对象')
            for field in FIELDS:row[field]=serial_text(body.get(field))
            status=body.get('Status',{})
            if not isinstance(status,dict):status={}
            row['state']=serial_text(status.get('State'));row['health']=serial_text(status.get('Health'))
            location=body.get('MemoryLocation',{})
            row['location']={k:serial_text(location.get(k)) for k in ('Socket','Controller','Channel','Slot')} if isinstance(location,dict) else {}
            value=capacity(body.get('CapacityMiB'))
            if row['state'].lower() in ('absent','notpresent'):
                row['presence']='absent'
            elif value:
                row.update(presence='present',capacity_mib=value,capacity_gib=value/1024)
            else:row['note']='容量缺失、无效或为0；不能据此确定为空槽'
            locator=row.get('DeviceLocator')
            if locator and locator in locators:
                complete=False;row['note']='插槽标识重复，容量统计需人工核验'
            if locator:locators.add(locator)
        except InterruptedError:
            complete=False;row['note']='已停止或此前认证/连接失败；本项未继续读取'
        except (BmcError,ValueError,TypeError) as exc:
            complete=False;row['note']='DIMM读取失败：'+error_text(exc)
            if not isinstance(exc,ApiError) or exc.status in (401,403,429,503):halted=True
        result['dimms'].append(row)
    if any(d.get('presence')=='unknown' for d in result['dimms']):complete=False
    if complete and members:result['state']='complete'
    elif result['state']!='unsupported':result['state']='partial' if members else 'unavailable'
    if not members:complete=False;result['notes'].append('没有取得DIMM成员；不是“0根内存”的证明')
    result['summary']=summary(result['dimms'],complete=complete,expected=expected,total_gib=total)
    if result['summary']['total_comparison'].startswith('不一致'):
        result['notes'].append('DIMM容量合计与System总容量不一致，需核验');result['state']='partial'
    if not result['summary']['present_known'] and result['state']=='complete':result['state']='partial'
    return result
