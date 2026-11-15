"""Consolidated offline acceptance only. Never install, launch services or contact a broker."""
from __future__ import annotations
import argparse, hashlib, importlib.metadata, json, os, re, shutil, stat, subprocess, sys, tempfile, zipfile
from pathlib import Path, PurePosixPath
ROOT=Path(__file__).resolve().parent
ORIGINALS={
 'Swift-Integrated-188-Review.zip':'5041bdeae2279b084f2c3487eac037cc773e3c3bf33794af4f133449d7d5081e',
 'Swift-OneShot-Paper-Pilot.zip':'5c20075cb459b7f5c61863a3607e0483c0ecf47521e8091e1af590832a2d7b7d',
 'Swift-Local-Completion.zip':'abeb523db8b2fd5e1c9259d00c10742b16d0872f8afc455fda9a9c8a4e246e6d',
}
GUARDS=('NETWORK_GUARD_SELF_TEST_PASS','EXTERNAL_EXECUTION_GUARD_SELF_TEST_PASS','SQLITE_GUARD_SELF_TEST_PASS')

def sha(b):return hashlib.sha256(b).hexdigest()
def unique(raw):
    def pairs(items):
        d={}
        for k,v in items:
            if k in d:raise ValueError('duplicate key')
            d[k]=v
        return d
    return json.loads(raw,object_pairs_hook=pairs)
def relpath(name):
    p=PurePosixPath(name)
    if not name or p.is_absolute() or p.as_posix()!=name or any(q in ('.','..') or ':' in q or '\\' in q or q.endswith(('.', ' ')) for q in p.parts):raise ValueError('unsafe path')
    return p

def verify_bundle(expected):
    manifest=ROOT/'BUNDLE_MANIFEST.json'; raw=manifest.read_bytes()
    if sha(raw)!=expected:raise ValueError('bundle manifest fingerprint differs')
    m=unique(raw)
    if not isinstance(m,dict) or not m:raise ValueError('empty manifest')
    files={};seen=set()
    for name,digest in m.items():
        p=relpath(name)
        if name.casefold() in seen or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('invalid manifest entry')
        seen.add(name.casefold()); f=ROOT.joinpath(*p.parts)
        for path in (f,*f.parents):
            info=path.lstat()
            if path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:raise ValueError('redirected file')
            if path==ROOT:break
        if not f.is_file() or f.stat().st_size>8_000_000:raise ValueError('invalid bundle file')
        b=f.read_bytes()
        if sha(b)!=digest:raise ValueError('bundle file mismatch: '+name)
        files[name]=b
    actual={p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file()}
    if actual!=set(files)|{'BUNDLE_MANIFEST.json'}:raise ValueError('unexpected bundle files')
    return files

def archive(raw,expected,manifest_name):
    import io
    if sha(raw)!=expected:raise ValueError('original archive digest differs')
    files={};seen=set();total=0
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for i in z.infolist():
            p=relpath(i.filename)
            if i.is_dir() or stat.S_ISLNK(i.external_attr>>16) or i.filename.casefold() in seen:raise ValueError('invalid archive member')
            seen.add(i.filename.casefold());total+=i.file_size
            if total>8_000_000:raise ValueError('archive exceeds size cap')
            files[i.filename]=z.read(i)
    m=unique(files[manifest_name]);
    if not isinstance(m,dict) or set(m)!=set(files)-{manifest_name} or any(sha(files[k])!=v for k,v in m.items()):raise ValueError('inner manifest mismatch')
    return files

def write_files(dest,files):
    dest.mkdir(parents=True,exist_ok=False)
    for rel,b in files.items():
        f=dest.joinpath(*relpath(rel).parts);f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b)
        if sha(f.read_bytes())!=sha(b):raise ValueError('copy mismatch')

