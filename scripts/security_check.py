"""Inspect INDEX blobs, not worktree copies. Never print a detected secret value."""
from pathlib import PurePosixPath,Path
import argparse,re,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN={'.env','data','var','node_modules','.venv','__pycache__','.git','01_原始资料','02_最终成果','03_采集记录','04_历史归档','05_工具包'}
SUFFIXES={'.sqlite3','.sqlite','.db','.xlsx','.xls','.pdf','.pcap','.pcapng','.zip','.pyz','.tar','.svf','.ima','.rom','.jed','.bin','.pem','.key','.p12','.pfx','.log','.jsonl'}
PATTERNS=[('private-key',re.compile(rb'-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----')),
          ('github-token',re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}')),
          ('cloud-access-key',re.compile(rb'AKIA[A-Z0-9]{16}'))]
def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args])
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--staged',action='store_true',help='compatibility flag; always checks all indexed blobs');p.parse_args()
    paths=git('ls-files','-z').split(b'\0');problems=[];count=0
    for raw in paths:
        if not raw:continue
        name=raw.decode('utf-8');path=PurePosixPath(name);count+=1
        if any(x in FORBIDDEN or x.startswith('dist') or (x.startswith('.env') and x!='.env.example') for x in path.parts):problems.append((name,'forbidden-path'))
        if path.name in {'access-key','service.lock'} or path.suffix.lower() in SUFFIXES:problems.append((name,'runtime-or-private-file'))
        if path.suffix=='.csv' and not name.startswith('cli/examples/'):problems.append((name,'non-example-csv'))
        blob=git('show',':'+name)
        if len(blob)>1024*1024:problems.append((name,'file-over-1MiB'))
        if path.name=='.env.example':
            for line in blob.decode('utf-8').splitlines():
                if line.strip().startswith(('BMC_USERNAME=','BMC_PASSWORD=')) and line.split('=',1)[1].strip():problems.append((name,'nonempty-credential-template'))
        for rule,pattern in PATTERNS:
            if pattern.search(blob):problems.append((name,rule))
    if not count:
        print('No indexed files: stage reviewed sources before validation.',file=sys.stderr);return 1
    for name,rule in problems:print(f'BLOCKED: {name} [{rule}]',file=sys.stderr)
    if problems:return 1
    print(f'INDEX hygiene passed: {count} files; no forbidden files or high-confidence secret patterns. This is not a full secret audit.')
    return 0
if __name__=='__main__':raise SystemExit(main())
