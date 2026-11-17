"""Offline safety/state-machine tests; localhost HTTPS test is also included below."""
from dataclasses import replace
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import base64
import ctypes
import ctypes.util
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from bmc_client import BmcClient, BmcError
from firmware_auth import Blowfish, confirmation_key
from firmware_batch import (COMPONENTS, CPLD, INVENTORY, Image, Options, Target, NodeLock, UpgradeClient,
                            parse_targets, import_targets, run_batch, snapshot, check, save_config, load_config, WriteApproval)
from firmware_demo import DemoClient, DEMO_MARKER
from xlsx_io import InputBook, write_xlsx

class RecordingClient(DemoClient):
    writes = []
    behavior = ''
    after_write = None
    def write(self, method, path, payload, **kwargs):
        type(self).writes.append((self.address, method, path, payload if not isinstance(payload, bytes) else 'image'))
        if type(self).behavior == 'write-error' and path.endswith('RequestedActivation'):
            raise BmcError('write failed')
        result = super().write(method, path, payload, **kwargs)
        if type(self).after_write:
            type(self).after_write(path)
        return result
    def cpld(self):
        obj = super().cpld()
        if type(self).behavior == 'stale':
            obj['Activation'] = 'Active'; obj['Progress'] = 100
            obj['VersionList'] = dict.fromkeys(COMPONENTS, '0.3')
        if type(self).behavior == 'busy':
            obj['Activation'] = 'Activating'
        if type(self).behavior == 'bad-vendor':
            obj['VendorList'][COMPONENTS[0]] = 'Other'
        if type(self).behavior == 'acoff':
            obj['EffectiveList'][0] = [COMPONENTS[0], 'acoff', 'poweroff']
        if type(self).behavior == 'target-race' and type(self).writes:
            obj['ExtendedVersion'] = 'OTHER'
        return obj
    def system(self):
        obj = super().system()
        if type(self).behavior == 'power-on':
            obj['PowerState'] = 'On'
        if type(self).behavior == 'bad-model':
            obj['Model'] = 'OTHER'
        return obj

