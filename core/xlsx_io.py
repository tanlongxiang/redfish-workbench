"""Small, dependency-free XLSX reader/writer for BMC inventory tables.

Only cell values are read. Macros, external links and formulas are not executed.
Output identifiers are explicit text cells, including 16+ digit serial numbers.
"""
from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
XML = 'http://www.w3.org/XML/1998/namespace'
MAX_FILE = 40 * 1024 * 1024
MAX_XML = 40 * 1024 * 1024
MAX_TOTAL = 160 * 1024 * 1024
MAX_ROWS = 100000


def col_name(index: int) -> str:
    result = ''
    while index >= 0:
        index, tail = divmod(index, 26)
        result = chr(65 + tail) + result
        index -= 1
    return result


def col_index(reference: str) -> int:
    match = re.match(r'^([A-Z]+)', reference.upper())
    if not match:
        raise ValueError('Excel 单元格坐标无效')
    value = 0
    for char in match[1]:
        value = value * 26 + ord(char) - 64
    if value > 16384:
        raise ValueError('Excel 列数超限')
    return value - 1


def _text(value) -> str:
    # XML 1.0 disallows most control characters.
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', '' if value is None else str(value))


class InputBook:
    def __init__(self, filename: str):
        self.path = Path(filename)
        if self.path.stat().st_size > MAX_FILE:
            raise ValueError('输入文件超过 40 MB，请拆分后导入')
        self.sheets: dict[str, str] = {}
        self.shared: list[str] = []
        self.csv_rows = None
        if self.path.suffix.lower() == '.csv':
            self._read_csv()
            self.sheets = {'CSV': ''}
            return
        if self.path.suffix.lower() != '.xlsx':
            raise ValueError('仅支持 .xlsx 和 .csv；旧版 .xls 请先另存为 .xlsx')
        with zipfile.ZipFile(self.path) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_TOTAL:
                raise ValueError('Excel 解压后数据过大，请拆分文件')
            root = self._xml(archive, 'xl/workbook.xml')
            relationships = self._xml(archive, 'xl/_rels/workbook.xml.rels')
            mapping = {}
            for item in relationships:
                if item.get('TargetMode') == 'External':
                    continue
                target = item.get('Target', '')
                path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
                if not path.startswith('xl/'):
                    continue
                mapping[item.get('Id')] = path
                if item.get('Type', '').endswith('/sharedStrings'):
                    shared = self._xml(archive, path)
                    self.shared = [''.join(t.text or '' for t in si.iter(f'{{{NS}}}t')) for si in shared]
            for item in root.findall(f'{{{NS}}}sheets/{{{NS}}}sheet'):
                path = mapping.get(item.get(f'{{{REL}}}id'))
                if path:
                    self.sheets[item.get('name', 'Sheet')] = path
        if not self.sheets:
            raise ValueError('Excel 中没有可读取的工作表')

    @staticmethod
    def _xml(archive, name):
        if archive.getinfo(name).file_size > MAX_XML:
            raise ValueError('工作表数据过大，请拆分文件')
        return ET.fromstring(archive.read(name))

    def _read_csv(self):
        raw = self.path.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('gb18030')
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        if len(rows) > MAX_ROWS:
            raise ValueError('输入记录超过 100000 行，请拆分文件')
        self.csv_rows = [(number, row) for number, row in enumerate(rows, 1)]

    def read_sheet(self, name: str) -> list[tuple[int, list[str]]]:
        if self.csv_rows is not None:
            return self.csv_rows
        with zipfile.ZipFile(self.path) as archive:
            root = self._xml(archive, self.sheets[name])
        result = []
        for fallback, row in enumerate(root.findall(f'{{{NS}}}sheetData/{{{NS}}}row'), 1):
            row_number = int(row.get('r', fallback))
            values = {}
            for fallback_col, cell in enumerate(row.findall(f'{{{NS}}}c')):
                index = col_index(cell.get('r', col_name(fallback_col)))
                kind = cell.get('t')
                value = cell.find(f'{{{NS}}}v')
                if kind == 'inlineStr':
                    text = ''.join(x.text or '' for x in cell.iter(f'{{{NS}}}t'))
                elif kind == 's' and value is not None:
                    try:
                        text = self.shared[int(value.text)]
                    except (IndexError, ValueError, TypeError):
                        raise ValueError(f'Excel 第 {row_number} 行共享字符串无效') from None
                elif cell.find(f'{{{NS}}}f') is not None and value is None:
                    text = '[公式无缓存，请用 Excel 计算后保存]'
                else:
                    text = value.text if value is not None and value.text is not None else ''
                values[index] = text
            width = max(values, default=-1) + 1
            result.append((row_number, [values.get(i, '') for i in range(width)]))
            if len(result) > MAX_ROWS:
                raise ValueError('输入记录超过 100000 行，请拆分文件')
        return result


