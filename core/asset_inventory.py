"""Read-only asset collection. No firmware writer and no device power operations."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from bmc_client import BmcClient, ApiError, BmcError, serial_text
from node_verify import FIRMWARE, has_sn
from inventory_merge import usable_firmware
from firmware_demo import DemoClient
from firmware_batch import COMPONENTS, Target
from xlsx_io import write_xlsx
from firmware_batch import Journal, NodeLock


class DemoInventoryClient:
    """Memory-only, explicitly simulated assets; never creates a socket."""
    def __init__(self, address, options):
        self.demo = DemoClient(address, options)
        self.address = address
        self.root = {'Systems': {'@odata.id': '/redfish/v1/Systems'}}
    def login(self, *args): pass
    def close(self): return ''
    def request(self, method, path):
        if method != 'GET': raise AssertionError('Inventory transport is read-only')
        if path == '/redfish/v1/Systems':
            return {'Members': [{'@odata.id': '/redfish/v1/Systems/1'}], 'Members@odata.count': 1}, {}
        if path == '/redfish/v1/Systems/1':
            suffix = self.address.rsplit('.', 1)[-1]
            return {'Model': 'CY62-T58', 'Manufacturer': 'DEMO (simulation)',
                    'SerialNumber': '' if suffix == '13' else 'DEMO-SN-' + suffix,
                    'PowerState': self.demo.state['power'], 'BiosVersion': 'DEMO-BIOS-1.0',
                    'ProcessorSummary': {'Count': 2, 'Model': 'DEMO CPU'},
                    'MemorySummary': {'TotalSystemMemoryGiB': 512},
                    'Memory': {'@odata.id':'/redfish/v1/Systems/1/Memory'},
                    'Links': {'Chassis': [{'@odata.id': '/redfish/v1/Chassis/1'}]}}, {}
        if path == '/redfish/v1/Chassis/1':
            return {'SerialNumber': 'DEMO-CHASSIS'}, {}
        memory='/redfish/v1/Systems/1/Memory'
        suffix=self.address.rsplit('.',1)[-1]
        sizes=([65536]*8 if suffix=='11' else [32768]*16 if suffix=='12' else [65536]*4+[32768]*8)
        if path==memory:
            return {'Members':[{'@odata.id':memory+'/DIMM_'+str(i)} for i in range(len(sizes))],'Members@odata.count':len(sizes)},{}
        if path.startswith(memory+'/DIMM_'):
            index=int(path.rsplit('_',1)[-1])
            return {'Id':'DIMM_'+str(index),'CapacityMiB':sizes[index],'DeviceLocator':'DIMM_'+str(index),'Manufacturer':'DEMO','PartNumber':'DEMO-DIMM','SerialNumber':'DEMO-DIMM-'+str(index),'MemoryDeviceType':'DDR4','OperatingSpeedMhz':3200,'RankCount':2,'Status':{'State':'Enabled','Health':'OK'}},{}
        prefix = '/redfish/v1/UpdateService/FirmwareInventory/'
        if path.startswith(prefix):
            endpoint = path[len(prefix):]
            value = self.demo.state['versions'].get(endpoint, 'DEMO-1.0')
            state = 'Absent' if self.address.endswith('.12') and endpoint == 'Pciesw0' else 'Enabled'
            return {'Version': value, 'Status': {'State': state}}, {}
        raise BmcError('Unknown simulated inventory resource')


def system_resource(client, *, return_path=False):
    link = client.root.get('Systems', {})
    path = link.get('@odata.id') if isinstance(link, dict) else None
    collection, _ = client.request('GET', path or '/redfish/v1/Systems')
    members = collection.get('Members', [])
    if (not isinstance(members, list) or len(members) != 1 or
        collection.get('Members@odata.count') not in (None, 1, '1') or
        collection.get('Members@odata.nextLink') or collection.get('@odata.nextLink') or
        not isinstance(members[0], dict) or not isinstance(members[0].get('@odata.id'), str)):
        raise BmcError('Systems 不是唯一且完整的单节点集合；不猜测设备身份')
    system, _ = client.request('GET', members[0]['@odata.id'])
    if not isinstance(system, dict): raise BmcError('System 不是 JSON 对象')
    return (system,members[0]['@odata.id']) if return_path else system


def collect_one(target, options, stop, callback, journal, *, demo=False, client_class=None, firmware=True, memory_details=False):
    result = {'node': target.name, 'address': target.address, 'mode':'模拟（无设备操作）' if demo else '真实只读', 'status': '读取失败',
              'collection_state': 'failed', 'firmware': {}, 'notes': [], 'components': []}
    client = None
    def emit(event, **data):
        try:
            journal.add(target, event, **data)
            callback(target, event, data)
        except Exception:
            stop.set(); raise
    try:
        if stop.is_set():
            result.update(status='已停止，未连接', collection_state='stopped'); return result
        with NodeLock(target.address):
            cls = client_class or (DemoInventoryClient if demo else BmcClient)
            client = cls(target.address, options) if demo else cls(target.address, options.timeout, options.self_signed)
            client.login(options.username, options.password, options.auth)
            if stop.is_set(): raise InterruptedError()
            system,system_uri = system_resource(client,return_path=True)
            sn = serial_text(system.get('SerialNumber'))
            result.update(model=serial_text(system.get('Model')), power=serial_text(system.get('PowerState')),
                          manufacturer=serial_text(system.get('Manufacturer')), system_sn=sn if has_sn(sn) else '',
                          bios=serial_text(system.get('BiosVersion')))
            processor, memory = system.get('ProcessorSummary', {}), system.get('MemorySummary', {})
            if isinstance(processor, dict):
                count = processor.get('Count'); result['cpu_count'] = count if type(count) is int and 0 <= count <= 4096 else None
                result['cpu_model'] = serial_text(processor.get('Model'))
            if isinstance(memory, dict):
                gib = memory.get('TotalSystemMemoryGiB')
                result['memory_gib'] = gib if type(gib) in (int, float) and 0 <= gib <= 10**7 else None
            if memory_details:
                from memory_inventory import read_memory
                result['memory']=read_memory(client,system,system_uri,stop)
            links = system.get('Links', {})
            chassis = links.get('Chassis', []) if isinstance(links, dict) else []
            if isinstance(chassis, list) and len(chassis) == 1 and isinstance(chassis[0], dict) and isinstance(chassis[0].get('@odata.id'), str):
                try:
                    body, _ = client.request('GET', chassis[0]['@odata.id'])
                    result['chassis_sn'] = serial_text(body.get('SerialNumber'))
                except BmcError: result['notes'].append('Chassis 读取失败（不替代 System SN）')
            for name, endpoint in (FIRMWARE if firmware else ()):
                if stop.is_set(): raise InterruptedError()
                try:
                    body, _ = client.request('GET', '/redfish/v1/UpdateService/FirmwareInventory/' + endpoint)
                    value = body.get('Version')
                    if usable_firmware(value, body.get('Status')):
                        result['firmware'][name] = str(value).strip()
                    else: result['notes'].append(name + '：无有效版本或部件不在位')
                except ApiError as exc:
                    result['notes'].append(f'{name}: HTTP {exc.status}')
                    if exc.status in (401, 403, 429, 503): break
                except BmcError:
                    result['notes'].append(name + '：接口读取失败，停止后续请求'); break
            result['collection_state'] = ('complete' if len(result['firmware']) == len(FIRMWARE) else 'partial') if firmware else 'basic'
            result['status'] = ('资产采集完成' if result['collection_state'] == 'complete' else '资产部分采集') if firmware else '基础资产已采集'
            if memory_details:
                if result['memory']['state']=='complete':result['status']='内存DIMM已采集（只读）'
                else:result['collection_state']='partial';result['status']='内存信息未完整，待核验'
    except InterruptedError:
        result.update(status='已停止（保留已读取信息）', collection_state='stopped')
    except Exception as exc:
        result['error'] = str(exc) if isinstance(exc, BmcError) else '资产读取失败：' + type(exc).__name__
    finally:
        if client is not None:
            try: client.close()
            except Exception: result['notes'].append('本次会话注销未确认')
        # Explicitly selected fields only. Never persist a raw response or headers.
        for secret in (options.password,):
            if secret:
                result['notes'] = [s.replace(secret, '[REDACTED]') for s in result['notes']]
                if result.get('error'): result['error'] = result['error'].replace(secret, '[REDACTED]')
        result['observed'] = time.time()
        emit('资产采集结果', inventory=result)
    return result


def run_inventory(targets, options, stop, callback, folder, *, demo=False, firmware=True, memory_details=False):
    journal = Journal(folder)
    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        futures = [pool.submit(collect_one, t, options, stop, callback, journal, demo=demo, firmware=firmware, memory_details=memory_details) for t in targets]
        results = [future.result() for future in as_completed(futures)]
    order = {t.address: i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order[r['address']])
    import json
    (journal.folder/'results.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    headers = ['节点名', 'BMC_IP', '模式', '结果', '机型', '电源', '现场SystemSN', 'ChassisSN', 'CPU数量', '内存GiB'] + [n for n, _ in FIRMWARE if firmware] + ['说明']
    rows = [[r['node'], r['address'], '模拟' if demo else '真实只读', r['status'], r.get('model', ''), r.get('power', ''), r.get('system_sn', ''), r.get('chassis_sn', ''), r.get('cpu_count'), r.get('memory_gib')] + [r['firmware'].get(n, '') for n, _ in FIRMWARE if firmware] + ['；'.join(r['notes']) or r.get('error', '')] for r in results]
    write_xlsx(str(journal.folder/'维护结果.xlsx'), headers, rows)
    if memory_details:
        from memory_report import export_memory_report
        export_memory_report(journal.folder/'内存配置统计.xlsx',results)
    return results, journal.folder
