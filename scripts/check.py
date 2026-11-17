"""Portable development entrypoint. Tests connect only to local fixtures, never BMCs."""
from pathlib import Path
import argparse,os,shutil,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
def run(command,env=None):
    print('+ '+' '.join(map(str,command)),flush=True)
    subprocess.run(list(map(str,command)),cwd=ROOT,env=env,check=True)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--python',default=sys.executable,help='Python interpreter containing web test dependencies')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor');sub.add_parser('build-web');sub.add_parser('security')
    test=sub.add_parser('test');test.add_argument('--suite',choices=['all','repo','core','cli','web'],default='all')
    browser=sub.add_parser('browser');browser.add_argument('--legacy',action='store_true')
    args=p.parse_args()
    if args.command=='doctor':
        for name in ['git','node','npm','openssl']:
            path=shutil.which(name);print(name+': '+('available' if path else 'missing'))
        print('Python: '+sys.version.split()[0]);print('No credentials loaded; no device connections.');return 0
    if args.command=='security':run([args.python,'-I','-B',ROOT/'scripts/security_check.py']);return 0
    if args.command=='build-web':
        npm=shutil.which('npm.cmd' if os.name=='nt' else 'npm')
        if not npm:raise SystemExit('Node.js/npm missing; see docs/DEVELOPMENT.md')
        run([npm,'--prefix',ROOT/'web/frontend','run','build']);return 0
    env={k:v for k,v in os.environ.items() if not k.startswith('BMC_')}
    env.update(BMC_USERNAME='',BMC_PASSWORD='',BMC_CLI_APP=str(ROOT/'cli/bmc_tool.py'),PYTHONDONTWRITEBYTECODE='1')
    if args.command=='test':
        if not shutil.which('openssl'):raise SystemExit('openssl is required for the HTTPS fixture tests')
        suites=['repo','core','cli','web'] if args.suite=='all' else [args.suite]
        for suite in suites:
            directory=ROOT/('tests' if suite=='repo' else 'core' if suite=='core' else suite+'/tests')
            flags=['-I','-B']+([] if suite=='web' else ['-S'])
            run([args.python,*flags,'-m','unittest','discover','-s',directory,'-p','test_*.py'],env)
    else:
        for name in ['redfish_ui_smoke.py','production_ui_smoke.py']+(['browser_smoke.py'] if args.legacy else []):
            run([args.python,'-I','-B',ROOT/'web/tests'/name],env)
    return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except subprocess.CalledProcessError as exc:raise SystemExit(exc.returncode)
