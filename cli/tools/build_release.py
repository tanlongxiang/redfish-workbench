"""Reproducible-layout, explicit-allowlist stdlib CLI release builder.
No real .env, reports, SQLite, device logs, images, caches or Web packages.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import tempfile
import zipapp
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'_engine' if (ROOT/'_engine').is_dir() else ROOT.parent/'core'
CORE=('redfish_catalog_data.py','redfish_ops.py','redfish_session.py','redfish_workflow.py','bmc_client.py','firmware_auth.py','firmware_batch.py','firmware_demo.py','firmware_readiness.py',
      'memory_inventory.py','memory_report.py','asset_catalog.py','asset_metadata.py','asset_inventory.py','inventory_merge.py','node_verify.py',
      'local_config.py','xlsx_io.py','workspace_paths.py','blowfish_constants.json')

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def copy_safe(src,dest):
    if src.is_symlink() or not src.is_file(): raise ValueError('Refusing absent or linked input: '+src.name)
    dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=ROOT/'dist')
    args=p.parse_args();output=args.output.resolve()
    if output.exists() and any(output.iterdir()): raise ValueError('Output directory is not empty; use a new location')
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bmc_cli_build_') as tmp:
        stage=Path(tmp)/'app';stage.mkdir()
        for name in CORE:copy_safe(ENGINE/name,stage/name)
        for path in sorted((ROOT/'bmc_cli').glob('*.py')):copy_safe(path,stage/'bmc_cli'/path.name)
        copy_safe(ENGINE/'blowfish_constants.json',stage/'bmc_cli/blowfish_constants.json')
        auth=stage/'firmware_auth.py';code=auth.read_text()
        old="raw = Path(__file__).with_name('blowfish_constants.json').read_bytes()"
        new="raw = __import__('pkgutil').get_data('bmc_cli', 'blowfish_constants.json') or b''"
        if old not in code: raise ValueError('Review firmware_auth resource loader before rebuilding')
        auth.write_text(code.replace(old,new))
        (stage/'blowfish_constants.json').unlink() # Public constants are now a package resource.
        (stage/'__main__.py').write_text('from bmc_cli.app import main\nraise SystemExit(main())\n')
        application=output/'bmc_tool.pyz'
        zipapp.create_archive(stage,application,interpreter='/usr/bin/env python3',compressed=True)
        # Build a maintainable source copy alongside the single-file executable.
        source=output/'source';source.mkdir()
        for name in CORE:copy_safe(ENGINE/name,source/'_engine'/name)
        for path in sorted((ROOT/'bmc_cli').glob('*.py')):copy_safe(path,source/'bmc_cli'/path.name)
        for path in sorted((ROOT/'tests').glob('*.py')):copy_safe(path,source/'tests'/path.name)
        for path in sorted((ROOT/'tools').glob('*.py')):copy_safe(path,source/'tools'/path.name)
        for name in ('bmc_tool.py','README.md','工具登记说明.txt','.env.example'):
            copy_safe(ROOT/name,source/name)
        for name in ('README.md','工具登记说明.txt','.env.example'):copy_safe(ROOT/name,output/name)
        for path in sorted((ROOT/'examples').glob('*')):
            copy_safe(path,output/'examples'/path.name);copy_safe(path,source/'examples'/path.name)
        (output/'bmc.bat').write_text('@echo off\nchcp 65001 >nul\npy -3 -I "%~dp0bmc_tool.pyz" %*\n',encoding='utf-8')
        (output/'bmc.sh').write_text('#!/usr/bin/env sh\nexec python3 -I "$(dirname "$0")/bmc_tool.pyz" "$@"\n')
        files=sorted(p for p in output.rglob('*') if p.is_file())
        for path in files:
            if any(x in {'.env','data','node_modules','.venv','__pycache__'} for x in path.relative_to(output).parts):raise ValueError('Forbidden release input')
        manifest={p.relative_to(output).as_posix():digest(p) for p in files}
        (output/'MANIFEST.sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'application':str(application),'sha256':digest(application),'files':len(manifest)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
