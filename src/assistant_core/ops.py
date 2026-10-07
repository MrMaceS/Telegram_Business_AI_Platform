"""Offline backup/restore and healthcheck. Stop runtime before snapshot of files."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

def backup(source, destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if not (source/'state.sqlite3').is_file():
        raise ValueError('Source database does not exist')
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('Use a new backup directory outside data directory')
    destination.mkdir(parents=True,mode=0o700)
    with sqlite3.connect(source/'state.sqlite3') as src, sqlite3.connect(destination/'state.sqlite3') as dst:
        src.backup(dst)
    if (source/'files').exists():shutil.copytree(source/'files',destination/'files')
    manifest={str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest()
              for p in destination.rglob('*') if p.is_file()}
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2))

def restore(source,destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if destination.exists():raise ValueError('Restore requires NEW empty path; never overwrite live data')
    manifest=json.loads((source/'manifest.json').read_text())
    for name,digest in manifest.items():
        p=(source/name).resolve()
        if not p.is_relative_to(source) or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
            raise ValueError('Backup integrity check failed')
    shutil.copytree(source,destination)
    with sqlite3.connect(destination/'state.sqlite3') as db:
        db.execute("UPDATE meta SET value='1' WHERE key='paused'")
        db.execute("UPDATE outgoing SET status='UNKNOWN' WHERE status='SENDING'")
        db.execute("UPDATE inbox SET status='REVIEW' WHERE status IN ('RECEIVED','PENDING','RUNNING')")
        db.execute("UPDATE conversations SET task_status='ISSUE_REVIEW' WHERE task_status='ISSUING'")
        for id,path in db.execute('SELECT id,path FROM artifacts').fetchall():
            db.execute('UPDATE artifacts SET path=? WHERE id=?',(str(destination/'files'/Path(path).name),id))
    return destination

def health(data):
    db=sqlite3.connect((Path(data).resolve()/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    row=db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone();db.close()
    if not row or time.time()-float(row[0])>120:raise SystemExit(1)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['backup','restore','health']);p.add_argument('source');p.add_argument('destination',nargs='?');a=p.parse_args()
    if a.action=='health':health(a.source)
    elif not a.destination:p.error('destination required')
    elif a.action=='backup':backup(a.source,a.destination)
    else:restore(a.source,a.destination)
if __name__=='__main__':main()
