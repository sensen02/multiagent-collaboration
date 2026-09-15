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
            # 按任务取事件（任务详情抽屉要"这个任务发生了什么"）。没有这个索引，
            # `WHERE task_id=?` 就是一次全表扫描——事件表 196 MB、单条负载可达 176 KB，
            # 一次要 1.2 秒；有索引之后是按 (task_id, seq) 的定位读取。
            3: '''
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
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

    def events(self, run_id=None, after=0, limit=500, types=None):
        """按序取事件。`types` 只取这几类——**这不是优化选项，是必要的过滤**。

        `ModelReturned` 带上游逐条消息的 usage 归属：一次调用 1600+ 条 item、单条事件
        176 KB，实测占某个运行 151 MB 里的 99%。而历史派生一行都不用它——
        不过滤就等于为 0.35 MB 的正文解析 151 MB 的 JSON（430 倍）。
        """
        sql = 'SELECT * FROM events WHERE seq>? AND (? IS NULL OR run_id=?)'
        params = [after, run_id, run_id]
        if types:
            sql += " AND type IN (%s)" % ','.join('?' * len(types))
            params.extend(types)
        sql += ' ORDER BY seq LIMIT ?'
        params.append(limit)
        rows = self.db.execute(sql, params)
        return [dict(r) | {'payload': json.loads(r['payload'])} for r in rows]

    def event_index(self, run_id, until=None):
        """只取 (seq, task_id, type)：回放与协作图不读负载。

        用 `events()` 取同样这些行会把整个运行的负载全部解析一遍（大运行约 1.5 秒），
        而回放根本不需要负载。
        """
        rows = self.db.execute(
            'SELECT seq, task_id, type FROM events WHERE run_id=? AND (? IS NULL OR seq<=?) ORDER BY seq',
            (run_id, until, until))
        return [dict(r) for r in rows]

    def task_events(self, task_id, limit=200):
        """某个任务的**最近**若干条事件（含负载，抽屉里要看原文）。

        `events()` 是"某运行里 seq>游标的前 N 条"，语义不同；这里要的是"这个任务发生过
        什么"，并且只留尾部。走 events_task 索引，避免为 200 条记录把整个运行解析一遍。
        """
        rows = self.db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq DESC LIMIT ?',
                               (task_id, limit)).fetchall()
        return [dict(r) | {'payload': json.loads(r['payload'])} for r in reversed(rows)]

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
