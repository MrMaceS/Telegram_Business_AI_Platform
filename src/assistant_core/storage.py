"""SQLite repository. One database per company; one runtime process per database."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

# Candidate stages (TZ section 3). Manual mode and global STOP are separate flags, not stages.
STAGES = ('NEW', 'CONDITIONS_SENT', 'CONSENT_RECORDED', 'PRESENTATION_SENT',
          'INVITE_SENT', 'DECLINED', 'AWAITING_OWNER')
# Forward-only transitions. CONSENT_RECORDED is reachable only through record_consent();
# leaving AWAITING_OWNER is possible only through resume_candidate().
TRANSITIONS = {
    'NEW': {'CONDITIONS_SENT', 'DECLINED', 'AWAITING_OWNER'},
    'CONDITIONS_SENT': {'DECLINED', 'AWAITING_OWNER'},
    'CONSENT_RECORDED': {'PRESENTATION_SENT', 'AWAITING_OWNER'},
    'PRESENTATION_SENT': {'INVITE_SENT', 'AWAITING_OWNER'},
    'INVITE_SENT': {'AWAITING_OWNER'},
    'DECLINED': {'AWAITING_OWNER'},
    'AWAITING_OWNER': set(),
}

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
        CREATE TABLE IF NOT EXISTS candidates(chat INTEGER PRIMARY KEY,
          stage TEXT NOT NULL DEFAULT 'NEW',prev_stage TEXT,
          conditions_version TEXT,conditions_mid INTEGER,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS consents(id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat INTEGER NOT NULL,mid INTEGER NOT NULL,text TEXT NOT NULL,
          conditions_version TEXT NOT NULL,at_utc TEXT NOT NULL,UNIQUE(chat,mid));
        ''')
        schema = self.get('schema')
        bound = self.get('client_id')
        if schema not in (None, '1') or (bound is not None and bound != client_id):
            self.db.close()  # Do not keep the file open (Windows locks it).
            raise ValueError('Unsupported database schema; explicit migration required'
                             if schema not in (None, '1') else 'Database belongs to a different client')
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
        unclear = [r[0] for r in self.db.execute(
            "SELECT DISTINCT chat FROM outgoing WHERE status='SENDING'").fetchall()]
        self.execute("UPDATE outgoing SET status='UNKNOWN' WHERE status='SENDING'")
        # A candidate with an unclear send goes to the owner for reconciliation, keeping its stage.
        for chat in unclear:
            if self.candidate(chat):
                self.set_stage(chat, 'AWAITING_OWNER', reason='unclear send after restart')
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

    def candidate(self, chat):
        return self.db.execute('SELECT * FROM candidates WHERE chat=?', (chat,)).fetchone()

    def set_stage(self, chat, stage, expected=None, reason='', conditions_version=None, conditions_mid=None):
        """Move a candidate to `stage`. Call only after the step's result is persisted
        (e.g. CONDITIONS_SENT only after the conditions send is confirmed SENT).
        Returns True if changed, False if already there or `expected` no longer matches
        (stale state). Raises ValueError on a forbidden transition."""
        if stage not in STAGES:
            raise ValueError(f'Unknown stage {stage}')
        if stage == 'CONSENT_RECORDED':
            raise ValueError('CONSENT_RECORDED is set only by record_consent()')
        if stage == 'CONDITIONS_SENT' and not conditions_version:
            raise ValueError('CONDITIONS_SENT requires conditions_version')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO candidates(chat,stage,updated) VALUES(?,'NEW',?)",
                            (chat, time.time()))
            row = self.candidate(chat)
            current = row['stage']
            if expected is not None and current != expected:
                return False
            if current == stage:
                return False  # Idempotent; a repeated escalation keeps the original prev_stage.
            if stage not in TRANSITIONS[current]:
                raise ValueError(f'Forbidden transition {current} -> {stage}')
            if stage == 'AWAITING_OWNER':
                self.db.execute('UPDATE candidates SET stage=?,prev_stage=?,updated=? WHERE chat=?',
                                (stage, current, time.time(), chat))
            elif stage == 'CONDITIONS_SENT':
                self.db.execute('''UPDATE candidates SET stage=?,conditions_version=?,conditions_mid=?,
                  updated=? WHERE chat=?''', (stage, conditions_version, conditions_mid, time.time(), chat))
            else:
                self.db.execute('UPDATE candidates SET stage=?,updated=? WHERE chat=?',
                                (stage, time.time(), chat))
            self.db.execute('INSERT INTO audit(at,kind,chat,detail) VALUES(?,?,?,?)',
                            (time.time(), 'STAGE', chat, f'{current}->{stage} {reason}'[:500]))
        return True

    def record_consent(self, chat, mid, text, conditions_version):
        """Record an explicit consent message and move CONDITIONS_SENT -> CONSENT_RECORDED
        atomically. Recognising the text as explicit consent is the workflow's job.
        Returns 'RECORDED', 'DUPLICATE' (already recorded for this version: nothing changes,
        nothing must be re-sent) or 'REJECTED' (wrong stage, other conditions version,
        message not after the conditions)."""
        with self.db:
            if self.db.execute('SELECT 1 FROM consents WHERE chat=? AND mid=?', (chat, mid)).fetchone():
                return 'DUPLICATE'
            row = self.candidate(chat)
            if not row or row['conditions_version'] != conditions_version:
                return 'REJECTED'  # Consent to an older version is never carried over.
            if row['stage'] != 'CONDITIONS_SENT':
                done = self.db.execute('SELECT 1 FROM consents WHERE chat=? AND conditions_version=?',
                                       (chat, conditions_version)).fetchone()
                return 'DUPLICATE' if done else 'REJECTED'
            # Условия должны быть фактически отправлены (conditions_mid установлен)
            if row['conditions_mid'] is None:
                return 'REJECTED'
            if mid <= row['conditions_mid']:
                return 'REJECTED'
            at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self.db.execute('INSERT INTO consents(chat,mid,text,conditions_version,at_utc) VALUES(?,?,?,?,?)',
                            (chat, mid, text[:4000], conditions_version, at))
            self.db.execute("UPDATE candidates SET stage='CONSENT_RECORDED',updated=? WHERE chat=?",
                            (time.time(), chat))
            self.db.execute('INSERT INTO audit(at,kind,chat,detail) VALUES(?,?,?,?)',
                            (time.time(), 'CONSENT', chat, f'mid={mid} version={conditions_version}'))
        return 'RECORDED'

    def resume_candidate(self, chat, to_stage=None):
        """Owner's explicit decision for a candidate in AWAITING_OWNER: return to the saved
        stage (default) or close as DECLINED. Does not lift the global STOP, does not change
        MANUAL mode and does not repeat sends: steps already reserved in `outgoing` stay
        blocked by their dedupe key. Returns the new stage, or None if not awaiting the owner."""
        with self.db:
            row = self.candidate(chat)
            if not row or row['stage'] != 'AWAITING_OWNER':
                return None
            target = to_stage or row['prev_stage'] or 'NEW'
            if target not in (row['prev_stage'], 'DECLINED'):
                raise ValueError(f'Can resume only to {row["prev_stage"]} or DECLINED')
            self.db.execute('UPDATE candidates SET stage=?,prev_stage=NULL,updated=? WHERE chat=?',
                            (target, time.time(), chat))
            # Invalidate drafts prepared before the owner's decision.
            self.db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?', (chat,))
            self.db.execute('INSERT INTO audit(at,kind,chat,detail) VALUES(?,?,?,?)',
                            (time.time(), 'CANDIDATE_RESUMED', chat, target))
        return target

    def quota(self, limit):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO usage VALUES(?,0)', (day,))
            cur = self.db.execute('UPDATE usage SET calls=calls+1 WHERE day=? AND calls<?', (day, limit))
        return cur.rowcount == 1

    def close(self):
        self.db.close()