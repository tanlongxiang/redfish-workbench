"""Offline asset ledger, read-only collection and independent power regression."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import copy
import tempfile
import unittest
from unittest.mock import patch
import test_api as support
from bmc_web.bridge import BmcClient,DemoClient,FIRMWARE,Target,InputBook
from bmc_web.asset_catalog import Catalog


class AssetTests(unittest.TestCase):
    setUp=support.APITests.setUp
    post=support.APITests.post
    demo=support.APITests.demo
    wait=support.APITests.wait
    run_=support.APITests.run_
    off=support.APITests.off
    checked=support.APITests.checked
    execute=support.APITests.execute
    def setUp(self):
        support.APITests.setUp(self)
        self.read_guard=patch.object(BmcClient,'request',side_effect=AssertionError('real BMC forbidden'))
        self.read_guard.start();self.addCleanup(self.read_guard.stop)
    def records(self,**filters):
        response=self.client.get('/api/asset-records',params={'mode':'demo',**filters})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def import_(self,text,mode='demo',status=200):
        r=self.client.post('/api/asset-records/import?mode='+mode,files={'file':('assets.csv',text.encode())},headers=self.headers)
        self.assertEqual(r.status_code,status,r.text);return r
    def test_optional_expected_upgrade_and_unasserted_readback(self):
        draft=self.demo(('Dcu02Cpld','Dcu46Cpld'))
        for image in draft['images']:image.pop('expected')
        self.checked(draft)
        job=self.execute('upgrade',draft);self.assertEqual(job['state'],'completed')
        verify=self.run_('verify',draft)
        self.assertEqual(verify['state'],'completed')
        self.assertTrue(all('未提供预期版本' in r['status'] for r in verify['results']))
        self.assertFalse(self.post('/api/readiness',draft)['ready'])
    def test_power_plan_ignores_even_unusable_cpld_selection(self):
        draft=self.demo();draft['images']=[{'component':'Dcu02Cpld','image_id':'not-present'}]
        with patch.object(DemoClient,'cpld',side_effect=AssertionError('No CPLD access for power')):
            read=self.run_('power-read',{**draft,'model':''});self.assertEqual(read['state'],'completed')
            for action in ('GracefulShutdown','On','ForceOff'):
                job=self.execute(action,draft);self.assertEqual(job['state'],'completed',job)
                self.assertEqual(job['draft']['images'],[])
        self.assertFalse(self.post('/api/readiness',draft)['ready'])
    def test_legacy_model_does_not_gate_power_upgrade_or_readback(self):
        draft=self.demo();draft['model']='OLD-WRONG-MODEL'
        system=DemoClient.system
        def different_model(client):
            value=system(client);value['Model']='OBSERVED-OTHER-MODEL';return value
        with patch.object(DemoClient,'system',different_model):
            self.checked(draft)
            plan=self.post('/api/plans',{'action':'upgrade','draft':draft})
            self.assertTrue(all(t['model']=='OBSERVED-OTHER-MODEL' for t in plan['targets']))
            result=self.execute('upgrade',draft);self.assertEqual(result['state'],'completed')
            readback=self.run_('verify',draft);self.assertEqual(readback['state'],'completed')
            for action in ('On','GracefulShutdown'):
                result=self.execute(action,draft);self.assertEqual(result['state'],'completed')
            workspace=self.post('/api/workspaces/export',draft)
            workspace['draft']['model']='OLD-WRONG-MODEL'
            restored=self.post('/api/workspaces/restore',workspace)['draft']
            self.assertNotIn('model',restored)
    def test_empty_ledger_no_implicit_business_import(self):
        value=self.records();self.assertEqual(value['total'],0)
    def test_offline_import_long_sn_and_metadata_no_device_access(self):
        self.app.state.settings.test_simulation=True
        text='节点名,BMC_IP,SN,机型,位置,标签,备注\na04r1n01,20.1.4.1,001234567890123456789,CY62-T58,A04,液冷,参考资料\n'
        self.import_(text,mode='live')
        r=self.client.get('/api/asset-records?mode=live').json()['items'][0]
        self.assertEqual(r['metadata']['declared_sn'],'001234567890123456789')
        self.assertEqual(r['collection_state'],'uncollected')
        self.assertEqual(r['data'],{})
    def test_imported_firmware_is_reference_not_fresh_live_evidence(self):
        self.import_('节点名,BMC_IP,SN,Bios,BMC_Active,Dcu02Cpld\na04r1n01,20.1.4.1,REF,REFERENCE-BIOS,REFERENCE-BMC,0.3\n')
        row=self.records()['items'][0]
        self.assertEqual(row['reference_count'],3)
        self.assertEqual(row['firmware_count'],0)
        self.assertEqual(row['collection_state'],'uncollected')
        self.assertNotIn('last_attempt',row['data'])
        self.assertEqual(self.records(version_source='reference',firmware_field='BMC_Active',firmware_version='REFERENCE-BMC')['filtered'],1)
        self.assertEqual(self.records(version_source='observed',firmware_field='BMC_Active',firmware_version='REFERENCE-BMC')['filtered'],0)
    def test_import_is_atomic_and_duplicate_addresses_refused(self):
        self.import_('节点名,BMC_IP\na,192.0.2.1\nb,192.0.2.1\n',status=400)
        self.assertEqual(self.records()['total'],0)
        self.import_('节点名,BMC_IP,SN\na,192.0.2.1,1.23E+20\n',status=400)
        self.assertEqual(self.records()['total'],0)
    def test_collect_38_fields_and_keep_reference_sn_separate(self):
        draft=self.demo();draft['images']=[];draft['model']=''
        self.import_('节点名,BMC_IP,SN,位置\nDEMO-01,192.0.2.11,MENTOR-REF,R01\n')
        result=self.run_('asset-collect',draft);self.assertEqual(result['state'],'completed',result)
        data=self.records();self.assertEqual(data['total'],3)
        items={r['name']:r for r in data['items']}
        first=items['DEMO-01'];self.assertEqual(first['metadata']['declared_sn'],'MENTOR-REF')
        self.assertEqual(first['sn_state'],'conflict');self.assertEqual(first['firmware_count'],38)
        self.assertEqual(first['data']['memory_gib'],512)
        self.assertEqual(items['DEMO-02']['latest_count'],37)
        self.assertEqual(items['DEMO-02']['collection_state'],'partial')
        self.assertEqual(items['DEMO-03']['sn_state'],'missing')
        self.assertEqual(self.records(sn='conflict')['filtered'],1)
        self.assertEqual(self.records(power='On')['filtered'],1)
        self.assertEqual(self.records(status='partial')['filtered'],1)
        self.assertFalse(self.post('/api/readiness',draft)['ready'])
    def test_failure_and_na_never_erase_previous_valid_versions(self):
        draft=self.demo();self.run_('asset-collect',draft)
        catalog=self.app.state.runtime.catalog;target=Target(**draft['targets'][0])
        old=catalog.get('demo',target.address)
        failure={'node':target.name,'address':target.address,'collection_state':'failed','observed':1234,'firmware':{},'error':'fixture failure'}
        catalog.collected('demo',target,failure)
        current=catalog.get('demo',target.address)
        self.assertEqual(current['data']['firmware'],old['data']['firmware'])
        self.assertEqual(current['data']['firmware_observed'],old['data']['firmware_observed'])
        self.assertEqual(current['collection_state'],'failed')
        self.assertEqual(current['latest_count'],0)
        self.assertEqual(self.records(status='failed')['filtered'],1)
    def test_edit_sparse_reimport_and_persistence(self):
        draft=self.demo();row=self.records()['items'][0]
        body={'mode':'demo','address':row['address'],**row['metadata'],'declared_sn':'REFERENCE','location':'RACK 42','lifecycle':'repair'}
        response=self.client.put('/api/asset-records/metadata',json=body,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.import_(f"节点名,BMC_IP\n{row['name']},{row['address']}\n")
        value=self.records(lifecycle='repair')['items'][0]
        self.assertEqual(value['metadata']['declared_sn'],'REFERENCE')
        self.assertEqual(value['metadata']['location'],'RACK 42')
        again=Catalog(self.app.state.runtime.store)
        self.assertEqual(again.get('demo',row['address'])['metadata']['declared_sn'],'REFERENCE')
    def test_filtered_export_text_sn_and_full_scope(self):
        draft=self.demo();self.run_('asset-collect',draft)
        self.import_('节点名,BMC_IP,SN\nDEMO-01,192.0.2.11,001234567890123456789\n')
        r=self.client.get('/api/asset-records/export?mode=demo&q=DEMO-01')
        self.assertEqual(r.status_code,200);self.assertIn('attachment',r.headers['content-disposition'])
        p=self.root/'export.xlsx';p.write_bytes(r.content)
        book=InputBook(str(p));rows=book.read_sheet(next(iter(book.sheets)))
        self.assertEqual(len(rows),2);self.assertIn('001234567890123456789',rows[1][1])
        self.assertNotIn('fixture-password',r.content.decode(errors='ignore'))
    def test_pagination_version_filters_and_query_validation(self):
        draft=self.demo();self.run_('asset-collect',draft)
        value=self.records(page=2,page_size=1)
        self.assertEqual(len(value['items']),1);self.assertEqual(value['filtered'],3)
        self.assertEqual(self.records(firmware_field='BMC_Active',firmware_version='DEMO-1.0')['filtered'],3)
        self.assertEqual(self.client.get('/api/asset-records?mode=demo&firmware_field=bad').status_code,400)
        self.assertEqual(self.client.get('/api/asset-records?mode=demo&page_size=10000').status_code,400)
    def test_ledger_routes_authenticated_and_mutations_need_csrf(self):
        self.assertEqual(self.client.put('/api/asset-records/metadata',json={},headers=self.origin).status_code,403)
        self.client.cookies.clear()
        for url in ('/api/asset-records','/api/asset-records/export'):
            self.assertEqual(self.client.get(url).status_code,401)
    def test_demo_only_blocks_full_live_inventory_before_connection(self):
        self.app.state.settings.test_simulation=True
        draft=self.demo();draft.update(mode='live',images=[],targets=[{'name':'fixture','address':'20.1.1.1'}])
        self.post('/api/jobs',{'action':'asset-collect','draft':draft},400)

if __name__=='__main__':unittest.main(verbosity=2)
