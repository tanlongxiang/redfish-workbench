"""Local asset ledger; manual/reference SN is independent from live System SN.
Imports and edits never contact a device or rewrite an original workbook.
"""
import json
import time
from collections import Counter
from xlsx_io import InputBook
from node_verify import FIRMWARE, has_sn
from firmware_batch import COMPONENTS, parse_targets, Target
from inventory_merge import usable_firmware
from asset_metadata import AssetMetadata

FIELDS = [n for n, _ in FIRMWARE]
DEFAULT_META = AssetMetadata().model_dump()
STATES = {'uncollected', 'basic', 'complete', 'partial', 'failed', 'stopped'}


def import_ledger(path, *, target_parser=parse_targets):
    book = InputBook(str(path)); records = []; seen = set(); names = set()
    aliases = {'name': ('节点名', '节点名称', '完整节点', 'node', 'name'),
               'address': ('bmc_ip', 'bmcip', 'bmc ip', 'bmc地址', 'ip'),
               'declared_sn': ('sn', '参考sn', '来源sn', '来源表sn', 'serialnumber'),
               'declared_model': ('机型', 'model'), 'location': ('位置', '机柜', 'location'),
               'tags': ('标签', 'tags'), 'notes': ('备注', 'notes')}
    for sheet in book.sheets:
        rows = [(n, values) for n, values in book.read_sheet(sheet) if any(str(c).strip() for c in values)]
        if not rows: continue
        headers = [str(v).strip().lower() for v in rows[0][1]]
        columns = {key: next((i for i, h in enumerate(headers) if h in accepted), None) for key, accepted in aliases.items()}
        if columns['name'] is None and columns['address'] is None: raise ValueError('资产清单缺少节点名 / BMC_IP 列')
        for _, row in rows[1:]:
            def cell(key):
                i = columns[key]
                return str(row[i]).strip() if i is not None and i < len(row) else ''
            name, address = cell('name'), cell('address')
            if not name and not address: continue
            target = target_parser((name or address) + (' ' + address if address else ''))[0]
            if target.address in seen or target.name in names: raise ValueError('资产清单有重复节点名或地址，未导入')
            seen.add(target.address); names.add(target.name)
            meta = AssetMetadata(**{key: cell(key) for key in aliases if key not in ('name', 'address')}).model_dump()
            sn = meta['declared_sn']
            import re
            if re.fullmatch(r'[+-]?\d+(?:\.\d+)?[eE][+-]?\d+', sn): raise ValueError('SN 为科学计数法，无法保证精度；请使用文本 SN')
            references={}
            for field in FIELDS:
                index=next((i for i,h in enumerate(headers) if h==field.lower()),None)
                value=str(row[index]).strip() if index is not None and index<len(row) else ''
                if usable_firmware(value):references[field]=value
            records.append({'name': target.name, 'address': target.address, 'metadata': meta, 'reference_firmware':references})
            if len(records) > 20000: raise ValueError('单次导入最多 20000 台')
    if not records: raise ValueError('清单没有有效资产')
    return records


