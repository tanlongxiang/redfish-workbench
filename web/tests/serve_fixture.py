"""Offline browser-test fixture, deliberately separate from the normal launcher.
Never use this file as a normal maintenance entrypoint. All BMC networking is denied.
"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import argparse
import socket
from unittest.mock import patch
import uvicorn
from bmc_web.app import create_app
from bmc_web.bridge import BmcClient, UpgradeClient
from bmc_web.service import Settings

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    args=parser.parse_args()
    app=create_app(Settings(args.data_dir,origin=f'http://127.0.0.1:{args.port}',test_simulation=True,
                            credential_provider=lambda:('fixture-user','fixture-password')))
    denied=AssertionError('Offline fixture forbids all outgoing connections and real BMC requests')
    with patch.object(socket.socket,'connect',side_effect=denied), \
         patch.object(socket,'create_connection',side_effect=denied), \
         patch.object(BmcClient,'request',side_effect=denied), \
         patch.object(UpgradeClient,'request',side_effect=denied):
        uvicorn.run(app,host='127.0.0.1',port=args.port,workers=1,access_log=False,log_level='warning')
