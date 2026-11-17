"""Merge observed firmware values and naturally sort a 41-column inventory.

Keep latest-source SNs authoritative and preserve existing values when a live
field is unavailable, failed, absent, or an all-ones hexadecimal sentinel.
"""
from __future__ import annotations
from collections import Counter
import csv
import hashlib
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZipFile

from node_verify import CIRCLES, FIRMWARE, read_records
from xlsx_io import InputBook

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def natural_key(node):
    match = re.fullmatch(r'([a-hjkmn])(\d{2})r([1-4])n(\d{2})', node.strip().lower())
    if not match:
        raise ValueError('无法排序的节点名：'+node)
    circle, cabinet, rack, position = match.groups()
    return CIRCLES.index(circle), int(cabinet), int(rack), int(position)


def usable_firmware(value, status=None):
    if value is None or isinstance(value, (bool, dict, list)):
        return False
    text = str(value).strip()
    if text.lower() in ('', 'na', 'n/a', 'none', 'null', 'unknown', '-', 'error', 'unsupported', '未采集'):
        return False
    if re.fullmatch(r'v?0x[f]{4,}', text, re.IGNORECASE):
        return False
    if isinstance(status, dict) and str(status.get('State', '')).lower() in ('absent', 'notpresent'):
        return False
    return True


def field_status(result, endpoint):
    suffix = '/UpdateService/FirmwareInventory/'+endpoint
    for item in result.get('evidence', []):
        if item.get('path', '').endswith(suffix):
            fields = item.get('fields', {})
            if isinstance(fields, dict):
                return fields.get('Status')
    return None