def env_for(stage):
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','PATHEXT','SYSTEMDRIVE','COMSPEC','OS','PROGRAMFILES','PROGRAMFILES(X86)'}}
    for folder in ('home','temp'):(stage/folder).mkdir(exist_ok=True)
    env.update({k:str(stage/'home') for k in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA')}); env.update({k:str(stage/'temp') for k in ('TMP','TEMP','TMPDIR')});env['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
    return env

def checked_run(args,cwd,env,count,guarded=True):
    r=subprocess.run([sys.executable,'-I','-B',*args],cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=240)
    print(r.stdout,flush=True)
    if r.returncode:raise RuntimeError('test subprocess failed')
    if guarded:
        matches=re.findall(r'\b(\d+) passed\b',r.stdout)
        if not matches or int(matches[-1])!=count or any(g not in r.stdout for g in GUARDS) or re.search(r'\b\d+ (?:failed|skipped|xfailed|xpassed|errors?)\b',r.stdout):raise RuntimeError('guarded test receipt invalid')
    elif not re.search(r'Ran '+str(count)+r' tests\b',r.stdout) or '\nOK' not in r.stdout:raise RuntimeError('rehearsal receipt invalid')
    return count

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest-sha256',required=True);a=p.parse_args()
    files=verify_bundle(a.manifest_sha256)
    baseline=archive(files['baseline/Swift-Integrated-188-Review.zip'],ORIGINALS['Swift-Integrated-188-Review.zip'],'SOURCE_MANIFEST.json')
    paper=archive(files['baseline/Swift-OneShot-Paper-Pilot.zip'],ORIGINALS['Swift-OneShot-Paper-Pilot.zip'],'PACKAGE_SHA256.json')
    rx=archive(files['baseline/Swift-Local-Completion.zip'],ORIGINALS['Swift-Local-Completion.zip'],'PACKAGE_SHA256.json')
    if not (len(baseline)==84 and len(paper)==67 and len(rx)==9):raise ValueError('archive counts differ')
    # Refuse a missing test environment before any subprocess. Never install dependencies.
    print('test_python',sys.version.split()[0], 'pytest',importlib.metadata.version('pytest'),flush=True)
    with tempfile.TemporaryDirectory(prefix='swift-offline-acceptance-') as tmp:
        work=Path(tmp); review=work/'review';write_files(review,baseline)
        print('== original 188 ==',flush=True)
        first=checked_run([str(review/'validate_review.py'),'--manifest-sha256',sha(baseline['SOURCE_MANIFEST.json'])],work,env_for(work),188)
        print('== hardened paper ==',flush=True)
        stage=work/'paper'
        subset={k:v for k,v in paper.items() if k.startswith('runtime/')}
        subset.update({'candidate/'+k:v for k,v in paper.items() if '/' not in k and k.endswith('.py')})
        for k,v in files.items():
            if k.startswith('paper-overlay/'):subset['candidate/'+k.split('/',1)[1]]=v
        subset['candidate/create_mandate_cli.py']=baseline['candidate/create_mandate_cli.py'];subset['isolated_worker.py']=baseline['isolated_worker.py']
        write_files(stage,subset)
        worker="import importlib.util,pathlib,sys; p=pathlib.Path.cwd(); s=importlib.util.spec_from_file_location('worker',p/'isolated_worker.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);sys.exit(m.worker(p))"
        second=checked_run(['-c',worker],stage,env_for(stage),33)
        print('== operational rehearsal ==',flush=True)
        op=work/'rehearsal.py';op.write_bytes(files['operations/Swift-Operational-Rehearsal.py'])
        # Run unit cases plus archive rehearsal, expressly skip local_status() on Windows.
        z=work/'original.zip';z.write_bytes(files['baseline/Swift-Integrated-188-Review.zip'])
        code="import importlib.util,pathlib,sys,unittest; s=importlib.util.spec_from_file_location('r',pathlib.Path.cwd()/'rehearsal.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(m.Tests));print(m.rehearsal(m.archive_files(pathlib.Path.cwd()/'original.zip')));sys.exit(0 if r.wasSuccessful() else 2)"
        third=checked_run(['-c',code],work,env_for(work),7,False)
    print(json.dumps({'status':'OFFLINE_REVIEW_PASSED_NOT_LIVE_CERTIFIED','review_tests':first,'paper_tests':second,'rehearsal_tests':third,'total_case_executions':first+second+third,'source_manifest_entries':len(baseline)-1+len(paper)-1+len(rx)-1,'platform':sys.platform,'production_modified':False,'broker_contacted':False,'live_certified':False},indent=2))
if __name__=='__main__':main()
