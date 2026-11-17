"""Text-only exports; never execute spreadsheet formulas or replace old reports."""
from pathlib import Path
import csv
import json
import os
import shutil
import tempfile
from xlsx_io import write_xlsx
from node_verify import FIRMWARE

def json_text(value):
    return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)

def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        f.write(json_text(value)+'\n');f.flush();os.fsync(f.fileno())

def export_ledger(path,items):
    path=Path(path).expanduser().resolve()
    if path.suffix.lower() not in ('.xlsx','.csv','.json'): raise ValueError('导出文件须为 .xlsx/.csv/.json')
    if path.exists(): raise ValueError('输出文件已存在；请换用新文件名')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.suffix.lower()=='.json':
        write_json(path,{'format':'bmc-cli-ledger-v1','items':items});return
    fields=[n for n,_ in FIRMWARE]
    headers=['节点名','BMC_IP','模式','机型','电源','SN','现场SystemSN','ChassisSN','SN状态','位置','标签','资产状态','备注','采集状态','CPU数量','CPU型号','内存GiB','最近采集时间']+[n+'（现场历史）' for n in fields]+fields
    rows=[]
    for item in items:
        m,d=item['metadata'],item['data']
        rows.append([item['name'],item['address'],item['mode'],item['model'],item['power'],m['declared_sn'],d.get('system_sn',''),d.get('chassis_sn',''),item['sn_state'],m['location'],m['tags'],m['lifecycle'],m['notes'],item['collection_state'],d.get('cpu_count',''),d.get('cpu_model',''),d.get('memory_gib',''),d.get('last_attempt','')]+[d.get('firmware',{}).get(n,'') for n in fields]+[d.get('reference_firmware',{}).get(n,'') for n in fields])
    # Write to a private temporary file, then create the requested path exclusively.
    with tempfile.TemporaryDirectory(prefix='bmc_export_',dir=path.parent) as folder:
        temporary=Path(folder)/('export'+path.suffix.lower())
        if path.suffix.lower()=='.xlsx': write_xlsx(str(temporary),headers,rows)
        else:
            # CSV has no text-cell type: guard spreadsheet formula interpretation.
            def safe(v):
                text='' if v is None else str(v)
                return "'"+text if text.lstrip().startswith(('=','+','-','@','\t','\r')) else text
            with temporary.open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f);writer.writerow(headers);writer.writerows([[safe(v) for v in row] for row in rows])
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as dest,temporary.open('rb') as src:
            shutil.copyfileobj(src,dest);dest.flush();os.fsync(dest.fileno())
