"""Dependency-free multi-sheet DIMM report. Never invent zeros for failed nodes."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import math
import os
import shutil
import tempfile
import zipfile
from xml.etree import ElementTree as ET
from xlsx_io import write_xlsx,NS,REL

CT='http://schemas.openxmlformats.org/package/2006/content-types'
PKG='http://schemas.openxmlformats.org/package/2006/relationships'

def workbook(path,sheets):
    path=Path(path)
    if path.exists():raise FileExistsError('内存报告已存在，请使用新文件名')
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dimm_report_',dir=path.parent) as tmp:
        tmp=Path(tmp);parts={}
        types=ET.Element('{'+CT+'}Types')
        ET.SubElement(types,'{'+CT+'}Default',Extension='rels',ContentType='application/vnd.openxmlformats-package.relationships+xml')
        ET.SubElement(types,'{'+CT+'}Default',Extension='xml',ContentType='application/xml')
        for part,kind in (('/xl/workbook.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'),('/xl/styles.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')):
            ET.SubElement(types,'{'+CT+'}Override',PartName=part,ContentType=kind)
        book=ET.Element('{'+NS+'}workbook');tab=ET.SubElement(book,'{'+NS+'}sheets');rels=ET.Element('{'+PKG+'}Relationships')
        for number,(name,headers,rows) in enumerate(sheets,1):
            one=tmp/f'{number}.xlsx';write_xlsx(str(one),headers,rows)
            with zipfile.ZipFile(one) as z:
                xml=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                # Real counts/capacities are numeric; identifiers and missing values stay text.
                for element,values in zip(xml.find('{'+NS+'}sheetData').findall('{'+NS+'}row')[1:],rows):
                    for cell,value in zip(element.findall('{'+NS+'}c'),values):
                        if type(value) in (int,float) and math.isfinite(value):
                            cell.attrib.pop('t',None)
                            for child in list(cell):cell.remove(child)
                            ET.SubElement(cell,'{'+NS+'}v').text=str(value)
                parts[f'xl/worksheets/sheet{number}.xml']=ET.tostring(xml,encoding='utf-8',xml_declaration=True)
                if number==1:
                    parts['xl/styles.xml']=z.read('xl/styles.xml');parts['_rels/.rels']=z.read('_rels/.rels')
            ET.SubElement(tab,'{'+NS+'}sheet',name=name,sheetId=str(number),attrib={'{'+REL+'}id':'rId'+str(number)})
            ET.SubElement(rels,'{'+PKG+'}Relationship',Id='rId'+str(number),Type=REL+'/worksheet',Target=f'worksheets/sheet{number}.xml')
            ET.SubElement(types,'{'+CT+'}Override',PartName=f'/xl/worksheets/sheet{number}.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
        ET.SubElement(rels,'{'+PKG+'}Relationship',Id='rIdStyles',Type=REL+'/styles',Target='styles.xml')
        for name,element in (('[Content_Types].xml',types),('xl/workbook.xml',book),('xl/_rels/workbook.xml.rels',rels)):
            parts[name]=ET.tostring(element,encoding='utf-8',xml_declaration=True)
        payload=tmp/'report.xlsx'
        with zipfile.ZipFile(payload,'w',compression=zipfile.ZIP_DEFLATED) as z:
            for name,value in parts.items():z.writestr(name,value)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as dest,payload.open('rb') as source:
            shutil.copyfileobj(source,dest);dest.flush();os.fsync(dest.fileno())

def export_memory_report(path,results):
    headers=['节点名','BMC_IP','采集模式','采集结果','配置结论','32GB条数','64GB条数','其他容量条数','已确认容量DIMM数','明确空槽数','容量未知/未读项数','集合声明成员数','已枚举DIMM数','DIMM容量合计GiB','System总内存GiB','容量合计核对','机型','主机电源','采集时间（北京时间）','说明']
    detail_headers=['节点名','BMC_IP','插槽/资源ID','在位判断','容量MiB','容量GiB','厂商','料号','内存条SN','内存类型','运行速率MHz','RankCount','数据位宽','模块类型','CPU Socket','Controller','Channel','Slot','State','Health','资源路径','说明']
    rows=[];details=[]
    for r in results:
        m=r.get('memory',{});s=m.get('summary',{});dimms=m.get('dimms',[])
        def value(key):return s.get(key) if s.get(key) is not None else '未取得'
        unknown=value('unknown_capacity') if dimms else '未取得'
        if s.get('unbounded_unknown'):unknown='至少'+str(s.get('unknown_capacity',0))+'项（集合不完整）'
        observed=r.get('observed');when=datetime.fromtimestamp(observed,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') if observed else '未执行'
        notes=[r.get('error',''),*r.get('notes',[]),*m.get('notes',[])]
        rows.append([r['node'],r['address'],r.get('mode','真实只读'),r.get('status','未取得'),s.get('conclusion','待采集/待核验，不能判断32GB或64GB'),value('count_32gb'),value('count_64gb'),value('count_other'),value('present_known') if dimms else '未取得',value('empty_slots') if dimms else '未取得',unknown,value('members_expected'),value('members_discovered') if dimms else '未取得',value('dimm_total_gib'),s.get('system_total_gib',r.get('memory_gib','未取得')),s.get('total_comparison','无法核对'),r.get('model','未取得'),r.get('power','未取得'),when,'；'.join(n for n in notes if n)])
        for d in dimms:
            location=d.get('location',{})
            details.append([r['node'],r['address'],d.get('DeviceLocator') or d.get('Id') or d['path'].rsplit('/',1)[-1],{'present':'有容量/在位','absent':'明确不在位','unknown':'待核验'}.get(d.get('presence'),'待核验'),d.get('capacity_mib') if d.get('capacity_mib') is not None else '',d.get('capacity_gib') if d.get('capacity_gib') is not None else '',d.get('Manufacturer',''),d.get('PartNumber',''),d.get('SerialNumber',''),d.get('MemoryDeviceType',''),d.get('OperatingSpeedMhz',''),d.get('RankCount',''),d.get('DataWidthBits',''),d.get('BaseModuleType',''),location.get('Socket',''),location.get('Controller',''),location.get('Channel',''),location.get('Slot',''),d.get('state',''),d.get('health',''),d.get('path',''),d.get('note','')])
    instructions=[['统计范围',f'{len(results)}个节点，按输入顺序逐节点保留；不回写源表'],['容量换算','CapacityMiB=32768/65536 对应32/64 GiB，现场习惯称32/64GB；不除以1000'],['缺失处理','未取得DIMM信息时显示未取得/待核验，不填0根或推测配置'],['空槽处理','只有明确State=Absent/NotPresent时统计空槽；0容量但未明确不在位标为待核验'],['完整性','分页/成员总数/重复链接/读取失败均影响完整性；部分64GB不能推断整机全64GB'],['容量合计','DIMM物理容量合计与System总容量分别保留；不一致时提示核验，不自动修改容量'],['时间与来源','采集时间不是硬件最近变更时间；配置结论以本次BMC返回为准'],['安全','仅读取资源，不关机、上电、上传或激活；无密码/Token/Cookie/AUTHKEY字段']]
    workbook(path,[('节点汇总',headers,rows),('内存条明细',detail_headers,details),('统计说明',['项目','说明'],instructions)])