def merge_sorted(master, latest_source, results, output):
    master, latest_source, output = map(Path, (master, latest_source, output))
    if output.resolve() in {master.resolve(), latest_source.resolve()} or output.exists():
        raise ValueError('合并输出必须使用新的文件名，不能覆盖输入')
    before_hash = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (master, latest_source)}
    book = InputBook(str(master))
    if len(book.sheets) != 1:
        raise ValueError('合并器要求单工作表的41列固件总表')
    sheet, part = next(iter(book.sheets.items()))
    original_rows = book.read_sheet(sheet)
    headers = original_rows[0][1]
    expected = ['节点名', 'BMC_IP', 'SN']+[n for n, _ in FIRMWARE]
    if headers != expected:
        raise ValueError('总表列结构不是预期的41列')
    firmware_columns = {name: (i+3, endpoint) for i, (name, endpoint) in enumerate(FIRMWARE)}
    latest = read_records(latest_source)
    original = {}
    with ZipFile(master) as z:
        raw = z.read(part)
    root = ET.fromstring(raw)
    for feature in ('.//m:f', 'm:mergeCells', 'm:hyperlinks', 'm:conditionalFormatting'):
        if root.find(feature, NS) is not None:
            raise ValueError('总表有公式或位置关联对象，不能直接移动整行')
    raw_rows = re.findall(rb'<row\b[^>]*(?:/>|>.*?</row>)', raw, re.DOTALL)
    if len(raw_rows) != len(original_rows):
        raise ValueError('工作表行数据无法完整定位')
    raw_map = {int(re.search(rb'\br="(\d+)"', r).group(1)): r for r in raw_rows}
    for rn, values in original_rows[1:]:
        values = list(values)+['']*(len(headers)-len(values))
        if len(values) != 41 or not values[0]:
            raise ValueError('总表存在非标准数据行')
        node = values[0].strip().lower()
        if node in original:
            raise ValueError('总表节点重复：'+node)
        original[node] = (rn, values)
    if not set(latest).issubset(original):
        raise ValueError('最新来源节点尚未全部进入合并总表')
    changes, missing, counts = [], [], Counter()
    ordered = sorted(original, key=natural_key)
    cell_pattern = rb'<c\b[^>]*\br="([A-Z]+)(\d+)"[^>]*(?:/>|>.*?</c>)'
    output_rows = [raw_map[original_rows[0][0]]]
    expected_values = {}
    for new_rn, node in enumerate(ordered, 2):
        old_rn, old = original[node]
        values = list(old)
        live = results.get(node, {})
        if node in latest:
            values[2] = latest[node].sn
            if values[2] != old[2]:
                changes.append([node, 'SN', old[2], values[2], '最新补录表', old_rn, new_rn])
                counts['latest_sn_changes'] += 1
        for name, (index, endpoint) in firmware_columns.items():
            value = live.get('firmware', {}).get(name)
            status = field_status(live, endpoint)
            if usable_firmware(value, status):
                text = str(value).strip()
                values[index] = text
                counts['usable_live_firmware_fields'] += 1
                if old[index] != text:
                    kind = '补齐原空值' if not usable_firmware(old[index]) else '更新已有版本'
                    changes.append([node, name, old[index], text, '现场Redfish：'+kind, old_rn, new_rn])
                    counts['firmware_fields_changed'] += 1
                    counts['filled_empty_fields' if kind == '补齐原空值' else 'updated_existing_fields'] += 1
                else:
                    counts['firmware_fields_unchanged'] += 1
            elif node in latest:
                reason = ('原值保留；现场未取得有效版本' if usable_firmware(old[index]) else '仍缺有效版本')
                missing.append([node, name, '' if value is None else str(value), old[index], reason,
                                live.get('status', '未采集'), '；'.join(live.get('notes', [])), new_rn])
                counts['unavailable_live_firmware_fields'] += 1
                if not usable_firmware(old[index]):
                    counts['remaining_missing_source_firmware_fields'] += 1
        modifications = {index: new for index, new in enumerate(values) if new != old[index]}
        patched_indices = set()
        def patch_cell(match):
            col = match.group(1).decode()
            idx = 0
            for c in col:
                idx = idx*26+ord(c)-64
            idx -= 1
            if idx not in modifications:
                return match.group(0)
            head = match.group(0).split(b'>', 1)[0]
            head = re.sub(rb'\s+t="[^"]*"', b'', head).removesuffix(b'/')
            patched_indices.add(idx)
            return head+b' t="inlineStr"><is><t>'+escape(modifications[idx]).encode()+b'</t></is></c>'
        row = re.sub(cell_pattern, patch_cell, raw_map[old_rn])
        if patched_indices != set(modifications):
            raise ValueError('待更新的单元格未全部定位：'+node)
        row, changed = re.subn(rb'(<row\b[^>]*\br=")\d+(")', lambda m:m.group(1)+str(new_rn).encode()+m.group(2), row, count=1)
        if changed != 1:
            raise ValueError('无法更新行号')
        row = re.sub(rb'(\br=")([A-Z]+)\d+(")', lambda m:m.group(1)+m.group(2)+str(new_rn).encode()+m.group(3), row)
        output_rows.append(row)
        expected_values[node] = values
    new_last = len(ordered)+1
    data = re.search(rb'(<sheetData\b[^>]*>)(.*?)(</sheetData>)', raw, re.DOTALL)
    if not data:
        raise ValueError('工作表数据区缺失')
    updated = raw[:data.start(2)]+b''.join(output_rows)+raw[data.end(2):]
    updated, dimension_count = re.subn(rb'(<dimension\b[^>]*\bref=")[^"]+(")', lambda m:m.group(1)+f'A1:AO{new_last}'.encode()+m.group(2), updated, count=1)
    if dimension_count != 1:
        raise ValueError('工作表范围缺失')
    def sort_range(match):
        return re.sub(rb'(\bref="\$?[A-Z]+\$?\d+:\$?[A-Z]+\$?)\d+(")', lambda m:m.group(1)+str(new_last).encode()+m.group(2), match.group(0), count=1)
    updated = re.sub(rb'<sort(?:State|Condition)\b[^>]*>', sort_range, updated)
    ET.fromstring(updated)
    stage = output.with_name(output.name+'.merge-stage')
    if stage.exists():
        raise ValueError('合并临时文件已存在')
    try:
        with ZipFile(master) as before, ZipFile(stage, 'x') as after:
            after.comment = before.comment
            for info in before.infolist():
                after.writestr(info, updated if info.filename == part else before.read(info.filename))
        with ZipFile(stage) as z:
            if z.testzip() is not None:
                raise ValueError('合并输出ZIP损坏')
            verify_root = ET.fromstring(z.read(part))
            shared = book.shared
            parsed_values = []
            for row in verify_root.findall('m:sheetData/m:row', NS):
                cells = []
                for c in row:
                    value = c.find('m:v', NS)
                    if c.get('t') == 'inlineStr':
                        text = ''.join(t.text or '' for t in c.findall('.//m:t', NS))
                    elif c.get('t') == 's':
                        text = shared[int(value.text)]
                    else:
                        text = value.text if value is not None and value.text else ''
                    cells.append(text)
                parsed_values.append(cells)
            if parsed_values[0] != headers or [r[0].lower() for r in parsed_values[1:]] != ordered:
                raise ValueError('排序或表头验证失败')
            for values in parsed_values[1:]:
                if values != expected_values[values[0].lower()]:
                    raise ValueError('逐单元格验证失败：'+values[0])
            for node, rec in latest.items():
                if expected_values[node][2] != rec.sn:
                    raise ValueError('最新SN未保留')
        if any(hashlib.sha256(p.read_bytes()).hexdigest() != digest for p, digest in before_hash.items()):
            raise ValueError('输入表在合并期间发生变化')
        stage.replace(output)
    finally:
        if stage.exists():
            stage.unlink()
    changes_path = output.with_name(output.stem+'_更新明细.csv')
    missing_path = output.with_name(output.stem+'_未取得有效版本字段.csv')
    for path, headings, rows in [
        (changes_path, ['节点名','字段','合并前值','合并后值','依据','原行号','排序后行号'], changes),
        (missing_path, ['节点名','字段','现场原始值','保留的原表值','处理说明','SN采集状态','采集说明','排序后行号'], missing),
    ]:
        with path.open('x', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f); writer.writerow(headings); writer.writerows(rows)
    return {'output': str(output), 'total_nodes': len(ordered), 'source_nodes': len(latest),
            'counts': dict(counts), 'changes_csv': str(changes_path), 'missing_fields_csv': str(missing_path),
            'sort': 'circle,cabinet,rack,node', 'latest_source_sn_preserved': True,
            'original_inputs_unchanged': True}
