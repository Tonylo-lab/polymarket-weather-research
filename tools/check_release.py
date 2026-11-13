"""Read-only release scan. Prints locations/categories, never candidate values.

Checks an exact file allowlist, working bytes, index blobs and all reachable
commit trees/metadata. This is a bounded heuristic scan, not a secret detector
with an absolute guarantee. Run before push: python -B tools/check_release.py.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
ALLOWLIST=ROOT/'provenance/allowed_files.txt'
REVIEWED=ROOT/'provenance/scan_allowances.json'
RULES={
    'private_key_or_signature':r'0x[0-9a-fA-F]{64}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----',
    'wallet_address':r'\b0x[0-9a-fA-F]{40}\b',
    'provider_token':r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,})\b',
    'telegram_token':r'\b[0-9]{6,12}:[A-Za-z0-9_-]{25,}\b',
    'personal_path':r'\b[A-Za-z]:[\\/]|/(?:Users|home)/[^/\s]+/',
    'email':r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
    'ip_address':r'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])',
    'credential_assignment':r'''(?i)\b(?:private_key|secret|password|api_key|access_token|chat_id|webhook)\s*[=:]\s*["'][^"'\n]{8,}["']''',
}
def git(*args,allow_empty=False):
    p=subprocess.run(['git',*args],cwd=ROOT,capture_output=True)
    if p.returncode and not allow_empty:raise RuntimeError('LOCAL_GIT_INSPECTION_FAILED')
    return p.stdout

def scan(data,label):
    findings=[]
    try:text=data.decode('utf-8')
    except UnicodeDecodeError:return [(label,0,'non_utf8_file')]
    for i,line in enumerate(text.splitlines(),1):
        for kind,pattern in RULES.items():
            for m in re.finditer(pattern,line):
                # Nonpersonal publication identity required by Git, metadata only.
                if kind=='email' and (label.startswith('commit:') or label.endswith('tools/check_release.py')) and m.group()=='contributors@users.noreply.github.com':continue
                findings.append((label,i,kind))
        # Hex digests are permitted only with an explicit hash/fingerprint context.
        for m in re.finditer(r'(?<![A-Za-z0-9])[a-fA-F0-9]{64}(?![A-Za-z0-9])',line):
            if not re.search(r'(?i)sha256|fingerprint',line):findings.append((label,i,'unlabelled_hex64'))
        for m in re.finditer(r'(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9])',line):
            token=m.group()
            if not (re.search('[a-z]',token) and re.search('[A-Z]',token) and re.search('[0-9]',token)):continue
            entropy=-sum(n/len(token)*math.log2(n/len(token)) for n in Counter(token).values())
            if entropy>=4.5:findings.append((label,i,'high_entropy_candidate'))
    approved=json.loads(REVIEWED.read_text(encoding='utf-8')) if REVIEWED.exists() else []
    lines=text.splitlines()
    return [(loc,n,kind) for loc,n,kind in findings if not any(
        loc.split(':',1)[-1]==a['path'] and kind==a['category'] and
        hashlib.sha256(lines[n-1].encode()).hexdigest()==a['line_sha256'] for a in approved)]

def inspect(history=True):
    allowed=set(ALLOWLIST.read_text(encoding='utf-8').splitlines())
    actual={p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.relative_to(ROOT).parts}
    if actual!=allowed:raise RuntimeError('WORKING_FILE_ALLOWLIST_MISMATCH')
    findings=[]
    for name in sorted(allowed):
        p=ROOT/name
        if p.is_symlink():raise RuntimeError('SYMLINK_NOT_ALLOWED')
        if p.stat().st_size>1_000_000:raise RuntimeError('UNEXPECTED_LARGE_FILE')
        findings+=scan(p.read_bytes(),'work:'+name)
    staged=git('ls-files','--stage','-z',allow_empty=True).split(b'\0')
    staged_count=0
    for entry in staged:
        if not entry:continue
        meta,path=entry.split(b'\t',1);mode,oid,stage=meta.split();name=path.decode()
        if mode!=b'100644' or stage!=b'0' or name not in allowed:raise RuntimeError('UNAPPROVED_INDEX_ENTRY')
        data=git('cat-file','blob',oid.decode())
        if data!=(ROOT/name).read_bytes():raise RuntimeError('INDEX_WORKING_BYTES_DIFFER')
        findings+=scan(data,'index:'+name);staged_count+=1
    if staged_count and staged_count!=len(allowed):raise RuntimeError('INDEX_ALLOWLIST_MISMATCH')
    commits=git('rev-list','--all',allow_empty=True).decode().split() if history else []
    blobs=set()
    for oid in commits:
        findings+=scan(git('cat-file','commit',oid),'commit:'+oid[:12])
        names=set()
        for entry in git('ls-tree','-r','-z',oid).split(b'\0'):
            if not entry:continue
            meta,path=entry.split(b'\t',1);mode,typ,blob=meta.split();name=path.decode()
            if mode!=b'100644' or typ!=b'blob' or name not in allowed:raise RuntimeError('UNAPPROVED_HISTORY_ENTRY')
            names.add(name)
            if blob not in blobs:
                findings+=scan(git('cat-file','blob',blob.decode()),'history:'+name);blobs.add(blob)
        if names!=allowed:raise RuntimeError('HISTORY_ALLOWLIST_MISMATCH')
    # Print locations and rule names only, even on failure.
    result={'working_files':len(allowed),'staged_files':staged_count,'reachable_commits':len(commits),
            'unique_historical_blobs':len(blobs),'findings':[{'location':a,'line':b,'category':c} for a,b,c in findings],
            'conclusion':'Within the checked scope, no unapproved sensitive information was found.' if not findings else 'REVIEW_REQUIRED'}
    print(json.dumps(result,indent=2))
    return 1 if findings else 0
if __name__=='__main__':sys.exit(inspect())
