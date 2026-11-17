"""Test-only entrypoint: real web app + 127.0.0.1 HTTPS BMC, never site credentials."""
from pathlib import Path
import sys,argparse,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'cli/tests'))
import uvicorn
from unittest.mock import patch
from redfish_fixture import Fixture
from bmc_web.app import create_app
from bmc_web.service import Settings
from bmc_web.bridge import BmcClient,UpgradeClient
from redfish_session import ManagedSession
p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);args=p.parse_args()
fixture=Fixture()
try:
 app=create_app(Settings(args.data_dir,origin=f'http://127.0.0.1:{args.port}',test_simulation=True,credential_provider=lambda:('fixture-user','fixture-password')))
 def loopback(address,*args):
  if address!=fixture.address:raise AssertionError('Fixture forbids non-loopback BMC')
  return ManagedSession(address,*args)
 app.state.redfish.client_factory=loopback
 (args.data_dir/'fixture.json').write_text(json.dumps({'address':fixture.address}))
 with patch.object(BmcClient,'request',side_effect=AssertionError('Legacy BMC forbidden')),patch.object(UpgradeClient,'request',side_effect=AssertionError('Legacy BMC forbidden')):
  uvicorn.run(app,host='127.0.0.1',port=args.port,workers=1,access_log=False,log_level='warning')
finally:fixture.close()
