"""Pure local tests for the button gate; no networking or device permissions."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import tempfile
import unittest
from dataclasses import replace
from firmware_readiness import ReadinessGate
from firmware_batch import COMPONENTS, Image, Options, Target


def snapshot(power='Off', model='CY62-T58', current='0.3'):
    return {'model':model,'power':power,'activation':'Active','vendors':dict.fromkeys(COMPONENTS,'Lattice'),
            'effective':[[c,'gpio','poweroff'] for c in COMPONENTS],
            'inventory':{c:{'version':current,'updateable':True,'state':'Enabled'} for c in COMPONENTS}}


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=Path(self.temp.name)/'FIXTURE_LT.svf';p.write_bytes(b'inert')
        self.images=(Image.prepare(COMPONENTS[0],p,'0.5'),)
        self.targets=(Target('node-a','192.0.2.21'),Target('node-b','192.0.2.22'))
        self.options=Options('fixture-user','fixture-password',model='CY62-T58',canary_confirmed=True)
        self.gate=ReadinessGate(ttl=120)
    def observe(self, power='Off', current='0.3'):
        for t in self.targets:self.gate.observe(t.address,snapshot(power=power,current=current),100)
    def commit(self,failed=None):
        self.gate.commit('plan',[{'address':t.address,'status':'停止并待核验' if t==failed else '预检查通过'} for t in self.targets])
    def evaluate(self,**kwargs):
        return self.gate.evaluate(kwargs.pop('key','plan'),kwargs.pop('targets',self.targets),kwargs.pop('images',self.images),
                                  kwargs.pop('options',self.options),now=kwargs.pop('now',110),**kwargs)
    def test_no_read_is_blocked(self):
        self.assertFalse(self.evaluate()[0])
    def test_off_read_alone_does_not_enable(self):
        self.observe();self.assertIn('尚未',self.evaluate()[1])
    def test_only_explicit_successful_precheck_enables(self):
        self.observe();self.commit();self.assertTrue(self.evaluate()[0])
    def test_on_unknown_transition_never_enable(self):
        for state in ('On','Unknown','PoweringOff','PoweringOn',''):
            self.observe(state);self.commit()
            self.assertFalse(self.evaluate()[0],state)
            self.assertIn('Off',self.evaluate()[1])
    def test_one_node_on_blocks_entire_selection(self):
        self.observe();self.gate.observe(self.targets[1].address,snapshot(power='On'),100);self.commit()
        self.assertFalse(self.evaluate()[0]);self.assertIn('node-b',self.evaluate()[1])
    def test_stale_state_blocks(self):
        self.observe();self.commit();self.assertIn('过期',self.evaluate(now=221)[1])
    def test_future_observation_blocks(self):
        self.observe();self.commit();self.assertFalse(self.evaluate(now=90)[0])
    def test_different_plan_and_selection_block(self):
        self.observe();self.commit()
        self.assertFalse(self.evaluate(key='changed')[0])
        self.assertFalse(self.evaluate(targets=(Target('other','192.0.2.99'),))[0])
    def test_failed_node_blocks_whole_batch(self):
        self.observe();self.commit(failed=self.targets[1]);self.assertFalse(self.evaluate()[0])
    def test_invalidation_cannot_be_undone_by_same_input(self):
        self.observe();self.commit();self.gate.invalidate();self.assertFalse(self.evaluate()[0])
    def test_new_observation_removes_old_pass(self):
        self.observe();self.commit();self.gate.observe(self.targets[0].address,snapshot(),105)
        self.assertFalse(self.evaluate()[0])
    def test_changed_connection_clears_observations(self):
        self.observe();self.commit();self.gate.invalidate(True)
        self.assertEqual(self.gate.observations,{});self.assertFalse(self.evaluate()[0])
    def test_no_targets_or_components_block(self):
        self.observe();self.commit()
        self.assertFalse(self.evaluate(targets=())[0]);self.assertFalse(self.evaluate(images=())[0])
    def test_no_required_model_when_component_precheck_passes(self):
        self.observe();self.commit()
        self.assertTrue(self.evaluate(options=replace(self.options,model=''))[0])
    def test_no_required_model_still_blocks_on(self):
        self.observe(power='On');self.commit()
        self.assertFalse(self.evaluate(options=replace(self.options,model=''))[0])
    def test_model_mismatch_blocks(self):
        self.observe();self.commit();self.assertFalse(self.evaluate(options=replace(self.options,model='OTHER'))[0])
    def test_all_target_versions_block_as_noop(self):
        self.observe(current='0.5');self.commit();self.assertIn('无需升级',self.evaluate()[1])
    def test_batch_canary_required_live_only(self):
        self.observe();self.commit();options=replace(self.options,canary_confirmed=False)
        self.assertFalse(self.evaluate(options=options)[0]);self.assertTrue(self.evaluate(options=options,demo=True)[0])
    def test_malformed_inventory_blocks(self):
        self.observe();self.gate.observations[self.targets[0].address][0]['inventory']={};self.commit()
        self.assertFalse(self.evaluate()[0])

if __name__=='__main__':unittest.main(verbosity=2)
