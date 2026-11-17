"""Generic IP targets. Site-specific name mapping is opt-in, never a default."""
from pathlib import Path
import re
from firmware_batch import Target, validate_targets, parse_targets as site_parse
from xlsx_io import InputBook

def parse_text(text, *, site_mapping=False):
    if site_mapping: return site_parse(text)
    result=[]
    for line in text.splitlines():
        line=line.strip()
        if not line or line.startswith('#'): continue
        parts=re.split(r'[,;\s]+',line)
        if len(parts)==1:
            name,sep,address=parts[0].partition('=')
            result.append(Target(name,address) if sep else Target(name,name))
        elif len(parts)==2: result.append(Target(parts[0],parts[1]))
        else: raise ValueError('每行使用 IP、名称=IP，或“名称 IP”；不支持 IP 范围和扫描')
    return list(validate_targets(result))

def load_targets(args):
    values=[]
    if args.targets:
        path=Path(args.targets)
        if path.suffix.lower()=='.txt':
            if path.stat().st_size>2*1024*1024: raise ValueError('TXT 清单超过2 MiB')
            values.extend(parse_text(path.read_text(encoding='utf-8-sig'),site_mapping=args.site_mapping))
        else:
            book=InputBook(str(path))
            for sheet in book.sheets:
                rows=[row for _,row in book.read_sheet(sheet) if any(str(v).strip() for v in row)]
                if not rows: continue
                headers=[str(h).strip().lower() for h in rows[0]]
                ni=next((i for i,h in enumerate(headers) if h in ('节点名','节点名称','node','name')),None)
                ii=next((i for i,h in enumerate(headers) if h in ('bmc_ip','bmcip','bmc ip','ip','bmc地址')),None)
                if ni is None and ii is None: raise ValueError('清单须有节点名或 BMC_IP 列')
                for row in rows[1:]:
                    name=str(row[ni]).strip() if ni is not None and ni<len(row) else ''
                    ip=str(row[ii]).strip() if ii is not None and ii<len(row) else ''
                    if not name and not ip: continue
                    values.extend(parse_text((name or ip)+(' '+ip if ip else ''),site_mapping=args.site_mapping))
    for target in args.target or []:
        values.extend(parse_text(target,site_mapping=args.site_mapping))
    if len(values)>2000: raise ValueError('单次设备任务最多 2000 台；请显式拆分范围')
    return list(validate_targets(values))