def write_xlsx(filename: str, headers: list[str], rows: list[list[str]]) -> None:
    """Write all identifiers as text. No formula-like value can execute."""
    ET.register_namespace('', NS)
    root = ET.Element(f'{{{NS}}}worksheet')
    last = f'{col_name(len(headers)-1)}{len(rows)+1}'
    ET.SubElement(root, f'{{{NS}}}dimension', ref=f'A1:{last}')
    views = ET.SubElement(root, f'{{{NS}}}sheetViews')
    view = ET.SubElement(views, f'{{{NS}}}sheetView', workbookViewId='0')
    ET.SubElement(view, f'{{{NS}}}pane', ySplit='1', topLeftCell='A2', activePane='bottomLeft', state='frozen')
    ET.SubElement(root, f'{{{NS}}}sheetFormatPr', defaultRowHeight='22')
    columns = ET.SubElement(root, f'{{{NS}}}cols')
    widths = [18, 27, 24, 24, 24, 24, 14, 65, 25, 13]
    width_by_header = {'交换机型号': 22, 'IBSwitch1 状态': 22, 'IBSwitch2 状态': 22, '验证通过的账号': 22, '验证通过的密码（明文）': 32, '凭据尝试次数': 18}
    for index in range(len(headers)):
        ET.SubElement(columns, f'{{{NS}}}col', min=str(index+1), max=str(index+1), width=str(width_by_header.get(headers[index], widths[index] if index < len(widths) else 24)), customWidth='1')
    data = ET.SubElement(root, f'{{{NS}}}sheetData')
    for number, values in enumerate([headers] + rows, 1):
        row = ET.SubElement(data, f'{{{NS}}}row', r=str(number), ht='30' if number == 1 else '24', customHeight='1')
        for index, value in enumerate(values):
            style = '1' if number == 1 else ('3' if number % 2 else '2')
            cell = ET.SubElement(row, f'{{{NS}}}c', r=f'{col_name(index)}{number}', s=style, t='inlineStr')
            inline = ET.SubElement(cell, f'{{{NS}}}is')
            text = ET.SubElement(inline, f'{{{NS}}}t', {f'{{{XML}}}space': 'preserve'})
            text.text = _text(value)
    ET.SubElement(root, f'{{{NS}}}autoFilter', ref=f'A1:{last}')
    ET.SubElement(root, f'{{{NS}}}pageMargins', left='0.3', right='0.3', top='0.5', bottom='0.5', header='0.2', footer='0.2')
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/><color rgb="FF243746"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/><color rgb="FFFFFFFF"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF163D58"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF0F5F9"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="49" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="center"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
    types = '''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    package_rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook = f'''<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{NS}" xmlns:r="{REL}"><sheets><sheet name="BMC_SN汇总" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    path = Path(filename)
    temp = path.with_name(path.name + '.tmp')
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in {'[Content_Types].xml': types, '_rels/.rels': package_rels, 'xl/workbook.xml': workbook, 'xl/_rels/workbook.xml.rels': workbook_rels, 'xl/styles.xml': styles, 'xl/worksheets/sheet1.xml': xml}.items():
                archive.writestr(name, content)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()
