"""Offline regressions: no network, credentials, or device writes."""
from pathlib import Path
import copy
import sys
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from bmc_client import BmcError
from redfish_ops import catalog,contract
from redfish_workflow import _member,prepare,read_resource,resolve,run_node
from firmware_batch import Target


class ResourceClient:
    def __init__(self,resources):
        self.resources=resources
        self.calls=[]

    def get(self,path):
        self.calls.append(path)
        return copy.deepcopy(self.resources[path]),{},200

    def path(self,path):
        return path


class ResourceTests(unittest.TestCase):
    collection='/redfish/v1/Systems'

    def client(self,count=1):
        return ResourceClient({self.collection:{
            'Members':[{'@odata.id':self.collection+'/one'}],
            'Members@odata.count':count}})

    def test_integer_and_ascii_string_counts(self):
        for count in (1,'1','01'):
            with self.subTest(count=count):
                result=read_resource(self.client(count),self.collection)
                self.assertEqual(len(result['Members']),1)

    def test_missing_count_is_supported(self):
        c=self.client()
        del c.resources[self.collection]['Members@odata.count']
        self.assertEqual(_member(c,self.collection,None),self.collection+'/one')

    def test_invalid_or_mismatched_count_still_fails(self):
        for count in (True,1.0,'1.0','+1',' 1','１','',-1,'2','9'*100):
            with self.subTest(count=count),self.assertRaises(BmcError):
                read_resource(self.client(count),self.collection)

    def paged(self):
        return ResourceClient({
            self.collection:{'Members':[{'@odata.id':self.collection+'/one'}],
                             'Members@odata.count':'2',
                             'Members@odata.nextLink':self.collection+'?page=2'},
            self.collection+'?page=2':{'Members':[{'@odata.id':self.collection+'/two'}]}})

    def test_explicit_selection_on_later_page(self):
        self.assertEqual(_member(self.paged(),self.collection,'two'),self.collection+'/two')

    def test_multiple_members_need_explicit_selection(self):
        with self.assertRaises(BmcError):
            _member(self.paged(),self.collection,None)

    def test_unknown_member_is_not_guessed(self):
        with self.assertRaises(BmcError):
            _member(self.paged(),self.collection,'other')

    def test_duplicate_and_cross_collection_members_rejected(self):
        for member in ('one','../Chassis/one'):
            c=self.paged()
            c.resources[self.collection+'?page=2']['Members']=[
                {'@odata.id':self.collection+'/'+member if member=='one' else '/redfish/v1/Chassis/one'}]
            with self.subTest(member=member),self.assertRaises(BmcError):
                _member(c,self.collection,'two')

    def test_catalog_exposes_optional_system_context_for_writes(self):
        for row in catalog():
            with self.subTest(operation=row['id']):
                if row['write']:
                    self.assertIn('system_id',row['parameters'])
                elif '{system_id}' not in row['path']:
                    self.assertNotIn('system_id',row['parameters'])

    def test_context_contract_and_operation_url(self):
        op,params,_=contract('3.4.3',{'chassis_id':'rack','system_id':'two'},
                             {'IndicatorLED':'Off'})
        c=ResourceClient({
            '/redfish/v1/':{},
            '/redfish/v1/Chassis':{'Members':[{'@odata.id':'/redfish/v1/Chassis/rack'}],
                                  'Members@odata.count':'1'}})
        path,system=resolve(c,op,params)
        self.assertEqual(path,'/redfish/v1/Chassis/rack')
        self.assertIsNone(system)
        self.assertEqual(params['system_id'],'two')

    def test_read_only_and_unknown_context_still_rejected(self):
        for operation,params in (
            ('3.4.6',{'system_id':'two'}),
            ('3.4.3',{'system_id':'../two'}),
            ('3.4.3',{'arbitrary':'two'})):
            with self.subTest(operation=operation,params=params),self.assertRaises(BmcError):
                contract(operation,params,{'IndicatorLED':'Off'} if operation=='3.4.3' else {})

    def test_context_is_bound_to_confirmation_scope(self):
        targets=[Target('offline','https://127.0.0.1:1')]
        request={'operation':'3.4.3','params':{'chassis_id':'rack','system_id':'one'},
                 'payload':{'IndicatorLED':'Off'},'expected_model':'R6250A0'}
        *_,scope=prepare(targets,request)
        request['params']['system_id']='two'
        *_,changed_scope=prepare(targets,request)
        self.assertNotEqual(scope,changed_scope)

    def test_selected_system_used_without_weakening_write_checks(self):
        # In-memory transport only: _http below never sends a request.
        class Session(ResourceClient):
            session_timeout=600
            def login(self,*args):pass
            def safe(self,value):return copy.deepcopy(value)
            def close(self):return 'deleted'
            def get(self,path):
                body,_,status=super().get(path)
                return body,{'ETag':'"fixture"'},status
            def _http(self,method,path,payload,**kwargs):
                self.writes.append((method,path))
                self.resources[path].update(payload)
                return {},{},200
        for identifier,model,deadline,expected_writes in (
            ('two','R6250A0',time.time()+60,1),
            ('two','OTHER',time.time()+60,0),
            (None,'R6250A0',time.time()+60,0),
            ('missing','R6250A0',time.time()+60,0),
            ('two','R6250A0',time.time()-60,0)):
            with self.subTest(identifier=identifier,model=model,deadline=deadline):
                c=Session({
                    '/redfish/v1/':{},
                    '/redfish/v1/Chassis':{'Members':[{'@odata.id':'/redfish/v1/Chassis/rack'}]},
                    '/redfish/v1/Chassis/rack':{'IndicatorLED':'Off'},
                    self.collection:{'Members':[{'@odata.id':self.collection+'/one'},
                                                {'@odata.id':self.collection+'/two'}]},
                    self.collection+'/two':{'Model':model,'PowerState':'Off'}})
                c.writes=[]
                params={'chassis_id':'rack'}
                if identifier:params['system_id']=identifier
                request={'operation':'3.4.3','params':params,'payload':{'IndicatorLED':'Blinking'},
                         'expected_model':'R6250A0'}
                target=Target('offline','https://127.0.0.1:1')
                op,request,_,_=prepare([target],request)
                row=run_node(target,op,request,('fixture','fixture'),threading.Event(),
                             client_factory=lambda *args:c,write_deadline=deadline)
                self.assertEqual(len(c.writes),expected_writes,row)
                self.assertEqual(row['session_cleanup'],'deleted')
                self.assertEqual(row['state'],'completed' if expected_writes else 'needs_review')


if __name__=='__main__':
    unittest.main()
