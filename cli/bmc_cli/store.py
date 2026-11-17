"""CLI-local SQLite metadata only; never persist credentials or approvals."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3
import time

class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root/'assets.sqlite3'
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cli_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                action TEXT NOT NULL, data TEXT NOT NULL);''')
        os.chmod(self.path,0o600)
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            yield db;db.commit()
        except Exception:
            db.rollback();raise
        finally:db.close()
    def assets(self, mode):
        return []  # No legacy Web cache in an independent CLI database.
    def audit(self, action, **data):
        with self.connect() as db:
            db.execute('INSERT INTO cli_audit(at,action,data) VALUES(?,?,?)',
                       (time.time(),action,json.dumps(data,ensure_ascii=False)))
