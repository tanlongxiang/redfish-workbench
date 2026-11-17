"""Offline catalog extraction; input is pdftotext output, never executable code."""
from pathlib import Path
import argparse,re,pprint
p=argparse.ArgumentParser();p.add_argument('text',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
text=a.text.read_text();matches=[m for m in re.finditer(r'^\s*(3\.\d+\.\d+)\s+([^\n]+)$',text,re.M) if '...' not in m[2]]
rows=[]
for i,m in enumerate(matches):
 if m[1].rsplit('.',1)[0] not in {'3.3','3.4','3.7'}:continue
 block=text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)]
 before=block.split('字段列表')[0]
 method=re.search(r'操作方式[:：]\s*(\w+)',before)
 url=re.search(r'URL[:：]\s*(.*?)(?=请求头信息|请求消息体|⚫|字段列表)',before,re.S)
 path=re.sub(r'\s+','',url[1]) if url else ''
 path=re.sub(r'第\d+页/共355页','',path).replace('https://{bmc_ip}','')
 path=path.replace('{networkadapteras_id}','{networkadapters_id}')
 if 'RAIDStorage_id' in path:path=path.replace('RAIDStorage_id','{raid_storage_id}')
 body=before.split('请求消息体：',1)[-1] if '请求消息体：' in before else ''
 keys=list(dict.fromkeys(re.findall(r'["“]([^"”\n]+)["”]\s*[:：]',body)))
 rows.append(dict(id=m[1],module={'3.3':'Systems','3.4':'Chassis','3.7':'UpdateService'}[m[1].rsplit('.',1)[0]],title=m[2].strip(),method=method[1] if method else '',path=path,body_keys=keys,page=text[:m.start()].count('\f')+1))
assert len(rows)==69
content='"""G50 documentation catalog: 69 operations, not a hardware support claim.\nGenerated from the reviewed PDF; corrected parameter spelling is documented.\n"""\nOPERATIONS = '+pprint.pformat(rows,width=110,sort_dicts=False)+'\n'
with a.output.open('x',encoding='utf-8') as f:f.write(content)
print([(r['id'],r['body_keys']) for r in rows if r['method']!='GET'])
