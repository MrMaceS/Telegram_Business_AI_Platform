"""SQLite repository. One database per company; one runtime process per database."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

class Store:
    def __init__(self, directory: Path, client_id: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'state.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversations(chat INTEGER PRIMARY KEY,connection TEXT NOT NULL,
          mode TEXT NOT NULL DEFAULT 'AUTO',epoch INTEGER NOT NULL DEFAULT 0,last_in REAL NOT NULL,
          task_id TEXT,task_status TEXT,disclosed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages(chat INTEGER NOT NULL,mid INTEGER NOT NULL,
          direction TEXT NOT NULL,body TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(chat,mid));
        CREATE TABLE IF NOT EXISTS outgoing(id INTEGER PRIMARY KEY AUTOINCREMENT,
          dedupe TEXT UNIQUE NOT NULL,chat INTEGER NOT NULL,method TEXT NOT NULL,status TEXT NOT NULL,
          created REAL NOT NULL,telegram_id INTEGER);
        CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY AUTOINCREMENT,chat INTEGER NOT NULL,
          mid INTEGER NOT NULL,task_id TEXT,path TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
          original_name TEXT NOT NULL,UNIQUE(chat,mid));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,
          kind TEXT NOT NULL,chat INTEGER,detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY,calls INTEGER NOT NULL);
        ''')
        schema = self.get('schema')
        if schema not in (None, '1'):
            raise ValueError('Unsupported database schema; explicit migration required')
        bound = self.get('client_id')
        if bound is not None and bound != client_id:
            raise ValueError('Database belongs to a different client')
        self.set('client_id', client_id); self.set('schema', '1')
        if self.get('paused') is None:
            self.set('paused', '1')
        self.path.chmod(0o600)

    def execute(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, str(value)))

    def log(self, kind, chat=None, detail=''):
        # Details must be identifiers/statuses only, never raw prompts/tokens.
        self.execute('INSERT INTO audit(at,kind,chat,detail) VALUES(?,?,?,?)',
                     (time.time(), kind, chat, str(detail)[:500]))

    def ingest(self, update):
        uid = int(update['update_id'])
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?)',
                                  (uid, json.dumps(update, ensure_ascii=False), 'RECEIVED'))
            previous = int(self.get('offset', 0))
            self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            ('offset', str(max(previous, uid + 1))))
        return cur.rowcount == 1

    def complete(self, uid, status='DONE'):
        self.execute('UPDATE inbox SET status=? WHERE id=?', (status, uid))

    def pending(self):
        return self.db.execute("SELECT * FROM inbox WHERE status='PENDING' ORDER BY id LIMIT 20").fetchall()

    def received(self):
        return self.db.execute("SELECT * FROM inbox WHERE status='RECEIVED' ORDER BY id LIMIT 20").fetchall()

    def recover(self):
        # Safety on restart/restore: never replay a potentially executed outgoing action.
        self.set('paused', '1')
        self.execute("UPDATE outgoing SET status='UNKNOWN' WHERE status='SENDING'")
        self.execute("UPDATE conversations SET task_status='ISSUE_REVIEW' WHERE task_status='ISSUING'")
        self.execute("UPDATE inbox SET status='REVIEW' WHERE status='RUNNING'")
        # Received owner commands from before a restart require fresh approval.
        for row in self.db.execute("SELECT * FROM inbox WHERE status IN ('PENDING','RECEIVED')").fetchall():
            if 'message' in json.loads(row['payload']):
                self.complete(row['id'], 'REVIEW')
        self.log('STARTED_PAUSED')

    def pending_all(self):
        return self.db.execute("SELECT * FROM inbox WHERE status='PENDING'").fetchall()

    def conversation(self, chat):
        return self.db.execute('SELECT * FROM conversations WHERE chat=?', (chat,)).fetchone()

    def incoming(self, chat, connection, mid, body, date):
        with self.db:
            cur=self.db.execute('INSERT OR IGNORE INTO messages(chat,mid,direction,body) VALUES(?,?,?,?)',
                            (chat, mid, 'user', body[:8000]))
            if not cur.rowcount:
                return  # A recovered admission must not increment the epoch twice.
            self.db.execute('''INSERT INTO conversations(chat,connection,last_in) VALUES(?,?,?)
              ON CONFLICT(chat) DO UPDATE SET connection=excluded.connection,
              last_in=MAX(last_in,excluded.last_in),epoch=epoch+1''', (chat, connection, date))

    def mode(self, chat, mode):
        self.execute('UPDATE conversations SET mode=?,epoch=epoch+1 WHERE chat=?', (mode, chat))
        self.log('MODE', chat, mode)

    def history(self, chat):
        rows = self.db.execute('''SELECT direction,body FROM messages WHERE chat=? AND deleted=0
          ORDER BY mid DESC LIMIT 30''', (chat,)).fetchall()
        return [{'role':r[0], 'content':r[1][:1500]} for r in reversed(rows)]

    def reserve(self, key, chat, method):
        cur = self.execute('''INSERT OR IGNORE INTO outgoing(dedupe,chat,method,status,created)
          VALUES(?,?,?,'SENDING',?)''', (key, chat, method, time.time()))
        return cur.rowcount == 1

    def outgoing_result(self, key, status, message=None):
        self.execute('UPDATE outgoing SET status=?,telegram_id=? WHERE dedupe=?',
                     (status, message.get('message_id') if message else None, key))

    def quota(self, limit):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO usage VALUES(?,0)', (day,))
            cur = self.db.execute('UPDATE usage SET calls=calls+1 WHERE day=? AND calls<?', (day, limit))
        return cur.rowcount == 1

    def close(self):
        self.db.close()
