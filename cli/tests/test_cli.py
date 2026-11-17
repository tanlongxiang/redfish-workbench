"""Offline CLI regressions. Socket connections are forbidden for every test."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'_engine' if (ROOT/'_engine').exists() else ROOT.parent/'core'))
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from bmc_cli import app
from bmc_cli.targets import parse_text
from bmc_cli.reports import export_ledger
from asset_inventory import run_inventory
from firmware_batch import Options
from firmware_demo import DemoClient
from xlsx_io import InputBook

class OfflineCLI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=self.root/'state'
        original=app.engine.RUNS_DIR;self.addCleanup(setattr,app.engine,'RUNS_DIR',original)
        self.patches=[patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden')),
                      patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'))]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.csv=self.root/'nodes.csv';self.csv.write_text('节点名,BMC_IP,SN,位置,BMC_Active\nnode-01,192.0.2.11,001234567890123456789,R01,REFERENCE-BMC\n',encoding='utf-8')
        self.image=self.root/'FIXTURE_LT.svf';self.image.write_bytes(b'INERT-TEST-IMAGE')
    def invoke(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=app.main(list(map(str,argv)))
            except SystemExit as e:code=e.code
        return code,out.getvalue(),err.getvalue()
    def asset(self,verb,*argv):
        return self.invoke('asset',verb,'--state-dir',self.state,*argv)
    def test_help_and_doctor_without_third_party_imports(self):
        self.assertEqual(self.invoke('--help')[0],0)
        code,out,_=self.invoke('doctor');self.assertEqual(code,0);self.assertTrue(json.loads(out)['sqlite_json'])
        self.assertFalse(any(m in sys.modules for m in ('fastapi','pydantic','tkinter')))
    def test_generic_targets_ipv6_and_mapping_is_opt_in(self):
        targets=parse_text('other=192.0.2.1\nv6=[::1]:9443\na04r1n01 192.0.2.11')
        self.assertEqual(targets[1].address,'https://[::1]:9443')
        self.assertEqual(targets[2].address,'https://192.0.2.11')
        with self.assertRaises(ValueError):parse_text('a04r1n01 192.0.2.11',site_mapping=True)
        self.assertEqual(parse_text('a04r1n01',site_mapping=True)[0].address,'https://20.1.4.1')
    def test_duplicate_targets_and_url_credentials_rejected(self):
        self.assertEqual(self.invoke('targets','--target','a=192.0.2.1','--target','b=192.0.2.1')[0],2)
        self.assertEqual(self.invoke('targets','--target','https://user:pass@192.0.2.1')[0],2)
    def test_dry_run_does_not_read_env_or_construct_client(self):
        with patch.object(app,'read_env',side_effect=AssertionError('no implicit env')), \
             patch.object(app,'options',side_effect=AssertionError('no credentials')):
            code,out,_=self.invoke('power','off','--live','--target','fixture=192.0.2.11','--model','CY62-T58')
            self.assertEqual(code,0);self.assertIn('未连接、未写入',out)
    def test_upgrade_dry_run_shows_exact_hash_without_target_version(self):
        code,out,_=self.invoke('cpld','upgrade','--target','fixture=192.0.2.11','--model','CY62-T58','--image','02='+str(self.image))
        self.assertEqual(code,0);self.assertIn('Dcu02Cpld',out);self.assertIn('sha256',out)
        self.assertNotIn('Dcu13Cpld',out)
    def test_read_commands_require_live_before_credentials(self):
        for argv in (('collect',),('power','status'),('cpld','precheck','--image','02='+str(self.image),'--model','CY62-T58')):
            code,_,err=self.invoke(*argv,'--target','fixture=192.0.2.11')
            self.assertEqual(code,2);self.assertIn('--live',err)
    def test_pipe_cannot_approve_real_writes(self):
        with patch.object(sys.stdin,'isatty',return_value=False),patch.object(app,'options',side_effect=AssertionError('before connect')):
            for cmd in (('power','on'),('power','force-off'),('cpld','upgrade','--image','02='+str(self.image))):
                code,_,err=self.invoke(*cmd,'--target','fixture=127.0.0.1','--model','CY62-T58','--live','--execute')
                self.assertEqual(code,2);self.assertIn('交互终端',err)
    def test_password_argument_error_does_not_echo_password(self):
        code,out,err=self.invoke('collect','--password','SECRET-DO-NOT-ECHO')
        self.assertEqual(code,2);self.assertNotIn('SECRET-DO-NOT-ECHO',out+err)
    def test_credentials_only_explicit_file_and_no_shell_execution(self):
        path=self.root/'credentials.env';path.write_text('BMC_USERNAME=fixture-user\nBMC_PASSWORD="$(touch never-run)"\n')
        args=app.parser().parse_args(['collect','--target','127.0.0.1','--env-file',str(path)])
        with patch.dict(os.environ,{},clear=True):
            user,password=app.credentials(args)
        self.assertEqual(user,'fixture-user');self.assertEqual(password,'$(touch never-run)')
        self.assertFalse((self.root/'never-run').exists())
    def test_confirm_scope_one_run_phrase_and_timeout(self):
        scope={'action':'On','targets':[{'address':'https://127.0.0.1'}]}
        def correct(prompt):return prompt.split('原样输入：',1)[1].split('\n',1)[0]
        with patch.object(sys.stdin,'isatty',return_value=True),patch('builtins.input',side_effect=correct),contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(app.confirm_write(scope)['operator_confirmed'])
        with patch.object(sys.stdin,'isatty',return_value=True),patch('builtins.input',return_value='wrong'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):app.confirm_write(scope)
        with patch.object(sys.stdin,'isatty',return_value=True),patch('builtins.input',side_effect=correct),patch.object(app.time,'monotonic',side_effect=[0,121]),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'过期'):app.confirm_write(scope)
    def test_offline_ledger_import_edit_reference_statistics(self):
        self.assertEqual(self.asset('import','--file',self.csv)[0],0)
        code,out,_=self.asset('list','--version-source','reference');value=json.loads(out)
        self.assertEqual(code,0);self.assertEqual(value['filtered'],1)
        self.assertEqual(value['items'][0]['metadata']['declared_sn'],'001234567890123456789')
        self.assertEqual(value['items'][0]['collection_state'],'uncollected')
        self.assertEqual(self.asset('set','--address','192.0.2.11','--location','R02','--lifecycle','repair')[0],0)
        value=json.loads(self.asset('list','--lifecycle','repair')[1]);self.assertEqual(value['filtered'],1)
        self.assertEqual(value['items'][0]['metadata']['location'],'R02')
    def test_export_text_sn_no_overwrite_and_csv_formula_guard(self):
        self.asset('import','--file',self.csv)
        self.asset('set','--address','192.0.2.11','--notes','=1+1')
        output=self.root/'assets.xlsx';self.assertEqual(self.asset('export','--output',output)[0],0)
        book=InputBook(str(output));rows=book.read_sheet(next(iter(book.sheets)))
        self.assertIn('001234567890123456789',rows[1][1])
        before=output.read_bytes();self.assertEqual(self.asset('export','--output',output)[0],2);self.assertEqual(output.read_bytes(),before)
        output=self.root/'assets.csv';self.asset('export','--output',output)
        self.assertIn("'=1+1",output.read_text(encoding='utf-8-sig'))
    def test_stats_is_offline_and_cannot_overwrite_input(self):
        code,out,_=self.invoke('stats','--input',self.csv)
        self.assertEqual(code,0);self.assertEqual(json.loads(out)['total'],1)
        before=self.csv.read_bytes()
        self.assertEqual(self.invoke('stats','--input',self.csv,'--output',self.csv)[0],2)
        self.assertEqual(self.csv.read_bytes(),before)
    def test_full_memory_demo_and_labeled_inventory(self):
        with patch.object(app,'WriteApproval',side_effect=AssertionError('no live approval in demo')):
            code,out,err=self.invoke('demo','--output',self.root/'reports')
        self.assertEqual(code,0,err);summary=json.loads(out)
        self.assertTrue(summary['On_blocked']);self.assertTrue(summary['unselected_unchanged'])
        self.assertEqual(summary['real_device_connections'],0)
        result=json.loads(Path(summary['inventory_results']).read_text())
        self.assertTrue(all('模拟' in r['mode'] for r in result))
        self.assertEqual(self.invoke('stats','--input',summary['inventory_results'])[0],0)
    def test_basic_collect_does_not_poll_38_firmware_endpoints(self):
        _,catalog=app.configure_state(self.state)
        opts=Options('fixture','fixture',model='CY62-T58')
        target=app.Target('DEMO-01','192.0.2.11');stop=threading.Event()
        operation=app.Operation()
        cb=app.callback_for(catalog,'demo',operation)
        results,_=run_inventory([target],opts,stop,cb,self.root/'reports',demo=True,firmware=False)
        self.assertEqual(results[0]['collection_state'],'basic');self.assertEqual(results[0]['firmware'],{})
    def test_write_error_stops_later_operations(self):
        _,catalog=app.configure_state(self.state);operation=app.Operation()
        cb=app.callback_for(catalog,'demo',operation,writing=True)
        cb(app.Target('fixture','127.0.0.1'),'停止并待核验',{'reason':'结果未知'})
        self.assertTrue(operation.stop.is_set())
    def test_help_modes_do_not_create_state_directory(self):
        self.invoke('targets','--targets',self.csv,'--state-dir',self.state)
        self.assertFalse(self.state.exists())

if __name__=='__main__':unittest.main(verbosity=2)
