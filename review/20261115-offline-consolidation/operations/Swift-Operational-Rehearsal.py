"""Swift review-only deployment rehearsal and local status observer.
No broker, Telegram, credentials, subprocess, installation, task or live writes.
All release/pointer/lock/journal mutations are synthetic and confined to a new
TemporaryDirectory. This is not a production deployment or certification tool.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ARCHIVE = '5041bdeae2279b084f2c3487eac037cc773e3c3bf33794af4f133449d7d5081e'
MANIFEST = '53ffa50b463c490e899ac67190bb3fbc3a0afeb984e3d308ad0fa3d74667e27e'
PIN = '0c090e034ba7a761a1eb1a0cd08e06c91ddf8098'
MAX_BYTES = 2_000_000


def digest(b):
    return hashlib.sha256(b).hexdigest()


def unique_json(b):
    def unique(pairs):
        d = {}
        for k,v in pairs:
            if k in d:
                raise ValueError('duplicate JSON key')
            d[k]=v
        return d
    return json.loads(b,object_pairs_hook=unique)


def archive_files(path):
    if path.stat().st_size > MAX_BYTES or digest(path.read_bytes()) != ARCHIVE:
        raise ValueError('archive digest mismatch')
    with zipfile.ZipFile(path) as z:
        files={}; seen=set(); total=0
        for i in z.infolist():
            p=PurePosixPath(i.filename)
            if (i.is_dir() or p.is_absolute() or p.as_posix()!=i.filename or '..' in p.parts
                or ':' in i.filename or '\\' in i.filename or i.filename.casefold() in seen
                or stat.S_ISLNK(i.external_attr >> 16)):
                raise ValueError('invalid archive member')
            total+=i.file_size
            if total > MAX_BYTES: raise ValueError('archive too large')
            files[i.filename]=z.read(i); seen.add(i.filename.casefold())
    if len(files)!=84 or digest(files['SOURCE_MANIFEST.json'])!=MANIFEST:
        raise ValueError('manifest mismatch')
    manifest=unique_json(files['SOURCE_MANIFEST.json'])
    if not isinstance(manifest,dict) or set(manifest)!=set(files)-{'SOURCE_MANIFEST.json'}:
        raise ValueError('manifest coverage mismatch')
    if any(digest(files[k])!=v for k,v in manifest.items()):
        raise ValueError('source digest mismatch')
    return files


def verify_stage(root, files):
    actual={x.relative_to(root).as_posix() for x in root.rglob('*') if x.is_file()}
    if actual!=set(files): raise ValueError('unexpected staged contents')
    for rel,b in files.items():
        p=root/rel
        if p.is_symlink() or digest(p.read_bytes())!=digest(b):
            raise ValueError('staged bytes changed')


def stage(root, files):
    root.mkdir(exist_ok=False)
    for rel,b in files.items():
        p=root/rel; p.parent.mkdir(parents=True,exist_ok=True)
        with p.open('xb') as f: f.write(b)
    verify_stage(root,files)


def atomic_text(path, text):
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='rehearsal-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


@contextmanager
def simulation_lock(root):
    path=root/'simulation.lock'
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try: yield
    finally:
        os.close(fd); path.unlink()


def switch(root, name, files):
    if name not in {'review-A','review-B'}: raise ValueError('synthetic release only')
    with simulation_lock(root):
        verify_stage(root/name,files)
        atomic_text(root/'mock-active.txt',name+'\n')


def rehearsal(files):
    with tempfile.TemporaryDirectory(prefix='swift-disposable-rehearsal-') as tmp:
        root=Path(tmp)
        a=root/'review-A'; b=root/'review-B'
        stage(a,files); stage(b,files)
        switch(root,'review-A',files)
        journal=root/'mock-journal.json'
        atomic_text(journal,'{"synthetic_only":true,"events":["mock-history"]}\n')
        old=journal.read_bytes()
        with simulation_lock(root):
            try: switch(root,'review-B',files)
            except FileExistsError: pass
            else: raise AssertionError('concurrent switch allowed')
        if (root/'mock-active.txt').read_text().strip()!='review-A': raise AssertionError('pointer changed')
        one=b/'SOURCE_MANIFEST.json'; original=one.read_bytes(); one.write_bytes(original+b'\n')
        try: switch(root,'review-B',files)
        except ValueError: pass
        else: raise AssertionError('tampered release accepted')
        if (root/'mock-active.txt').read_text().strip()!='review-A': raise AssertionError('pointer changed on failure')
        one.write_bytes(original)
        switch(root,'review-B',files)
        if (root/'mock-active.txt').read_text().strip()!='review-B': raise AssertionError('promotion failed')
        atomic_text(journal,old.decode().strip()+'\n')
        # An event after simulated promotion must survive rollback.
        atomic_text(journal,'{"synthetic_only":true,"events":["mock-history","post-promotion-event"]}\n')
        later=journal.read_bytes()
        switch(root,'review-A',files)
        if (root/'mock-active.txt').read_text().strip()!='review-A' or journal.read_bytes()!=later:
            raise AssertionError('rollback lost post-promotion history')
        return {'exact_stage_verification':'PASS','exclusive_lock_conflict':'PASS','tamper_rejected_pointer_unchanged':'PASS','mock_promotion':'PASS','mock_rollback_preserves_post_promotion_history':'PASS','scope':'DISPOSABLE_SYNTHETIC_FILES_ONLY'}


def read_status(path):
    try:
        if path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400 or not path.is_file(): return None
        if path.stat().st_size>100_000: return None
        d=unique_json(path.read_bytes().decode('utf-8-sig'))
        return d if isinstance(d,dict) else None
    except (OSError,ValueError,TypeError): return None


def fresh(d, limit=45):
    try:
        t=datetime.fromisoformat(d['as_of_utc'].replace('Z','+00:00'))
        if t.utcoffset() is None: return False
        return 0 <= (datetime.now(timezone.utc)-t).total_seconds() <= limit
    except (KeyError,TypeError,ValueError,AttributeError): return False


def receiver(d):
    if not isinstance(d,dict): return 'UNAVAILABLE'
    if (fresh(d) and d.get('status')=='RUNNING' and d.get('mode')=='PAPER_ONLY'
        and all(d.get(k) is False for k in ('live_enabled','broker_writes_enabled','automated_paper_orders','paper_dispatch_worker_enabled'))
        and isinstance(d.get('workers'),dict) and d['workers'].get('telegram') is True):
        return 'FRESH_RECEIVE_ONLY_HEARTBEAT'
    return 'NOT_VERIFIED'


def paper(d):
    if not isinstance(d,dict): return {'status':'UNAVAILABLE'}
    safe=all(d.get(k) is v for k,v in {'real_order_tools_called':False,'live_certified':False,'source_read_only':True}.items()) and d.get('mode')=='PAPER_ONLY'
    return {'status':'FRESH_PAPER_HEARTBEAT' if safe and fresh(d,120) and d.get('status')=='PAPER_PILOT_RUNNING' else 'NOT_VERIFIED',
            'reported_quote_observation':bool(safe and d.get('fresh_quote_verified') is True),
            'reported_modeled_fill':bool(safe and d.get('forward_fill_verified') is True)}


def local_status():
    if os.name!='nt': return {'status':'WINDOWS_NOT_OBSERVED'}
    root=Path(r'E:\SwiftAutoTraderCertification\read-only-pilot\installed')
    try: release=(root/'active-release.txt').read_text(encoding='utf-8-sig').strip()==PIN
    except OSError: release=False
    local=os.environ.get('LOCALAPPDATA')
    p=read_status(Path(local)/'SwiftPaperPilot-20261114'/'state'/'PAPER-STATUS.json') if local else None
    return {'installed_marker_matches_prior_baseline':release,'receiver':receiver(read_status(root/'state'/'health.json')),
            'paper':paper(p),'task_and_process_identity':'NOT_VERIFIED','credentials':'NOT_READ','broker':'NOT_CONTACTED'}


class Tests(unittest.TestCase):
    def test_duplicate_json(self):
        with self.assertRaises(ValueError): unique_json('{"x":1,"x":2}')
    def test_missing_health(self): self.assertEqual(receiver(None),'UNAVAILABLE')
    def test_fresh_receiver(self):
        d={'as_of_utc':datetime.now(timezone.utc).isoformat(),'status':'RUNNING','mode':'PAPER_ONLY','workers':{'telegram':True}, **dict.fromkeys(('live_enabled','broker_writes_enabled','automated_paper_orders','paper_dispatch_worker_enabled'),False)}
        self.assertEqual(receiver(d),'FRESH_RECEIVE_ONLY_HEARTBEAT')
        del d['broker_writes_enabled']; self.assertEqual(receiver(d),'NOT_VERIFIED')
    def test_future_stale_naive(self):
        for t in ('2000-01-01T00:00:00Z','2999-01-01T00:00:00Z','2026-01-01T00:00:00'):
            self.assertFalse(fresh({'as_of_utc':t}))
    def test_paper_claim_denied(self):
        self.assertFalse(paper({'fresh_quote_verified':True,'forward_fill_verified':True})['reported_modeled_fill'])
    def test_staged_extra_file(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'x'; stage(p,{'a':b'a'}); (p/'extra').write_bytes(b'x')
            with self.assertRaises(ValueError): verify_stage(p,{'a':b'a'})
    def test_simulation(self):
        result=rehearsal({'SOURCE_MANIFEST.json':b'{}','fake.txt':b'fake'})
        self.assertEqual(result['mock_rollback_preserves_post_promotion_history'],'PASS')


def main():
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('--archive',type=Path); ap.add_argument('--self-test',action='store_true'); a=ap.parse_args()
    if a.self_test:
        res=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
        if not res.wasSuccessful(): return 2
    if a.archive:
        try:
            files=archive_files(a.archive); report=rehearsal(files)
            print(json.dumps({'status':'OFFLINE_REHEARSAL_PASSED_NOT_LIVE_CERTIFIED','archive_files':len(files),'manifest_entries':len(files)-1,'rehearsal':report,'local_observation':local_status(),'production_modified':False,'broker_contacted':False,'live_certified':False},indent=2)); return 0
        except (OSError,ValueError,KeyError,AssertionError,zipfile.BadZipFile) as exc:
            print(json.dumps({'status':'OFFLINE_REHEARSAL_BLOCKED','error_type':type(exc).__name__,'production_modified':False,'live_certified':False})); return 2
    return 0 if a.self_test else 2

if __name__=='__main__': raise SystemExit(main())
