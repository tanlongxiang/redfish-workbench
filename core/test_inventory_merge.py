"""Offline regression tests for firmware preservation and sorted inventory."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import csv
import hashlib
import tempfile
import unittest
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from inventory_merge import merge_sorted,natural_key,usable_firmware
from node_verify import FIRMWARE,node_address
from workspace_paths import default_inputs,default_master
from xlsx_io import InputBook,write_xlsx


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.master=self.root/'master.xlsx';self.source=self.root/'source.xlsx';self.out=self.root/'sorted.xlsx'
        self.headers=['节点名','BMC_IP','SN']+[n for n,_ in FIRMWARE]
        names=['n10r1n01','a04r2n15','b16r1n02','n09r1n01','b16r1n03']
        rows=[[n,node_address(n),'LATEST-'+n]+['OLD']*38 for n in names]
        rows[-1]=rows[-1][:3]+['']*38
        write_xlsx(str(self.master),self.headers,rows)
        write_xlsx(str(self.source),['完整节点','SN'],[[n,'LATEST-'+n] for n in names])
    def contents(self,path):
        b=InputBook(str(path));return b.read_sheet(next(iter(b.sheets)))
    def test_natural_order(self):
        self.assertEqual(sorted(['n10r1n01','a04r2n15','b16r1n02','n09r1n01','b16r1n03'],key=natural_key),
                         ['a04r2n15','b16r1n02','b16r1n03','n09r1n01','n10r1n01'])
    def test_bad_values(self):
        for x in ['NA','N/A','','V0xffffffff','0xFFFFFFFF',True,None]:self.assertFalse(usable_firmware(x))
        for x in ['0','1','V0.8','0508000712LongRun-ES']:self.assertTrue(usable_firmware(x))
    def test_absent_component(self):
        self.assertFalse(usable_firmware('1.2',{'State':'Absent'}))
        self.assertTrue(usable_firmware('1.2',{'State':'Enabled','Health':'Critical'}))
    def test_latest_sn_preserved_and_unsupported_not_overwritten(self):
        digest=hashlib.sha256(self.master.read_bytes()).hexdigest()
        results={'a04r2n15':{'system_sn':'DIFFERENT-LIVE-SN','firmware':{'Bios':'NEW','retimer0':'V0xffffffff','Pciesw0':'NA'},'notes':[]},
                 'b16r1n03':{'firmware':{'BMC_Active':'2.00'},'notes':[]}}
        summary=merge_sorted(self.master,self.source,results,self.out)
        self.assertEqual(summary['total_nodes'],5)
        rows=[v for _,v in self.contents(self.out)[1:]];by_node={r[0]:r for r in rows}
        self.assertEqual([r[0] for r in rows],sorted(by_node,key=natural_key))
        a=by_node['a04r2n15'];self.assertEqual(a[2],'LATEST-a04r2n15');self.assertEqual(a[3],'NEW')
        self.assertEqual(a[self.headers.index('retimer0')],'OLD')
        self.assertEqual(a[self.headers.index('Pciesw0')],'OLD')
        self.assertEqual(by_node['b16r1n03'][self.headers.index('BMC_Active')],'2.00')
        self.assertEqual(by_node['b16r1n03'][self.headers.index('Bios')],'')
        self.assertEqual(digest,hashlib.sha256(self.master.read_bytes()).hexdigest())
        self.assertEqual(summary['counts']['filled_empty_fields'],1)
        self.assertEqual(summary['counts']['updated_existing_fields'],1)
    def test_source_sn_can_be_newer_than_merged_input(self):
        write_xlsx(str(self.source),['完整节点','SN'],[['a04r2n15','NEWEST']])
        summary=merge_sorted(self.master,self.source,{},self.out)
        row=next(v for _,v in self.contents(self.out)[1:] if v[0]=='a04r2n15')
        self.assertEqual(row[2],'NEWEST');self.assertEqual(summary['counts']['latest_sn_changes'],1)
    def test_coordinate_and_styles_follow_row(self):
        merge_sorted(self.master,self.source,{},self.out)
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with ZipFile(self.out) as z:
            root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            for rn,row in enumerate(root.findall('s:sheetData/s:row',ns),1):
                self.assertEqual(row.get('r'),str(rn))
                for c in row:self.assertTrue(c.get('r').endswith(str(rn)))
        with ZipFile(self.master) as old,ZipFile(self.out) as new:
            self.assertEqual([p for p in old.namelist() if old.read(p)!=new.read(p)],['xl/worksheets/sheet1.xml'])
    def test_no_overwrite(self):
        with self.assertRaises(ValueError):merge_sorted(self.master,self.source,{},self.master)
    def test_synthetic_master_dry_merge(self):
        before = hashlib.sha256(self.master.read_bytes()).hexdigest()
        summary=merge_sorted(self.master,self.source,{},self.out)
        self.assertEqual(summary['total_nodes'],5)
        rows=[v for _,v in self.contents(self.out)[1:]]
        self.assertEqual([r[0] for r in rows],sorted([r[0] for r in rows],key=natural_key))
        positions={r[0]:i for i,r in enumerate(rows)}
        self.assertLess(positions['b16r1n02'],positions['b16r1n03'])
        self.assertLess(positions['n09r1n01'],positions['n10r1n01'])
        self.assertEqual(before,hashlib.sha256(self.master.read_bytes()).hexdigest())

if __name__=='__main__':unittest.main(verbosity=2)
