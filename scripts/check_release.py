"""Validate archive manifest and cold-run both delivered CLI entrypoints offline."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,subprocess,sys,tempfile,zipfile
p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
with tempfile.TemporaryDirectory(prefix='redfish_release_check_') as temporary:
    temporary=Path(temporary)
    with zipfile.ZipFile(a.directory/'redfish-workbench.zip') as z:
        for name in z.namelist():
            part=PurePosixPath(name)
            if part.is_absolute() or '..' in part.parts or not name.startswith('redfish-workbench/'):raise SystemExit('Unsafe archive path')
        z.extractall(temporary)
    root=temporary/'redfish-workbench';manifest=json.loads((root/'MANIFEST.sha256.json').read_text())
    for name,expected in manifest.items():
        path=root/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise SystemExit('Manifest mismatch: '+name)
    for entry in [root/'bmc_tool.pyz',root/'cli/bmc_tool.py']:
        result=subprocess.run([sys.executable,'-I','-S','-B',str(entry),'redfish','catalog'],cwd=temporary,capture_output=True,text=True,check=True)
        if len(json.loads(result.stdout)['operations'])!=69:raise SystemExit('Unexpected catalog count')
    print(f'Release PASS: {len(manifest)} manifest files; zipapp/source CLI cold run; zero BMC connections.')