class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'DEMO_LT_TEST.svf'
        self.path.write_bytes(DEMO_MARKER + b'0.5\n')
        self.images = [Image.prepare(c, self.path, '0.5') for c in COMPONENTS]
        self.options = Options('test-user', 'fixture-password', model='CY62-T58', poll=.005,
                               flash_timeout=1, canary_confirmed=True)
        self.targets = [Target('demo-1', '192.0.2.151'), Target('demo-2', '192.0.2.152')]
        DemoClient.states.clear(); RecordingClient.writes = []; RecordingClient.behavior = ''; RecordingClient.after_write = None
    def run_(self, action='upgrade', **kwargs):
        return run_batch(kwargs.pop('targets', self.targets), kwargs.pop('images', self.images),
                         kwargs.pop('options', self.options), action, folder=self.folder,
                         client_class=RecordingClient, **kwargs)
    def test_blowfish_standard_vector(self):
        self.assertEqual(Blowfish(bytes(8)).encrypt(bytes(8)).hex(), '4ef997456198dd78')
        self.assertEqual(Blowfish(bytes.fromhex('ffffffffffffffff')).encrypt(bytes.fromhex('ffffffffffffffff')).hex(), '51866fd5b85ecb8a')
    def test_auth_against_independent_openssl(self):
        library = ctypes.util.find_library('crypto')
        if not library:
            self.skipTest('libcrypto unavailable')
        lib = ctypes.CDLL(library)
        if not hasattr(lib, 'BF_set_key'):
            self.skipTest('Blowfish unavailable in libcrypto')
        class Key(ctypes.Structure):
            _fields_ = [('P', ctypes.c_uint32*18), ('S', ctypes.c_uint32*1024)]
        lib.BF_set_key.argtypes = [ctypes.POINTER(Key), ctypes.c_int, ctypes.c_void_p]
        lib.BF_ecb_encrypt.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Key), ctypes.c_int]
        key = Key(); lib.BF_set_key(ctypes.byref(key), 7, b'megarac')
        for password in ('a', 'fixture-password', 'a'*24):
            raw = password.encode().ljust(24, b'\0'); output = b''
            for i in range(0,24,8):
                buf = ctypes.create_string_buffer(8)
                lib.BF_ecb_encrypt(raw[i:i+8], buf, ctypes.byref(key), 1)
                output += buf.raw
            self.assertEqual(confirmation_key(password), base64.b64encode(output).decode())
    def test_password_limits(self):
        for p in ('', 'a'*25, '中文', 'a\0b'):
            with self.assertRaises(ValueError): confirmation_key(p)
    def test_parse_mapping_and_duplicates(self):
        self.assertEqual(parse_targets('a04r2n15')[0].address, 'https://20.1.4.35')
        for value in ('a04r2n15 20.1.4.36', 'x 192.0.2.1\ny 192.0.2.1', 'https://u:p@192.0.2.1'):
            with self.assertRaises((ValueError, BmcError)): parse_targets(value)
    def test_import_csv_xlsx(self):
        p = self.folder/'nodes.csv'; p.write_text('节点名,BMC_IP\na04r2n15,20.1.4.35\n')
        self.assertEqual(len(import_targets(p)),1)
        p = self.folder/'nodes.xlsx'; write_xlsx(str(p),['节点名','BMC_IP'],[['a04r2n15','20.1.4.35']])
        self.assertEqual(import_targets(p)[0].address, 'https://20.1.4.35')
    def test_precheck_never_writes(self):
        results, _ = self.run_('precheck')
        self.assertTrue(all(r['status']=='预检查通过' for r in results)); self.assertEqual(RecordingClient.writes, [])
    def test_four_components_serial_and_reports_no_password(self):
        results, folder = self.run_()
        self.assertTrue(all(r['status']=='写入完成，待生效核验' for r in results))
        for target in self.targets:
            writes = [w for w in RecordingClient.writes if w[0]==target.address]
            self.assertEqual(len(writes), 12)
            chosen = [w[3]['data'] for w in writes if w[2].endswith('ExtendedVersion')]
            self.assertEqual(chosen, list(COMPONENTS))
        combined = ''.join(p.read_text() for p in folder.glob('*.json*'))
        self.assertNotIn(self.options.password, combined)
        self.assertNotIn(confirmation_key(self.options.password), combined)
        book = InputBook(str(folder/'维护结果.xlsx'))
        self.assertEqual(len(book.read_sheet(next(iter(book.sheets)))),9)
    def test_every_nonempty_group_subset_only_writes_selected(self):
        for mask in range(1, 16):
            with self.subTest(mask=mask):
                DemoClient.states.clear(); RecordingClient.writes=[]
                images=[image for i,image in enumerate(self.images) if mask & (1<<i)]
                results,_=self.run_(images=images)
                selected=[i.component for i in images]
                for target,result in zip(self.targets,results):
                    self.assertEqual(result['status'],'写入完成，待生效核验')
                    self.assertEqual([i['component'] for i in result['components']],selected)
                    writes=[w for w in RecordingClient.writes if w[0]==target.address]
                    self.assertEqual(len(writes),3*len(selected))
                    self.assertEqual([w[3]['data'] for w in writes if w[2].endswith('ExtendedVersion')],selected)
                    self.assertEqual(DemoClient.states[target.address]['versions'],{c:'0.5' if c in selected else '0.3' for c in COMPONENTS})
    def test_empty_group_list_refuses_upgrade_before_connect(self):
        with self.assertRaises(ValueError): self.run_(images=[])
        self.assertEqual(RecordingClient.writes,[])
        self.assertEqual(DemoClient.states,{})
    def test_subset_config_does_not_expand_to_four(self):
        p=self.folder/'subset.json'
        selected=(self.images[1],self.images[3])
        save_config(p,self.targets,selected,self.options.model)
        _,images,_=load_config(p)
        self.assertEqual(tuple(i.component for i in images),(COMPONENTS[1],COMPONENTS[3]))
    def test_verify_and_same_version_skip(self):
        self.run_()
        results, _ = self.run_('verify')
        self.assertTrue(all('读回一致' in r['status'] for r in results))
        RecordingClient.writes=[]
        results, _ = self.run_()
        self.assertEqual(RecordingClient.writes, [])
        self.assertTrue(all('未刷写' in r['status'] for r in results))
    def test_mismatched_readback_not_success(self):
        results, _ = self.run_('verify')
        self.assertTrue(all(r['status']=='停止并待核验' for r in results))
    def test_no_model_required_for_precheck_upgrade_and_readback(self):
        options=replace(self.options,model='')
        RecordingClient.behavior='bad-model'  # Reports OTHER; no expected model is imposed.
        checked,_=self.run_('precheck',options=options)
        self.assertTrue(all(r['status']=='预检查通过' for r in checked))
        upgraded,_=self.run_(options=options)
        self.assertTrue(all(r['status']=='写入完成，待生效核验' for r in upgraded))
        verified,_=self.run_('verify',options=options)
        self.assertTrue(all('读回一致' in r['status'] for r in verified))
    def test_no_model_required_for_independent_power(self):
        options=replace(self.options,model='')
        RecordingClient.behavior='bad-model'
        for action in ('On','GracefulShutdown'):
            results,_=self.run_(action,options=options)
            self.assertTrue(all(not r.get('error') for r in results),results)
    def test_omitted_model_does_not_remove_power_or_vendor_checks(self):
        options=replace(self.options,model='')
        for behavior in ('power-on','bad-vendor','busy','acoff'):
            RecordingClient.behavior=behavior;RecordingClient.writes=[]
            result,_=self.run_(options=options)
            self.assertEqual(RecordingClient.writes,[],behavior)
            self.assertTrue(all(r.get('error') for r in result),behavior)
    def test_preflight_blocks_incompatible_devices(self):
        for behavior in ('power-on','bad-model','bad-vendor','busy','acoff'):
            RecordingClient.behavior=behavior
            results, _ = self.run_()
            self.assertEqual(RecordingClient.writes, [], behavior)
            self.assertTrue(all(r['status']=='停止并待核验' for r in results), behavior)
    def test_power_on_after_upload_blocks_target_and_activation(self):
        def power_on_after_upload(path):
            if path.startswith('/upload/image/'):
                DemoClient.states[self.targets[0].address]['power']='On'
        RecordingClient.after_write=power_on_after_upload
        results,_=self.run_(targets=self.targets[:1])
        self.assertEqual(results[0]['status'],'停止并待核验')
        self.assertIn('Off',results[0]['error'])
        self.assertEqual(len(RecordingClient.writes),1)
        self.assertEqual(results[0]['components'],[])
    def test_power_on_after_target_blocks_activation(self):
        def power_on_after_target(path):
            if path.endswith('ExtendedVersion'):
                DemoClient.states[self.targets[0].address]['power']='On'
        RecordingClient.after_write=power_on_after_target
        results,_=self.run_(targets=self.targets[:1])
        self.assertEqual(results[0]['status'],'停止并待核验')
        self.assertEqual(len(RecordingClient.writes),2)
        self.assertFalse(any(w[2].endswith('RequestedActivation') for w in RecordingClient.writes))
    def test_write_failure_stops_remaining_components(self):
        RecordingClient.behavior='write-error'
        results, _ = self.run_()
        self.assertTrue(all(len(r['components'])==0 for r in results))
        self.assertEqual(len(RecordingClient.writes), 6)
    def test_stale_100_not_success(self):
        RecordingClient.behavior='stale'
        results, _ = self.run_(targets=self.targets[:1])
        self.assertEqual(results[0]['status'], '停止并待核验')
        self.assertIn('残留', results[0]['error'])
        self.assertEqual(len(RecordingClient.writes),3)
    def test_target_race_prevents_activation(self):
        RecordingClient.behavior='target-race'
        results, _ = self.run_(targets=self.targets[:1])
        self.assertEqual(results[0]['status'],'停止并待核验')
        self.assertFalse(any(w[2].endswith('RequestedActivation') for w in RecordingClient.writes))
    def test_stop_before_and_during_flash(self):
        stop=threading.Event(); stop.set()
        self.run_(stop=stop)
        self.assertEqual(RecordingClient.writes,[])
        stop.clear()
        RecordingClient.after_write=lambda path: stop.set() if path.endswith('RequestedActivation') else None
        results,_=self.run_(targets=self.targets[:1],stop=stop)
        self.assertEqual(len(results[0]['components']),1)
        self.assertEqual(len(RecordingClient.writes),3)
        self.assertIn('停止后续',results[0]['status'])
    def test_image_changed_rejected_before_login(self):
        self.path.write_bytes(b'changed')
        with self.assertRaises(BmcError): self.run_()
        self.assertEqual(RecordingClient.writes,[])
    def test_batch_requires_canary(self):
        options=Options('u','p',model='CY62-T58')
        with self.assertRaises(ValueError): self.run_(options=options)
    def test_without_expected_version_still_requires_off_and_new_operation(self):
        images=[Image.prepare(COMPONENTS[0],self.path,'')]
        results,_=self.run_(images=images)
        self.assertTrue(all(r['status']=='写入完成，待生效核验' for r in results))
        results,_=self.run_('verify',images=images)
        self.assertTrue(all('未提供预期版本' in r['status'] for r in results))
        DemoClient.states.clear();RecordingClient.writes=[];RecordingClient.behavior='stale'
        results,_=self.run_(images=images,targets=self.targets[:1])
        self.assertIn('残留',results[0]['error'])
        DemoClient.states.clear();RecordingClient.writes=[];RecordingClient.behavior='power-on'
        results,_=self.run_(images=images)
        self.assertEqual(RecordingClient.writes,[])
        self.assertTrue(all('Off' in r['error'] for r in results))
    def test_power_without_images_never_reads_cpld_resources(self):
        with patch.object(RecordingClient,'cpld',side_effect=AssertionError('CPLD must not be read')):
            results,_=self.run_('power-read',images=[])
            self.assertTrue(all('电源信息已读取' in r['status'] for r in results))
            results,_=self.run_('On',images=[])
            self.assertTrue(all(r['status']=='已确认 On' for r in results))
            results,_=self.run_('GracefulShutdown',images=[])
            self.assertTrue(all(r['status']=='已确认 Off' for r in results))
    def test_demo_image_blocked_in_real_engine_and_transport(self):
        with self.assertRaises(ValueError):
            run_batch(self.targets,self.images,self.options,'upgrade',folder=self.folder)
        client=UpgradeClient('192.0.2.1',self.options)
        with self.assertRaises(BmcError): client.write('POST','/upload/image/DEMO_LT.svf',DEMO_MARKER,binary=True)
    def test_write_whitelist_and_read_only_unchanged(self):
        client=UpgradeClient('192.0.2.1',self.options)
        for method,path,payload in [('PUT','/xyz/delete',{}),('PUT',CPLD+'/attr/ExtendedVersion',{'data':'BMC'}),('POST','/redfish/v1/Systems/1/Actions/ComputerSystem.Reset',{'ResetType':'ForceRestart'})]:
            with self.assertRaises(BmcError): client.write(method,path,payload)
        client=BmcClient('192.0.2.1')
        with self.assertRaises(BmcError): client.request('PUT',CPLD+'/attr/ExtendedVersion',{'data':COMPONENTS[0]})
    def test_no_automatic_power_operations(self):
        self.run_()
        self.assertFalse(any('/Actions/' in w[2] for w in RecordingClient.writes))
    def test_separate_power_controls(self):
        results,_=self.run_('On')
        self.assertTrue(all(r['status']=='已确认 On' for r in results))
        results,_=self.run_('GracefulShutdown')
        self.assertTrue(all(r['status']=='已确认 Off' for r in results))
    def test_lock_refuses_concurrent_process(self):
        with NodeLock(self.targets[0].address):
            with self.assertRaises(BmcError):
                with NodeLock(self.targets[0].address): pass
    def test_live_writes_default_denied_before_connect(self):
        for action in ('On','GracefulShutdown','ForceOff'):
            with patch.object(UpgradeClient,'login',side_effect=AssertionError('must not connect')):
                with self.assertRaises(ValueError):
                    run_batch(self.targets,[],self.options,action,folder=self.folder)
        p=self.folder/'REAL_LT.svf';p.write_bytes(b'inert fixture')
        images=[Image.prepare(COMPONENTS[0],p,'0.5')]
        with patch.object(UpgradeClient,'login',side_effect=AssertionError('must not connect')):
            with self.assertRaises(ValueError):
                run_batch(self.targets,images,self.options,'upgrade',folder=self.folder)
    def test_approval_exact_scope_one_use_and_expiry(self):
        approval=WriteApproval(self.targets,self.images,self.options.model,'upgrade')
        with self.assertRaises(ValueError): approval.claim(self.targets[:1],self.images,self.options.model,'upgrade')
        with self.assertRaises(ValueError): approval.claim(self.targets,self.images,self.options.model,'On')
        with self.assertRaises(ValueError): approval.claim(self.targets,self.images,'other','upgrade')
        approval.claim(self.targets,self.images,self.options.model,'upgrade')
        with self.assertRaises(ValueError): approval.claim(self.targets,self.images,self.options.model,'upgrade')
        approval=WriteApproval(self.targets,self.images,self.options.model,'upgrade');approval.created-=601
        with self.assertRaises(ValueError): approval.claim(self.targets,self.images,self.options.model,'upgrade')
    def test_approval_cannot_expand_wire_operations(self):
        approval=WriteApproval(self.targets,self.images,self.options.model,'upgrade')
        self.assertFalse(approval.allows(self.targets[0].address,'PUT',CPLD+'/attr/ExtendedVersion',{'data':COMPONENTS[0]},False))
        approval.claim(self.targets,self.images,self.options.model,'upgrade')
        self.assertTrue(approval.allows(self.targets[0].address,'PUT',CPLD+'/attr/ExtendedVersion',{'data':COMPONENTS[0]},False))
        self.assertFalse(approval.allows('https://192.0.2.199','PUT',CPLD+'/attr/ExtendedVersion',{'data':COMPONENTS[0]},False))
        self.assertFalse(approval.allows(self.targets[0].address,'POST','/redfish/reset',{'ResetType':'On'},False))
        self.assertFalse(approval.allows(self.targets[0].address,'POST','/upload/image/DEMO_LT_TEST.svf',b'changed',True))
    def test_approval_rejects_unchecked_component(self):
        images=(self.images[2],)
        approval=WriteApproval(self.targets,images,self.options.model,'upgrade')
        approval.claim(self.targets,images,self.options.model,'upgrade')
        for component in COMPONENTS:
            self.assertEqual(approval.allows(self.targets[0].address,'PUT',CPLD+'/attr/ExtendedVersion',{'data':component},False),component==COMPONENTS[2])
    def test_config_roundtrip_no_credentials(self):
        p=self.folder/'plan.json'
        save_config(p,self.targets,self.images,self.options.model)
        targets,images,model=load_config(p)
        self.assertEqual(targets,tuple(self.targets)); self.assertEqual(images,tuple(self.images)); self.assertEqual(model,self.options.model)
        self.assertNotIn(self.options.password,p.read_text()); self.assertNotIn('canary',p.read_text())
    def test_config_changed_image_and_existing_output_rejected(self):
        p=self.folder/'plan.json';save_config(p,self.targets,self.images,self.options.model)
        with self.assertRaises(FileExistsError): save_config(p,self.targets,self.images,self.options.model)
        self.path.write_bytes(b'changed')
        with self.assertRaises(ValueError): load_config(p)
    def test_config_duplicate_target_rejected(self):
        p=self.folder/'plan.json';save_config(p,self.targets,self.images,self.options.model)
        data=json.loads(p.read_text());data['targets'].append(data['targets'][0]);p.write_text(json.dumps(data))
        with self.assertRaises(ValueError): load_config(p)
    def test_non_ascii_filename_and_placeholder_rejected(self):
        p=self.folder/'LT中文.svf';p.write_bytes(b'abc')
        with self.assertRaises(ValueError): Image.prepare(COMPONENTS[0],p,'0.5')
        with self.assertRaises(BmcError): Image.prepare(COMPONENTS[0],self.path,'V0xffffffff')

if __name__=='__main__':
    unittest.main(verbosity=2)
