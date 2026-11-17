"""No-network DIMM pagination, missing-capacity and report regressions."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'_engine' if (ROOT/'_engine').exists() else ROOT.parent/'core'))
import json
import tempfile
import threading
import unittest
from bmc_client import BmcClient,ApiError
from memory_inventory import read_memory,capacity
from memory_report import export_memory_report
from bmc_cli import app
from bmc_cli.targets import load_targets
from xlsx_io import InputBook

BASE='/redfish/v1/Systems/unique/Memory'
class Fake:
    base='https://192.0.2.1'
    safe_path=BmcClient.safe_path
    def __init__(self,sizes,total=None):
        self.seen=[];self.data={BASE:{'Members':[{'@odata.id':BASE+'/'+str(i)} for i in range(len(sizes))],'Members@odata.count':len(sizes)}}
        self.system={'Memory':{'@odata.id':BASE},'MemorySummary':{'TotalSystemMemoryGiB':total}}
        for i,size in enumerate(sizes):self.data[BASE+'/'+str(i)]={'Id':str(i),'DeviceLocator':'DIMM'+str(i),'CapacityMiB':size,'Status':{'State':'Enabled','Health':'OK'}}
    def request(self,method,path):
        assert method=='GET';self.seen.append(path)
        value=self.data[path]
        if isinstance(value,Exception):raise value
        return value,{}
    def read(self):return read_memory(self,self.system,'/redfish/v1/Systems/unique',threading.Event())

class MemoryTests(unittest.TestCase):
    def test_all64_and_total(self):
        r=Fake([65536]*8,512).read();self.assertEqual(r['state'],'complete');s=r['summary']
        self.assertEqual(s['count_64gb'],8);self.assertEqual(s['conclusion'],'8×64GB');self.assertEqual(s['total_comparison'],'一致')
    def test_all32(self):
        r=Fake([32768]*16,512).read();self.assertEqual(r['summary']['count_32gb'],16)
    def test_mixed_capacities(self):
        r=Fake([32768,65536],96).read();self.assertIn('混插',r['summary']['conclusion'])
    def test_unknown_capacity_not_zero_and_not_all64(self):
        r=Fake([65536,None]).read();self.assertEqual(r['state'],'partial');self.assertEqual(r['summary']['unknown_capacity'],1)
        self.assertIn('不能判定',r['summary']['conclusion'])
    def test_zero_capacity_is_not_proof_of_empty_slot(self):
        r=Fake([0]).read();self.assertEqual(r['summary']['empty_slots'],0);self.assertEqual(r['summary']['unknown_capacity'],1)
    def test_explicit_absent_excluded_even_with_residual_capacity(self):
        f=Fake([65536,65536]);f.data[BASE+'/1']['Status']['State']='Absent'
        r=f.read();self.assertEqual(r['summary']['count_64gb'],1);self.assertEqual(r['summary']['empty_slots'],1)
    def test_collection_404_has_no_fake_zero_dimm_counts(self):
        f=Fake([]);f.data[BASE]=ApiError(404,BASE);r=f.read()
        self.assertEqual(r['state'],'unsupported');self.assertIsNone(r['summary']['count_64gb'])
    def test_valid_pagination(self):
        f=Fake([32768,65536]);f.data[BASE]['Members']=f.data[BASE]['Members'][:1]
        f.data[BASE]['Members@odata.nextLink']=BASE+'?$skip=1'
        f.data[BASE+'?$skip=1']={'Members':[{'@odata.id':BASE+'/1'}],'Members@odata.count':2}
        r=f.read();self.assertEqual(r['state'],'complete');self.assertEqual(len(r['dimms']),2)
    def test_pagination_failure_marks_unread_member(self):
        f=Fake([65536,65536]);f.data[BASE]['Members']=f.data[BASE]['Members'][:1]
        f.data[BASE]['Members@odata.nextLink']=BASE+'?$skip=1';f.data[BASE+'?$skip=1']=ApiError(503,BASE)
        r=f.read();self.assertEqual(r['summary']['unknown_capacity'],1);self.assertEqual(r['state'],'partial')
    def test_duplicate_links_are_not_counted_twice(self):
        f=Fake([65536]);f.data[BASE]['Members']*=2;r=f.read()
        self.assertEqual(r['summary']['count_64gb'],1);self.assertEqual(r['state'],'partial')
    def test_cross_host_and_cross_system_links_never_followed(self):
        for bad in ('https://192.0.2.2/redfish/v1/Systems/1/Memory/X','/redfish/v1/Systems/other/Memory/X'):
            f=Fake([65536]);f.data[BASE]['Members']=[{'@odata.id':bad}];r=f.read()
            self.assertEqual(f.seen,[BASE]);self.assertNotEqual(r['state'],'complete');self.assertEqual(r['summary']['unknown_capacity'],1)
    def test_failed_resource_not_counted_as_absent(self):
        f=Fake([65536,65536]);f.data[BASE+'/1']=ApiError(404,BASE+'/1');r=f.read()
        self.assertEqual(r['summary']['unknown_capacity'],1);self.assertEqual(r['summary']['empty_slots'],0)
    def test_bad_capacity_types(self):
        for value in (None,False,True,-1,0,2**32-1,'NA',float('nan'),12.3):self.assertIsNone(capacity(value))
        self.assertEqual(capacity('65536'),65536)
    def test_mismatch_total_flagged_without_changing_dimm_capacity(self):
        r=Fake([65536],512).read();self.assertEqual(r['summary']['dimm_total_gib'],64);self.assertEqual(r['state'],'partial')
    def test_report_preserves_failed_nodes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'memory.xlsx';memory=Fake([65536],64).read()
            rows=[{'node':'fixture','address':'https://192.0.2.1','status':'ok','memory':memory},
                  {'node':'unread','address':'https://192.0.2.2','status':'未执行','attempted':False}]
            export_memory_report(path,rows);book=InputBook(str(path))
            self.assertEqual(list(book.sheets),['节点汇总','内存条明细','统计说明'])
            summary=book.read_sheet('节点汇总');self.assertEqual(len(summary),3)
            self.assertEqual(summary[2][1][6],'未取得')
            self.assertEqual(len(book.read_sheet('内存条明细')),2)
            with self.assertRaises(FileExistsError):export_memory_report(path,rows)
    def test_bmcip_alias_does_not_use_management_or_ib_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'nodes.csv';path.write_text('节点名,管理IP,BMCIP,IB0IP\nfixture,192.0.2.99,192.0.2.11,192.0.2.88\n')
            args=app.parser().parse_args(['collect','--targets',str(path),'--memory'])
            self.assertTrue(args.memory);self.assertEqual(load_targets(args)[0].address,'https://192.0.2.11')

if __name__=='__main__':unittest.main(verbosity=2)