class Catalog:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS asset_catalog (
                mode TEXT NOT NULL, address TEXT NOT NULL, name TEXT NOT NULL,
                metadata TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(mode,address));
                CREATE INDEX IF NOT EXISTS catalog_name ON asset_catalog(mode,name);''')
        # Upgrade older local installations without importing any business workbook.
        # Preserve cache timestamps and do not turn a historical read into a precheck.
        from firmware_batch import Target
        for mode in ('demo','live'):
            for cached in store.assets(mode):
                with store.connect() as db:
                    exists=db.execute('SELECT 1 FROM asset_catalog WHERE mode=? AND address=?',(mode,cached['address'])).fetchone()
                if not exists:
                    self.observe(mode,Target(cached['name'],cached['address']),cached['snapshot'],at=cached['observed'])

    def register(self, mode, records):
        if mode not in ('demo', 'live'): raise ValueError('模式无效')
        now = time.time()
        # Validate every row before beginning a transaction; no partial import.
        for r in records:
            AssetMetadata.model_validate(r.get('metadata', {}))
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for r in records:
                row = db.execute('SELECT * FROM asset_catalog WHERE mode=? AND address=?', (mode, r['address'])).fetchone()
                meta = {**DEFAULT_META, **(json.loads(row['metadata']) if row else {})}
                for key, value in r.get('metadata', {}).items():
                    # A sparse import does not erase an existing annotation or SN.
                    if value and key != 'lifecycle': meta[key] = value
                data = json.loads(row['data']) if row else {}
                reference=data.setdefault('reference_firmware',{})
                for field,value in r.get('reference_firmware',{}).items():
                    if field in FIELDS and usable_firmware(value):reference[field]=value
                if r.get('reference_firmware'):data['reference_imported_at']=now
                if not reference:data.pop('reference_firmware',None)
                db.execute('INSERT INTO asset_catalog VALUES(?,?,?,?,?,?,?) ON CONFLICT(mode,address) DO UPDATE SET name=excluded.name,metadata=excluded.metadata,data=excluded.data,updated=excluded.updated',
                           (mode, r['address'], r['name'], json.dumps(meta, ensure_ascii=False), json.dumps(data), now, now))
        return len(records)

    def edit(self, mode, address, metadata):
        meta = AssetMetadata.model_validate(metadata).model_dump()
        with self.store.connect() as db:
            cursor = db.execute('UPDATE asset_catalog SET metadata=?,updated=? WHERE mode=? AND address=?', (json.dumps(meta, ensure_ascii=False), time.time(), mode, address))
            if cursor.rowcount != 1: raise ValueError('资产不存在')
        return self.get(mode, address)

    def observe(self, mode, target, snapshot, at=None):
        self.register(mode, [{'name': target.name, 'address': target.address}])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM asset_catalog WHERE mode=? AND address=?', (mode, target.address)).fetchone()
            data = json.loads(row['data']); now = time.time() if at is None else at
            for key in ('model', 'power'):
                if snapshot.get(key): data[key] = snapshot[key]
            data['last_seen'] = now
            versions = data.setdefault('firmware', {}); observed = data.setdefault('firmware_observed', {})
            for component, item in snapshot.get('inventory', {}).items():
                if component in FIELDS and usable_firmware(item.get('version'),{'State':item.get('state')}):
                    versions[component] = item['version']; observed[component] = now
            db.execute('UPDATE asset_catalog SET data=?,updated=? WHERE mode=? AND address=?', (json.dumps(data, ensure_ascii=False), now, mode, target.address))

    def collected(self, mode, target, result):
        self.register(mode, [{'name': target.name, 'address': target.address}])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM asset_catalog WHERE mode=? AND address=?', (mode, target.address)).fetchone()
            data = json.loads(row['data']); now = result['observed']
            if result.get('model'):
                for key in ('model','power','manufacturer','system_sn','chassis_sn','cpu_count','cpu_model','memory_gib','bios'):
                    if key in result: data[key] = result[key]
                data['last_seen'] = now
            if 'memory' in result:
                data['memory']=result['memory'];data['memory_observed']=now
            versions = data.setdefault('firmware', {}); observed = data.setdefault('firmware_observed', {})
            for field, value in result.get('firmware', {}).items():
                if field in FIELDS and usable_firmware(value): versions[field] = value; observed[field] = now
            data.update(last_attempt=now, collection_state=result['collection_state'],
                        latest_fields=[k for k,v in result.get('firmware', {}).items() if k in FIELDS and usable_firmware(v)],
                        last_error=result.get('error', ''), notes=result.get('notes', []))
            # Empty/NA/absent/error versions never erase previously valid cached versions.
            db.execute('UPDATE asset_catalog SET data=?,updated=? WHERE mode=? AND address=?', (json.dumps(data, ensure_ascii=False), time.time(), mode, target.address))

    def get(self, mode, address):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM asset_catalog WHERE mode=? AND address=?', (mode, address)).fetchone()
        if not row: raise ValueError('资产不存在')
        return self.public(row)

    @staticmethod
    def public(row):
        meta = {**DEFAULT_META, **json.loads(row['metadata'])}; data = json.loads(row['data'])
        declared, actual = meta['declared_sn'], data.get('system_sn', '')
        sn_state = 'missing' if not has_sn(actual) else ('match' if declared == actual else 'conflict') if has_sn(declared) else 'uncompared'
        return {'mode': row['mode'], 'name': row['name'], 'address': row['address'], 'metadata': meta, 'data': data,
                'model': data.get('model') or meta['declared_model'] or '未知', 'power': data.get('power') or '未知',
                'collection_state': data.get('collection_state', 'uncollected'), 'sn_state': sn_state,
                'reference_count': len(data.get('reference_firmware', {})), 'firmware_count': len(data.get('firmware', {})), 'latest_count': len(data.get('latest_fields', [])),
                'created': row['created'], 'updated': row['updated']}

    def query(self, mode, *, q='', model='', power='', status='', sn='', lifecycle='', firmware_field='', firmware_version='', version_source='observed', page=1, page_size=50, all_rows=False):
        if mode not in ('demo','live'): raise ValueError('模式无效')
        if not 1 <= page_size <= 200 or page < 1: raise ValueError('分页参数无效')
        if status and status not in STATES: raise ValueError('采集状态无效')
        if sn and sn not in ('missing','match','conflict','uncompared'): raise ValueError('SN状态无效')
        if lifecycle and lifecycle not in ('active','spare','repair','retired'): raise ValueError('资产状态无效')
        if firmware_field and firmware_field not in FIELDS: raise ValueError('固件字段无效')
        # SQL paginates the row payload. Statistics operate on projected JSON fields,
        # not on a browser-sized copy of every record.
        conditions=['mode=?']; values=[mode]
        if q:
            pattern='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            conditions.append("(name LIKE ? ESCAPE '\\' OR address LIKE ? ESCAPE '\\' OR metadata LIKE ? ESCAPE '\\' OR json_extract(data,'$.system_sn') LIKE ? ESCAPE '\\')")
            values.extend([pattern]*4)
        expressions={'model': "COALESCE(NULLIF(json_extract(data,'$.model'),''),NULLIF(json_extract(metadata,'$.declared_model'),''),'未知')",
                     'power': "COALESCE(NULLIF(json_extract(data,'$.power'),''),'未知')",
                     'status': "COALESCE(json_extract(data,'$.collection_state'),'uncollected')",
                     'lifecycle': "COALESCE(json_extract(metadata,'$.lifecycle'),'active')"}
        # SN comparison is normalized once in this SQL expression, matching public().
        actual="COALESCE(json_extract(data,'$.system_sn'),'')"; declared="COALESCE(json_extract(metadata,'$.declared_sn'),'')"
        valid=lambda expr: f"lower(trim({expr})) NOT IN ('','0','00','01','1','na','n/a','-','none','null','unknown')"
        expressions['sn']=f"CASE WHEN NOT ({valid(actual)}) THEN 'missing' WHEN NOT ({valid(declared)}) THEN 'uncompared' WHEN {actual}={declared} THEN 'match' ELSE 'conflict' END"
        for key,value in (('model',model),('power',power),('status',status),('sn',sn),('lifecycle',lifecycle)):
            if value: conditions.append(expressions[key]+'=?'); values.append(value)
        if version_source not in ('observed','reference'):raise ValueError('版本来源无效')
        field=firmware_field or 'BMC_Active'
        source='firmware' if version_source=='observed' else 'reference_firmware'
        version_expr="COALESCE(json_extract(data, '$."+source+"."+field+"'),'未采集')"
        if firmware_version: conditions.append(version_expr+'=?'); values.append(firmware_version)
        where=' AND '.join(conditions)
        with self.store.connect() as db:
            total=db.execute('SELECT count(*) FROM asset_catalog WHERE mode=?',(mode,)).fetchone()[0]
            count=db.execute('SELECT count(*) FROM asset_catalog WHERE '+where,values).fetchone()[0]
            args=list(values)
            sql='SELECT * FROM asset_catalog WHERE '+where+' ORDER BY name,address'
            if not all_rows: sql+=' LIMIT ? OFFSET ?'; args.extend([page_size,(page-1)*page_size])
            items=[self.public(r) for r in db.execute(sql,args)]
            groups={key:[{'name':r[0], 'count':r[1]} for r in db.execute('SELECT '+expr+',count(*) FROM asset_catalog WHERE '+where+' GROUP BY '+expr+' ORDER BY count(*) DESC,1',values)] for key,expr in expressions.items()}
            groups['versions']=[{'name':r[0], 'count':r[1]} for r in db.execute('SELECT '+version_expr+',count(*) FROM asset_catalog WHERE '+where+' GROUP BY '+version_expr+' ORDER BY count(*) DESC,1',values)]
            model_options=[r[0] for r in db.execute('SELECT DISTINCT '+expressions['model']+' FROM asset_catalog WHERE mode=? ORDER BY 1',(mode,))]
        return {'items':items,'total':total,'filtered':count,'page':page,'page_size':page_size,'groups':groups,'model_options':model_options,'firmware_fields':FIELDS,'version_field':field}
