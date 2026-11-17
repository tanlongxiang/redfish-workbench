"""SQLite durable task/audit records. Credentials and session tokens never belong here."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ACTIVE = ('queued','running')
TERMINAL = ('completed','failed','stopped','needs_review')

class Store:
    def __init__(self, root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.root/'portal.sqlite3'
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, action TEXT NOT NULL, mode TEXT NOT NULL,
              state TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
              draft TEXT NOT NULL, results TEXT, report TEXT, error TEXT, stop_requested INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_job ON events(job,id);
            CREATE TABLE IF NOT EXISTS images (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
              path TEXT NOT NULL, demo INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS plans (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, action TEXT NOT NULL, draft TEXT NOT NULL,
              scope TEXT NOT NULL, phrase TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', job TEXT);
            CREATE TABLE IF NOT EXISTS assets (
              mode TEXT NOT NULL, address TEXT NOT NULL, name TEXT NOT NULL, snapshot TEXT NOT NULL,
              observed REAL NOT NULL, PRIMARY KEY(mode,address));
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL,
              event TEXT NOT NULL, data TEXT NOT NULL);
            ''')
        os.chmod(self.path,0o600)
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            yield db;db.commit()
        except Exception:
            db.rollback();raise
        finally:db.close()
    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='needs_review',finished=?,error='服务中断，结果待核验；不会自动恢复或重试' WHERE state IN ('queued','running')",(time.time(),))
            db.execute("UPDATE plans SET state='expired' WHERE state='pending'")
    def audit(self, actor, event, **data):
        with self.connect() as db:db.execute('INSERT INTO audit(at,actor,event,data) VALUES(?,?,?,?)',(time.time(),actor,event,json.dumps(data,ensure_ascii=False)))
    def add_image(self, row):
        with self.connect() as db:db.execute('INSERT INTO images VALUES(?,?,?,?,?,?,?)',(row['id'],row['name'],row['sha256'],row['size'],row['path'],int(row['demo']),time.time()))
    def image(self, image_id):
        with self.connect() as db:row=db.execute('SELECT * FROM images WHERE id=?',(image_id,)).fetchone()
        if not row:raise ValueError('镜像不存在，请重新上传')
        return dict(row)
    def images(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT id,name,sha256,size,demo,created FROM images ORDER BY created DESC LIMIT 200')]
    def insert_job(self, db, job, owner, action, draft):
        db.execute('INSERT INTO jobs(id,owner,action,mode,state,created,draft) VALUES(?,?,?,?,?,?,?)',
                   (job,owner,action,draft['mode'],'queued',time.time(),json.dumps(draft,ensure_ascii=False)))
    def job(self, job):
        with self.connect() as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
        if not row:raise ValueError('任务不存在')
        result=dict(row)
        for k in ('draft','results'):
            result[k]=json.loads(result[k]) if result[k] else None
        return result
    def jobs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,action,mode,state,created,started,finished,error,stop_requested FROM jobs ORDER BY created DESC LIMIT 100')]
    def update_job(self, job, **fields):
        allowed={'state','started','finished','results','report','error','stop_requested'}
        if not fields or not set(fields)<=allowed:raise ValueError('非法任务字段')
        fields={k:json.dumps(v,ensure_ascii=False) if k=='results' else v for k,v in fields.items()}
        with self.connect() as db:db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),job))
    def event(self, job, data):
        with self.connect() as db:db.execute('INSERT INTO events(job,at,data) VALUES(?,?,?)',(job,time.time(),json.dumps(data,ensure_ascii=False)))
    def events(self, job, after=0):
        with self.connect() as db:
            return [{'id':r['id'],'at':r['at'],**json.loads(r['data'])} for r in db.execute('SELECT * FROM events WHERE job=? AND id>? ORDER BY id LIMIT 300',(job,after))]
    def asset(self, mode, target, snapshot, observed):
        with self.connect() as db:db.execute('INSERT INTO assets VALUES(?,?,?,?,?) ON CONFLICT(mode,address) DO UPDATE SET name=excluded.name,snapshot=excluded.snapshot,observed=excluded.observed',
                                            (mode,target.address,target.name,json.dumps(snapshot,ensure_ascii=False),observed))
    def assets(self, mode):
        with self.connect() as db:return [{**dict(r),'snapshot':json.loads(r['snapshot'])} for r in db.execute('SELECT * FROM assets WHERE mode=? ORDER BY address',(mode,))]
