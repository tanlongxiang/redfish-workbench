"""Build from the audited Git index, plus freshly built frontend assets. No field data."""
from pathlib import Path
import argparse,hashlib,json,subprocess,sys,zipfile
ROOT=Path(__file__).resolve().parents[1]
def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args])
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    output=a.output.resolve()
    if output==ROOT or not output.is_relative_to(ROOT/'dist'):raise SystemExit('Output must be a new directory under repository dist/')
    if output.exists() and any(output.iterdir()):raise SystemExit('Output not empty; refusing overwrite')
    subprocess.run([sys.executable,'-I','-B',ROOT/'scripts/security_check.py'],check=True)
    if subprocess.run(['git','-C',str(ROOT),'diff','--quiet']).returncode:raise SystemExit('Stage reviewed changes before packaging; worktree differs from index')
    frontend=ROOT/'web/frontend/dist'
    if not (frontend/'index.html').is_file():raise SystemExit('Build web/frontend first')
    subprocess.run([sys.executable,'-I','-B',ROOT/'cli/tools/build_release.py','--output',output/'cli'],check=True)
    entries={}
    for raw in git('ls-files','-z').split(b'\0'):
        if raw:
            name=raw.decode();entries[name]=git('show',':'+name)
    source_manifest={n:hashlib.sha256(v).hexdigest() for n,v in sorted(entries.items())}
    for path in frontend.rglob('*'):
        if path.is_file():
            if path.is_symlink() or path.suffix not in {'.html','.js','.css','.svg','.png','.ico','.woff2','.txt'}:raise SystemExit('Unapproved frontend artifact')
            entries[path.relative_to(ROOT).as_posix()]=path.read_bytes()
    entries['bmc_tool.pyz']=(output/'cli/bmc_tool.pyz').read_bytes()
    entries['build-info.json']=(json.dumps({'format':'redfish-workbench-release-v1','source_tree_sha256':hashlib.sha256(json.dumps(source_manifest,sort_keys=True).encode()).hexdigest(),'hardware_acceptance':False,'note':'This manifest describes packaged files, not a new hardware validation result.'},indent=2)+'\n').encode()
    manifest={n:hashlib.sha256(v).hexdigest() for n,v in sorted(entries.items())}
    entries['MANIFEST.sha256.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
    archive=output/'redfish-workbench.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for name,body in sorted(entries.items()):z.writestr('redfish-workbench/'+name,body)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    with archive.with_suffix('.zip.sha256').open('x') as f:f.write(digest+'  '+archive.name+'\n')
    print(json.dumps({'archive':str(archive),'files':len(entries),'sha256':digest},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
