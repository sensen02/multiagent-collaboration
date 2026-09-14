from __future__ import annotations
import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


def uid(prefix=''):
    return prefix + uuid.uuid4().hex[:12]


def now():
    return time.time()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Store:
    """One writer (the runtime loop). Projections and facts commit together."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / 'objects'
        self.objects.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'state.sqlite3', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self._migrate()
        self.depth = 0

    def _migrate(self):
        """Apply small, ordered SQLite migrations and record the schema level."""
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        migrations = {
            1: '''
                CREATE TABLE IF NOT EXISTS records (
                  kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  run_id TEXT, task_id TEXT, revision INTEGER, type TEXT NOT NULL,
                  created REAL NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS calls (
                  id TEXT PRIMARY KEY, task_id TEXT, epoch INTEGER, name TEXT,
                  args TEXT, status TEXT, result TEXT);
            ''',
            2: '''
                CREATE TABLE IF NOT EXISTS workspace_generations (
                  workspace TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
                  manifest_id TEXT NOT NULL, updated REAL NOT NULL);
            ''',
        }
        latest = max(migrations)
        if version > latest:
            raise RuntimeError(f'数据库 schema 版本 {version} 高于程序支持的 {latest}')
        for target in range(version + 1, latest + 1):
            with self.db:
                self.db.executescript(migrations[target])
                self.db.execute(f'PRAGMA user_version={target}')
        # Older databases may have tables but no user_version marker.
        if version == 0:
            self.db.execute(f'PRAGMA user_version={latest}')

    @contextlib.contextmanager
    def transaction(self):
        outer = self.depth == 0
        if outer:
            self.db.execute('BEGIN IMMEDIATE')
        self.depth += 1
        try:
            yield
            if outer:
                self.db.execute('COMMIT')
        except BaseException:
            if outer:
                self.db.execute('ROLLBACK')
            raise
        finally:
            self.depth -= 1

    def put(self, kind, record):
        self.db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                        (kind, record['id'], dumps(record)))
        return record

    def get(self, kind, id):
        row = self.db.execute('SELECT body FROM records WHERE kind=? AND id=?', (kind,id)).fetchone()
        if not row:
            raise ValueError(f'{kind} not found: {id}')
        return json.loads(row['body'])

    def all(self, kind):
        return [json.loads(r['body']) for r in self.db.execute('SELECT body FROM records WHERE kind=? ORDER BY rowid', (kind,))]

    def event(self, type, payload=None, task=None, run_id=None, revision=None):
        run_id = task['run_id'] if task else run_id
        revision = task['revision'] if task else revision
        id = uid('e_')
        self.db.execute('INSERT INTO events(id,run_id,task_id,revision,type,created,payload) VALUES(?,?,?,?,?,?,?)',
                        (id,run_id,task['id'] if task else None,revision,type,now(),dumps(payload or {})))
        return self.db.execute('SELECT last_insert_rowid()').fetchone()[0]

    def events(self, run_id=None, after=0, limit=500):
        rows = self.db.execute('SELECT * FROM events WHERE seq>? AND (? IS NULL OR run_id=?) ORDER BY seq LIMIT ?',
                               (after,run_id,run_id,limit))
        return [dict(r) | {'payload': json.loads(r['payload'])} for r in rows]

    def cursor(self):
        return self.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]

    def blob(self, content):
        if isinstance(content,str):
            content = content.encode()
        digest = hashlib.sha256(content).hexdigest()
        path = self.objects / digest
        if not path.exists():
            path.write_bytes(content)
        return digest

    def read_blob(self, digest):
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid object id')
        return (self.objects / digest).read_bytes()

    def close(self):
        self.db.close()
