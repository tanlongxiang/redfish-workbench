"""Offline/unit tests plus a local HTTPS Redfish fixture; never contacts a real BMC."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

import base64
import hashlib
import http.server
import json
import io
from contextlib import redirect_stdout
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from bmc_client import ApiError, BmcClient, BmcError, normalize_target
import node_verify as nv
from xlsx_io import InputBook, write_xlsx
from local_config import read_env, load_credentials


SOURCE_SN = '9000000000000001'  # synthetic identifier, not a site SN
ORIGINAL_SN = 'TEST-BASELINE-SN-0001'


def plan(status='SN冲突', old=ORIGINAL_SN, new=SOURCE_SN):
    a = nv.Record('a04r2n15', old, '20.1.4.35', 'sheet', 256) if old is not None else None
    b = nv.Record('a04r2n15', new, '', 'a圈', 35)
    return nv.Plan('a04r2n15', '20.1.4.35', a, b, status)


class FixtureClient:
    serial = SOURCE_SN
    multiple = False
    fail_login = False
    fail_chassis = False
    instances = []
    def __init__(self, *args):
        self.root = {'Systems': {'@odata.id': '/redfish/v1/Systems'}}
        self.calls = []
        self.closed = False
        type(self).instances.append(self)
    def login(self, *args):
        if self.fail_login:
            raise ApiError(401, '/redfish/v1/')
    def request(self, method, path):
        self.calls.append((method, path))
        if path == '/redfish/v1/Systems':
            members = [{'@odata.id': '/redfish/v1/Systems/node42'}]
            if self.multiple:
                members += [{'@odata.id': '/redfish/v1/Systems/other'}]
            return {'Members': members}, {}
        if path == '/redfish/v1/Systems/node42':
            return {'Id': 'node42', 'SerialNumber': self.serial,
                    'Links': {'Chassis': [{'@odata.id': '/redfish/v1/Chassis/c42'}]}}, {}
        if path == '/redfish/v1/Chassis/c42':
            if self.fail_chassis:
                raise ApiError(403, path)
            return {'SerialNumber': 'CHASSIS-SEPARATE'}, {}
        raise ApiError(404, path)
    def close(self):
        self.closed = True
        return ''


class CoreTests(unittest.TestCase):
    def setUp(self):
        FixtureClient.serial = SOURCE_SN
        FixtureClient.multiple = FixtureClient.fail_login = FixtureClient.fail_chassis = False
        FixtureClient.instances = []
    def verify(self, p=None, **kwargs):
        return nv.verify_one(p or plan(), 'fixture-user', 'fixture-only-password', threading.Event(), client_class=FixtureClient, **kwargs)
    def test_mapping_circles(self):
        for node, ip in [('a04r2n15', '20.1.4.35'), ('b16r4n19', '20.2.16.79'),
                         ('j14r1n01', '20.9.14.1'), ('m01r2n01', '20.11.1.21'), ('n13r4n19', '20.12.13.79')]:
            self.assertEqual(nv.node_address(node), ip)
    def test_mapping_reject_invalid(self):
        for n in ['i01r1n01', 'a17r1n01', 'a00r1n01', 'a01r5n01', 'a01r1n21']:
            with self.assertRaises(ValueError): nv.node_address(n)
    def test_placeholders(self):
        for sn in ['01', '1', 'NA', 'N/A', '0', '']:
            self.assertFalse(nv.has_sn(sn))
        self.assertTrue(nv.has_sn(SOURCE_SN))
    def test_compare_all_outcomes(self):
        self.assertEqual(nv.compare_live(plan(), SOURCE_SN), '与来源一致（总表冲突）')
        self.assertEqual(nv.compare_live(plan(), ORIGINAL_SN), '与总表一致（来源不符）')
        self.assertEqual(nv.compare_live(plan(old=SOURCE_SN), SOURCE_SN), '三方一致')
        self.assertEqual(nv.compare_live(plan(old='01'), SOURCE_SN), '补录SN已验证')
        self.assertEqual(nv.compare_live(plan(old=None), SOURCE_SN), '新增节点SN已验证')
        self.assertEqual(nv.compare_live(plan(), 'OTHER'), '与两表均不一致')
        self.assertEqual(nv.compare_live(plan(), 'NA'), '现场SN缺失或占位')
    def test_suffix_is_not_equivalent(self):
        p = plan(old=SOURCE_SN+'A')
        r = self.verify(p)
        self.assertEqual(r['status'], '与来源一致（总表冲突）')
        self.assertTrue(any('尾缀' in n for n in r['notes']))
    def test_dynamic_system_discovery(self):
        r = self.verify()
        self.assertEqual(r['system_sn'], SOURCE_SN)
        self.assertEqual(r['chassis_sn'], 'CHASSIS-SEPARATE')
        self.assertIn(('GET', '/redfish/v1/Systems/node42'), FixtureClient.instances[0].calls)
        self.assertTrue(FixtureClient.instances[0].closed)
    def test_multiple_systems_not_guessed(self):
        FixtureClient.multiple = True
        r = self.verify()
        self.assertEqual(r['status'], '读取失败')
        self.assertEqual(len(FixtureClient.instances[0].calls), 1)
    def test_paginated_systems_not_guessed(self):
        class Paginated(FixtureClient):
            def request(self, method, path):
                data, headers = super().request(method, path)
                if path == '/redfish/v1/Systems':
                    data['Members@odata.nextLink'] = '/redfish/v1/Systems?$skip=1'
                    data['Members@odata.count'] = 2
                return data, headers
        r = nv.verify_one(plan(), 'u', 'p', threading.Event(), client_class=Paginated)
        self.assertEqual(r['status'], '读取失败')
        self.assertEqual(r['system_sn'], '')

    def test_chassis_not_used_as_system_sn(self):
        FixtureClient.serial = None
        r = self.verify()
        self.assertEqual(r['system_sn'], '')
        self.assertEqual(r['status'], '现场SN缺失或占位')
    def test_float_sn_rejected(self):
        FixtureClient.serial = 9000000000000001.0
        self.assertEqual(self.verify()['system_sn'], '')
    def test_invalid_sn_types(self):
        for value in [True, [], {}, False]:
            FixtureClient.serial = value
            self.assertEqual(self.verify()['system_sn'], '')
    def test_bad_credentials_no_retry(self):
        FixtureClient.fail_login = True
        r = self.verify()
        self.assertEqual(r['status'], '读取失败')
        self.assertEqual(len(FixtureClient.instances), 1)
        self.assertTrue(FixtureClient.instances[0].closed)
    def test_chassis_failure_preserves_system(self):
        FixtureClient.fail_chassis = True
        r = self.verify()
        self.assertEqual(r['status'], '与来源一致（总表冲突）')
        self.assertTrue(any('Chassis读取失败' in n for n in r['notes']))
    def test_stop_before_login(self):
        stop = threading.Event(); stop.set()
        r = nv.verify_one(plan(), 'u', 'p', stop, client_class=FixtureClient)
        self.assertEqual(r['status'], '已取消')
        self.assertEqual(FixtureClient.instances, [])
    def test_address_error_no_connection(self):
        p = plan(); p.address_error = True
        self.assertEqual(self.verify(p)['status'], '地址异常，未采集')
        self.assertEqual(FixtureClient.instances, [])
    def test_firmware_38_and_404_preserves_sn(self):
        self.assertEqual(len(nv.FIRMWARE), 38)
        r = self.verify(firmware=True)
        self.assertEqual(len(r['firmware']), 38)
        self.assertEqual(r['status'], '与来源一致（总表冲突）')
        headers, rows = nv.report_data([plan()], {plan().node: r})
        self.assertEqual(len(headers), 15+38)
        self.assertEqual(len(rows[0]), len(headers))
    def test_firmware_version_one_is_valid(self):
        p = plan(); p.baseline.fields['Bios'] = '1'
        r = self.verify(); r['firmware']['Bios'] = '2'
        _, rows = nv.report_data([p], {p.node: r})
        self.assertIn('Bios: 1 → 2', rows[0][8])
    def test_target_restricts_paths_and_credentials(self):
        for ip in ['http://127.0.0.1', 'https://user:pass@127.0.0.1', 'https://127.0.0.1/a', 'https://127.0.0.1?x=1']:
            with self.assertRaises(BmcError): normalize_target(ip)
    def test_transport_methods_restricted(self):
        c = BmcClient('127.0.0.1')
        for method, path in [('PATCH', '/redfish/v1/Systems/1'), ('POST', '/bad'), ('DELETE', '/other')]:
            with self.assertRaises(BmcError): c.request(method, path)
        c.close()
    def test_transport_cross_origin_rejected(self):
        c = BmcClient('127.0.0.1')
        with self.assertRaises(BmcError): c.safe_path('https://127.0.0.2/redfish/v1/')
        c.close()
    def test_offline_report_marks_uncollected(self):
        _, rows = nv.report_data([plan()])
        self.assertEqual(rows[0][7], '未采集')
    def test_unknown_requested_node_fails(self):
        with self.assertRaises(ValueError): nv.select_plans([plan()], nodes=['a01r1n01'])


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'.env'
    def test_missing_file(self):
        self.assertEqual(load_credentials(filename=self.path, environ={}), ('', ''))
    def test_plain_file(self):
        self.path.write_text('BMC_USERNAME=fixture-admin\nBMC_PASSWORD=fixture-secret\n')
        self.assertEqual(load_credentials(filename=self.path, environ={}), ('fixture-admin', 'fixture-secret'))
    def test_quoted_password_preserves_spaces(self):
        self.path.write_text('export BMC_USERNAME="fixture-admin"\nBMC_PASSWORD=" pass # word "\n')
        self.assertEqual(load_credentials(filename=self.path, environ={}), ('fixture-admin', ' pass # word '))
    def test_environment_wins_without_mutation(self):
        self.path.write_text('BMC_USERNAME=file-user\nBMC_PASSWORD=file-pass\n')
        environment = {'BMC_USERNAME': 'env-user', 'BMC_PASSWORD': 'env-pass'}
        self.assertEqual(load_credentials(filename=self.path, environ=environment), ('env-user', 'env-pass'))
        self.assertEqual(environment, {'BMC_USERNAME': 'env-user', 'BMC_PASSWORD': 'env-pass'})
    def test_cli_username_wins(self):
        self.path.write_text('BMC_USERNAME=file-user\nBMC_PASSWORD=file-pass\n')
        self.assertEqual(load_credentials('cli-user', filename=self.path, environ={'BMC_USERNAME': 'env-user'}), ('cli-user', 'file-pass'))
    def test_empty_environment_does_not_use_file_password(self):
        self.path.write_text('BMC_PASSWORD=file-pass\n')
        self.assertEqual(load_credentials(filename=self.path, environ={'BMC_PASSWORD': ''}), ('', ''))
    def test_values_are_not_executed_or_interpolated(self):
        self.path.write_text('OTHER=ignored\nBMC_PASSWORD=$(echo forbidden)\n')
        self.assertEqual(read_env(self.path), {'BMC_PASSWORD': '$(echo forbidden)'})
    def test_parse_error_does_not_echo_secret(self):
        self.path.write_text('BMC_PASSWORD="fixture-secret\n')
        with self.assertRaises(ValueError) as exc:
            read_env(self.path)
        self.assertNotIn('fixture-secret', str(exc.exception))


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root/'base.xlsx'; self.source = self.root/'source.xlsx'
        write_xlsx(str(self.base), ['节点名', 'BMC_IP', 'SN'], [['a04r2n15', '20.1.4.35', ORIGINAL_SN]])
        write_xlsx(str(self.source), ['完整节点', 'SN'], [['a04r2n15', SOURCE_SN]])
    def test_plan_and_export_roundtrip_exact_text(self):
        plans = nv.build_plan(self.base, self.source)
        self.assertEqual(plans[0].status, 'SN冲突')
        out = self.root/'report.xlsx'
        nv.export_report(out, plans, inputs=[self.base, self.source])
        book = InputBook(str(out)); data = book.read_sheet(next(iter(book.sheets)))
        self.assertEqual(data[1][1][3:5], [ORIGINAL_SN, SOURCE_SN])
    def test_cli_uses_config_without_interactive_prompt(self):
        captured = io.StringIO()
        with patch.object(nv, 'load_credentials', return_value=('configured-user', 'configured-password')), \
             patch.object(nv, 'run_live', return_value={}) as live, \
             patch('builtins.input', side_effect=AssertionError('must not prompt')), \
             patch.object(nv.getpass, 'getpass', side_effect=AssertionError('must not prompt')), redirect_stdout(captured):
            code = nv.main(['--baseline', str(self.base), '--source', str(self.source), '--scope', 'conflict', '--live'])
        self.assertEqual(code, 0)
        self.assertEqual(live.call_args.args[1:3], ('configured-user', 'configured-password'))
        self.assertNotIn('configured-password', captured.getvalue())

    def test_prevent_input_overwrite(self):
        with self.assertRaises(ValueError): nv.export_report(self.base, nv.build_plan(self.base, self.source), inputs=[self.base])
    def test_prevent_existing_output_overwrite(self):
        out = self.root/'already.xlsx'; out.write_bytes(b'keep')
        with self.assertRaises(ValueError): nv.export_report(out, nv.build_plan(self.base, self.source))
        self.assertEqual(out.read_bytes(), b'keep')
    def test_source_duplicate_node(self):
        write_xlsx(str(self.source), ['完整节点', 'SN'], [['a04r2n15', SOURCE_SN]]*2)
        with self.assertRaises(ValueError): nv.read_records(self.source)
    def test_scientific_notation_rejected(self):
        write_xlsx(str(self.source), ['完整节点', 'SN'], [['a04r2n15', '9.000000000000001E+15']])
        with self.assertRaises(ValueError): nv.read_records(self.source)
    def test_wrong_ip_blocked(self):
        write_xlsx(str(self.base), ['节点名', 'BMC_IP', 'SN'], [['a04r2n15', '20.1.4.36', ORIGINAL_SN]])
        self.assertTrue(nv.build_plan(self.base, self.source)[0].address_error)
    def test_missing_node_address_inferred(self):
        write_xlsx(str(self.base), ['节点名', 'BMC_IP', 'SN'], [['a04r2n17', '20.1.4.37', ORIGINAL_SN]])
        p = nv.build_plan(self.base, self.source)[0]
        self.assertEqual((p.status, p.ip), ('总表无此节点', '20.1.4.35'))
    def test_whitelisted_evidence_no_auth(self):
        plans = nv.build_plan(self.base, self.source)
        r = nv.verify_one(plans[0], 'user-for-fixture', 'secret-for-fixture', threading.Event(), client_class=FixtureClient)
        out = self.root/'live.xlsx'
        nv.export_report(out, plans, {plans[0].node: r}, inputs=[self.base, self.source])
        raw = (self.root/'live_现场证据.jsonl').read_text()
        self.assertNotIn('user-for-fixture', raw)
        self.assertNotIn('secret-for-fixture', raw)
        self.assertNotIn('Authorization', raw)
    def test_synthetic_workbooks_are_unchanged(self):
        # A clone must not need or inspect anyone's field workbooks.
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in [self.base, self.source]]
        plans = nv.build_plan(self.base, self.source)
        self.assertEqual(dict(nv.Counter(p.status for p in plans)), {'SN冲突': 1})
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in [self.base, self.source]])


@unittest.skipUnless(shutil.which('openssl'), 'openssl不可用，跳过本机HTTPS夹具')
class TransportIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name)
        cert, key = folder/'cert.pem', folder/'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                        '-out', str(cert), '-subj', '/CN=localhost', '-days', '1'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.seen = []
        owner = cls
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def send(self, code, data, headers=None):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                for k, v in (headers or {}).items(): self.send_header(k, v)
                self.end_headers(); self.wfile.write(body)
            def authorized(self):
                return self.headers.get('Authorization') == 'Basic '+base64.b64encode(b'fixture-user:fixture-password').decode() or self.headers.get('X-Auth-Token') == 'fixture-token'
            def do_GET(self):
                owner.seen.append(('GET', self.path, self.authorized()))
                if not self.authorized(): return self.send(401, {})
                if self.path == '/redfish/v1/': return self.send(200, {'Systems': {'@odata.id': '/redfish/v1/Systems'}})
                if self.path == '/redfish/v1/Systems': return self.send(200, {'Members': [{'@odata.id': '/redfish/v1/Systems/not-one'}]})
                if self.path == '/redfish/v1/Systems/not-one': return self.send(200, {'SerialNumber': SOURCE_SN})
                return self.send(404, {})
            def do_POST(self):
                owner.seen.append(('POST', self.path, False))
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                if self.path != '/redfish/v1/SessionService/Sessions' or data != {'UserName': 'fixture-user', 'Password': 'fixture-password'}:
                    return self.send(401, {})
                return self.send(201, {}, {'X-Auth-Token': 'fixture-token', 'Location': '/redfish/v1/SessionService/Sessions/test'})
            def do_DELETE(self):
                owner.seen.append(('DELETE', self.path, self.authorized()))
                return self.send(200, {})
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.target = f'127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.tmp.cleanup()
    def test_basic_auth_on_protected_root(self):
        self.seen.clear()
        p = plan(); p.ip = self.target
        r = nv.verify_one(p, 'fixture-user', 'fixture-password', threading.Event(), allow_self_signed=True)
        self.assertEqual(r['system_sn'], SOURCE_SN)
        self.assertEqual(self.seen[0], ('GET', '/redfish/v1/', True))
        self.assertTrue(all(method=='GET' for method, path, auth in self.seen))
    def test_session_created_and_only_own_session_deleted(self):
        self.seen.clear()
        p = plan(); p.ip = self.target
        r = nv.verify_one(p, 'fixture-user', 'fixture-password', threading.Event(), allow_self_signed=True, auth='session')
        self.assertEqual(r['system_sn'], SOURCE_SN)
        self.assertEqual(self.seen[0][:2], ('POST', '/redfish/v1/SessionService/Sessions'))
        self.assertEqual(self.seen[-1][:2], ('DELETE', '/redfish/v1/SessionService/Sessions/test'))
    def test_untrusted_certificate_rejected(self):
        p = plan(); p.ip = self.target
        r = nv.verify_one(p, 'fixture-user', 'fixture-password', threading.Event())
        self.assertEqual(r['status'], '读取失败')
        self.assertTrue(any('证书' in n for n in r['notes']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
