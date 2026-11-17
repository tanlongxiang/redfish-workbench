"""Node inventory reconciliation and read-only Redfish verification.

Offline by default. Neither offline nor live verification writes back to input files.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from bmc_client import ApiError, BmcClient, BmcError, normalize_target, serial_text, stamp
from xlsx_io import InputBook, write_xlsx
from local_config import load_credentials
from workspace_paths import default_inputs

CIRCLES = 'abcdefghjkmn'
PLACEHOLDERS = {'', '0', '00', '01', '1', 'na', 'n/a', '-', 'none', 'null', 'unknown'}
SCOPES = {'all': None, 'conflict': 'SN冲突', 'fill': '待补齐SN', 'missing': '总表无此节点'}
FIRMWARE = [('Bios', 'Bios'), ('BMC_Active', 'ActiveBMC'), ('BMC_Backup', 'BackupBMC'),
            ('CPLD_Main', 'Cpld'), ('CPLD_Backup', 'Cpld2'), ('Liquid', 'Liquid'),
            ('FunctionBoard', 'FunctionBoard')]
FIRMWARE += [(n, n) for n in ('Dcu02Cpld', 'Dcu13Cpld', 'Dcu46Cpld', 'Dcu57Cpld')]
FIRMWARE += [(f'{p}{i}', f'{p}{i}') for p, count in [('Vbis', 8), ('retimer', 8), ('Pciesw', 4), ('Hysw', 7)] for i in range(count)]
HEADERS = ['节点名', 'BMC_IP', '离线核对', '原总表SN', '来源表SN', '现场SystemSN',
           '现场ChassisSN（不替代节点SN）', '现场核验', '固件差异', '说明', '采集时间（UTC+08）',
           '总表工作表', '总表行号', '来源工作表', '来源行号']


def has_sn(value):
    return str(value).strip().lower() not in PLACEHOLDERS


def node_address(node):
    node = str(node).strip().lower()
    match = re.fullmatch(r'([a-hjkmn])(\d{2})r([1-4])n(\d{2})', node)
    if not match:
        raise ValueError('无效节点名：' + node)
    letter, cabinet, rack, position = match.groups()
    if not 1 <= int(cabinet) <= 16 or not 1 <= int(position) <= 20:
        raise ValueError('节点柜号或位序超出范围：' + node)
    return f'20.{CIRCLES.index(letter)+1}.{int(cabinet)}.{20*(int(rack)-1)+int(position)}'


def _key(value):
    return re.sub(r'[\s_\-（）()]', '', str(value)).lower()


@dataclass
class Record:
    node: str
    sn: str
    ip: str
    sheet: str
    row: int
    fields: dict = field(default_factory=dict)


@dataclass
class Plan:
    node: str
    ip: str
    baseline: Record | None
    source: Record
    status: str
    notes: list[str] = field(default_factory=list)
    address_error: bool = False


def read_records(filename):
    book = InputBook(str(filename))
    records = {}
    for sheet in book.sheets:
        rows = book.read_sheet(sheet)
        nonempty = [(n, v) for n, v in rows if any(str(c).strip() for c in v)]
        if not nonempty:
            continue
        header_row, headers = nonempty[0]
        aliases = {_key(h): i for i, h in enumerate(headers)}
        def column(*names):
            return next((aliases[_key(n)] for n in names if _key(n) in aliases), None)
        ni = column('节点名', '完整节点', '节点名称', 'node', 'nodename')
        si = column('SN', 'SerialNumber', '来源SN', '来源表SN')
        ii = column('BMC_IP', 'BMC IP', 'BMC地址', 'BMC IP / 节点名')
        if ni is None or si is None:
            raise ValueError(f'工作表“{sheet}”缺少节点名或SN列（首个非空行须为表头）')
        for rn, values in nonempty[1:]:
            def cell(index):
                return str(values[index]).strip() if index is not None and index < len(values) else ''
            node, sn, ip = cell(ni).lower(), cell(si), cell(ii)
            if not node:
                raise ValueError(f'{sheet}第{rn}行有数据但缺少节点名')
            node_address(node)
            if re.fullmatch(r'[+-]?\d+(?:\.\d+)?[eE][+-]?\d+', sn):
                raise ValueError(f'{sheet}第{rn}行SN为科学计数法，不能保证精度，请重新获取原始文本SN')
            if node in records:
                raise ValueError(f'节点{node}重复：{records[node].sheet}第{records[node].row}行、{sheet}第{rn}行')
            records[node] = Record(node, sn, ip, sheet, rn, {h: cell(i) for i, h in enumerate(headers)})
    if not records:
        raise ValueError('输入文件没有有效节点')
    return records


def build_plan(baseline_path, source_path):
    baseline, source = read_records(baseline_path), read_records(source_path)
    counts = Counter(r.sn for r in source.values() if has_sn(r.sn))
    plans = []
    for node, src in source.items():
        old = baseline.get(node)
        status = ('来源SN缺失' if not has_sn(src.sn) else
                  '总表无此节点' if old is None else
                  '待补齐SN' if not has_sn(old.sn) else
                  'SN一致' if src.sn == old.sn else 'SN冲突')
        mapped = node_address(node)
        notes, error = [], False
        # Existing BMC_IP is authoritative for connection, but discrepancies
        # with either sheet or the project mapping block automatic collection.
        ip = (old.ip if old else '') or src.ip or mapped
        try:
            origin = normalize_target(ip)
            if origin != normalize_target(mapped):
                notes.append('BMC_IP与节点名推算不一致，已阻止现场采集')
                error = True
            if src.ip and normalize_target(src.ip) != origin:
                notes.append('来源表与总表BMC_IP不一致，已阻止现场采集')
                error = True
        except BmcError:
            notes.append('BMC_IP格式无效，已阻止现场采集')
            error = True
            ip = '[无效地址，未展示原值]'
        if counts[src.sn] > 1:
            notes.append('来源表同一SN对应多个节点，需核实')
        if not old or not old.ip:
            notes.append('BMC_IP按节点规则推算，尚未现场验证')
        plans.append(Plan(node, ip, old, src, status, notes, error))
    targets = Counter(p.ip for p in plans)
    for plan in plans:
        if targets[plan.ip] > 1:
            plan.address_error = True
            plan.notes.append('多个节点指向相同BMC地址，已阻止现场采集')
    return plans


def select_plans(plans, scope='all', nodes=()):
    if scope not in SCOPES:
        raise ValueError('未知核验范围')
    selected = [p for p in plans if SCOPES[scope] is None or p.status == SCOPES[scope]]
    if nodes:
        names = {n.strip().lower() for n in nodes}
        unknown = names - {p.node for p in selected}
        if unknown:
            raise ValueError('节点不在当前来源表/核验范围中：' + '、'.join(sorted(unknown)))
        selected = [p for p in selected if p.node in names]
    return selected


def compare_live(plan, actual):
    if not has_sn(actual):
        return '现场SN缺失或占位'
    old = plan.baseline.sn if plan.baseline else ''
    if actual == plan.source.sn:
        if not plan.baseline:
            return '新增节点SN已验证'
        if not has_sn(old):
            return '补录SN已验证'
        return '三方一致' if actual == old else '与来源一致（总表冲突）'
    if has_sn(old) and actual == old:
        return '与总表一致（来源不符）'
    return '与两表均不一致'


def verify_one(plan, username, password, stop, timeout=10, allow_self_signed=False,
               auth='basic', firmware=False, client_class=BmcClient):
    result = {'node': plan.node, 'ip': plan.ip, 'system_sn': '', 'chassis_sn': '',
              'status': '读取失败', 'notes': [], 'when': stamp(), 'firmware': {}, 'evidence': []}
    if plan.address_error:
        result.update(status='地址异常，未采集')
        return result
    if stop.is_set():
        result.update(status='已取消')
        return result
    client = None
    def get(path, fields):
        if stop.is_set():
            raise InterruptedError('已停止后续请求')
        try:
            data, _ = client.request('GET', path)
        except BmcError as exc:
            result['evidence'].append({'path': path, 'error': str(exc)})
            raise
        result['evidence'].append({'path': path,
                                   'fields': {k: data.get(k) for k in fields if k in data} if isinstance(data, dict) else {},
                                   'response_type': type(data).__name__})
        if not isinstance(data, dict):
            raise BmcError('Redfish资源不是JSON对象：' + path)
        return data
    try:
        if not username or not password:
            raise BmcError('缺少账号或密码')
        client = client_class(plan.ip, timeout, allow_self_signed)
        client.login(username, password, auth)
        root = client.root
        link = root.get('Systems')
        collection_path = link.get('@odata.id') if isinstance(link, dict) else None
        collection_path = collection_path or '/redfish/v1/Systems'
        collection = get(collection_path, ['Members@odata.count'])
        members = collection.get('Members')
        if (not isinstance(members, list) or len(members) != 1 or not isinstance(members[0], dict)
                or collection.get('Members@odata.nextLink') or collection.get('@odata.nextLink')
                or collection.get('Members@odata.count') not in (None, 1, '1')):
            raise BmcError('Systems清单不是唯一资源，不能确认节点身份；未任意选择Systems/1')
        system_path = members[0].get('@odata.id')
        if not isinstance(system_path, str) or not system_path:
            raise BmcError('Systems清单缺少资源链接')
        system = get(system_path, ['Id', 'SerialNumber', 'Model', 'Manufacturer', 'BiosVersion'])
        raw_sn = system.get('SerialNumber')
        result['system_sn'] = serial_text(raw_sn)
        if isinstance(raw_sn, (bool, float, dict, list)):
            result['notes'].append('SerialNumber类型异常，未作为有效SN')
        result['status'] = compare_live(plan, result['system_sn'])
        old = plan.baseline.sn if plan.baseline else ''
        actual = result['system_sn']
        if actual and old != actual and (old in (actual+'A', actual+'B') or actual in (old+'A', old+'B')):
            result['notes'].append('原SN与现场SN仅尾缀A/B不同；仍按不同SN处理，不自动去尾缀')
        # A chassis identifier is a separate component and never a SN fallback.
        links = system.get('Links')
        chassis = links.get('Chassis') if isinstance(links, dict) else None
        if isinstance(chassis, list) and len(chassis) == 1 and isinstance(chassis[0], dict):
            chassis_path = chassis[0].get('@odata.id')
            if isinstance(chassis_path, str) and chassis_path:
                try:
                    data = get(chassis_path, ['Id', 'SerialNumber', 'Model'])
                    result['chassis_sn'] = serial_text(data.get('SerialNumber'))
                except BmcError as exc:
                    result['notes'].append('Chassis读取失败（不影响SystemSN比对）：' + str(exc))
        if firmware:
            for name, endpoint in FIRMWARE:
                path = '/redfish/v1/UpdateService/FirmwareInventory/' + endpoint
                try:
                    data = get(path, ['Id', 'Name', 'Version', 'Status'])
                    result['firmware'][name] = serial_text(data.get('Version')) or 'NA'
                except ApiError as exc:
                    result['firmware'][name] = 'NA'
                    result['notes'].append(f'{name}: HTTP {exc.status}')
                    if exc.status in (401, 403, 429, 503):
                        result['notes'].append('停止后续固件请求；SN比对结果保留')
                        break
                except BmcError as exc:
                    result['firmware'][name] = 'NA'
                    result['notes'].append(f'{name}: {exc}')
                    break
    except InterruptedError:
        result['status'] = '已取消（保留已读取字段）'
    except BmcError as exc:
        result['notes'].append(str(exc))
    except Exception as exc:
        result['notes'].append('未预期异常：' + type(exc).__name__)
    finally:
        if client is not None:
            warning = client.close()
            if warning:
                result['notes'].append(warning)
    return result


def run_live(plans, username, password, stop, workers=4, callback=None, **options):
    if not 1 <= workers <= 16:
        raise ValueError('并发数须为1～16')
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(verify_one, p, username, password, stop, **options): p for p in plans}
        for future in as_completed(futures):
            value = future.result()
            results[value['node']] = value
            if callback:
                callback(value)
    return results


def report_data(plans, results=None):
    results = results or {}
    include_firmware = any(r['firmware'] for r in results.values())
    headers = HEADERS + ([name for name, _ in FIRMWARE] if include_firmware else [])
    rows = []
    for p in plans:
        r = results.get(p.node, {})
        differences = []
        for name, value in r.get('firmware', {}).items():
            original = p.baseline.fields.get(name, '') if p.baseline else ''
            if serial_text(original) and serial_text(value) and original != value:
                differences.append(f'{name}: {original} → {value}')
        row = [p.node, p.ip, p.status, p.baseline.sn if p.baseline else '', p.source.sn,
               r.get('system_sn', ''), r.get('chassis_sn', ''), r.get('status', '未采集'),
               '；'.join(differences), '；'.join(p.notes + r.get('notes', [])), r.get('when', ''),
               p.baseline.sheet if p.baseline else '', str(p.baseline.row) if p.baseline else '',
               p.source.sheet, str(p.source.row)]
        if include_firmware:
            row += [r.get('firmware', {}).get(name, '未采集') for name, _ in FIRMWARE]
        rows.append(row)
    return headers, rows


def validate_output(filename, inputs=(), live=False):
    path = Path(filename).resolve()
    if path.suffix.lower() != '.xlsx':
        raise ValueError('报告文件须为.xlsx')
    if path in {Path(p).resolve() for p in inputs}:
        raise ValueError('不能将报告覆盖到原总表或来源表')
    evidence_path = path.with_name(path.stem + '_现场证据.jsonl')
    if path.exists() or (live and evidence_path.exists()):
        raise ValueError('输出文件已存在，请换用新文件名')
    if not path.parent.is_dir():
        raise ValueError('输出目录不存在')
    return path, evidence_path


def export_report(filename, plans, results=None, inputs=()):
    results = {p.node: results[p.node] for p in plans if results and p.node in results}
    path, evidence_path = validate_output(filename, inputs, bool(results))
    headers, rows = report_data(plans, results)
    write_xlsx(str(path), headers, rows)
    if results:
        with evidence_path.open('x', encoding='utf-8') as f:
            for p in plans:
                if p.node in results:
                    # Whitelisted response fields only, never request headers,
                    # usernames, passwords, cookies, tokens, or complete JSON.
                    f.write(json.dumps(results[p.node], ensure_ascii=False) + '\n')
    return path


def parser():
    baseline, source = default_inputs()
    p = argparse.ArgumentParser(description='节点BMC信息核验；默认仅离线对照，不自动连接设备或回写原表。')
    p.add_argument('--gui', action='store_true', help='启动图形界面')
    p.add_argument('--baseline', type=Path, default=baseline)
    p.add_argument('--source', type=Path, default=source)
    p.add_argument('--scope', choices=SCOPES, default='all')
    p.add_argument('--node', action='append', default=[], help='仅处理指定来源节点，可多次指定')
    p.add_argument('--output', type=Path)
    p.add_argument('--live', action='store_true', help='显式启用现场只读采集')
    p.add_argument('--limit', type=int, default=1, help='现场采集数量，默认1；0表示当前范围全部')
    p.add_argument('--username', help='优先于BMC_USERNAME环境变量和本目录.env；均未设置时交互输入')
    p.add_argument('--auth', choices=['basic', 'session'], default='basic')
    p.add_argument('--timeout', type=int, default=10)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--allow-self-signed', action='store_true')
    p.add_argument('--firmware', action='store_true', help='额外采集参考总表中的38个固件字段')
    return p


def main(argv=None):
    import sys
    args = parser().parse_args(argv)
    if args.gui or (argv is None and len(sys.argv) == 1):
        from verify_gui import launch
        launch(args)
        return 0
    if args.limit < 0 or not 1 <= args.workers <= 16 or not 1 <= args.timeout <= 120:
        raise ValueError('limit不能为负；workers须1～16；timeout须1～120秒')
    plans = build_plan(args.baseline, args.source)
    print('全部来源节点离线核对：', dict(Counter(p.status for p in plans)))
    selected = select_plans(plans, args.scope, args.node)
    if not selected:
        raise ValueError('当前核验范围没有节点')
    if args.output:
        validate_output(args.output, [args.baseline, args.source], args.live)
    results = {}
    if args.live:
        selected = selected[:args.limit] if args.limit else selected
        print(f'现场核验：{len(selected)}个节点；认证{args.auth}；不回写原表。')
        configured_user, configured_password = load_credentials(args.username)
        username = configured_user or input('BMC账号：').strip()
        password = configured_password or getpass.getpass('BMC密码（不回显）：')
        configured_password = ''
        try:
            results = run_live(selected, username, password, threading.Event(), workers=args.workers,
                               timeout=args.timeout, allow_self_signed=args.allow_self_signed,
                               auth=args.auth, firmware=args.firmware)
        finally:
            password = ''
        print('现场结果：', dict(Counter(r['status'] for r in results.values())))
    if args.output:
        output = export_report(args.output, selected, results, [args.baseline, args.source])
        print('报告已保存：', output)
    else:
        headers, rows = report_data(selected, results)
        for row in rows[:10]:
            print(' | '.join(row[:8]))
        print(f'共{len(rows)}行；用--output保存完整Excel报告。')
    return 1 if any(r['status'] in ('读取失败', '地址异常，未采集') or r['status'].startswith('已取消') for r in results.values()) else 0
