"""Contract and extension checks; no Docker or historical files required."""
import json
from pathlib import Path
import tempfile
import unittest
from .core import AlgorithmAdapter,EnvironmentSpec,ExperimentSpec,identity
from .scenarios import selection,get
from .algorithms import ProcessAdapter
from .artifacts import seal,verify,export_bundle,import_bundle


class FrameworkChecks(unittest.TestCase):
    def test_all_tiers_compile_deterministically(self):
        for tier,pressure in [('basic',False),('advanced',True)]:
            for name in selection(tier,pressure=pressure):
                a=get(name).generate(11);b=get(name).generate(11)
                self.assertEqual(a,b);self.assertEqual(a.manifest()['sha256'],b.manifest()['sha256'])

    def test_full_advanced_validation(self):
        import background_jobs as jobs
        for scene in selection('advanced',pressure=True):
            result=jobs.validate(dict(EnvironmentSpec().settings(scene,123,42),algorithm_profile='image_v1'))
            self.assertEqual(result['execution_mode'],'lockstep')
            self.assertEqual(result['algorithm_lifecycle'],'fresh')

    def test_relative_clock_rule_preserves_actual_changes(self):
        from .diagnostics import relative_events,first_difference
        from report import write
        with tempfile.TemporaryDirectory() as tmp:
            a,b=Path(tmp)/'a',Path(tmp)/'b'
            epoch=1_700_000_000_000_000_000
            for folder,offset in ((a,0),(b,100)):
                (folder/'session-evidence').mkdir(parents=True)
                write(folder/'run-state.json',dict(measurement_end_stamp_ns=epoch+offset+1000))
                rows=[dict(kind='input',jpeg_sha256='same',data=dict(stamp_ns=epoch+offset,step_id=1+offset,session='s',yaw=0.)),
                      dict(kind='result',data=dict(result=dict(stamp_ns=epoch+offset+20,yaw=.1,fire_advice=True)))]
                (folder/'session-evidence/events.jsonl').write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            self.assertIsNotNone(first_difference(a,b))
            self.assertEqual(relative_events(a),relative_events(b))
            path=b/'session-evidence/events.jsonl'
            path.write_text(path.read_text().replace('0.1','0.2'))
            self.assertNotEqual(relative_events(a),relative_events(b))

    def test_protocol_rejects_wrong_frame_and_nonfinite(self):
        from .protocol import validate_result
        frame=dict(session='s',seq=1,stamp_ns=3)
        good=dict(frame,valid=True,fire_advice=True,yaw=0.,pitch=0.,distance=5.)
        self.assertEqual(validate_result(frame,good),good)
        for changes in ({'seq':2},{'yaw':float('nan')},{'valid':False}):
            with self.assertRaises(ValueError):validate_result(frame,dict(good,**changes))

    def test_unknown_and_seeds(self):
        with self.assertRaises(ValueError):get('unknown')
        with self.assertRaises(ValueError):ExperimentSpec('B1','E1',('hf_static_5',),(1,1)).validate()
        with self.assertRaises(ValueError):ExperimentSpec('B1','E1',('hf_static_5',),(-1,)).validate()

    def test_custom_scenario(self):
        from .core import Scenario,ScenarioPlan
        from .scenarios import register
        class Custom(Scenario):
            def generate(self,seed):return ScenarioPlan('check_custom',1,'duration = 1',dict(seed=seed))
        register('check_custom',Custom())
        self.assertEqual(get('check_custom').generate(5).metadata['seed'],5)
        with self.assertRaises(ValueError):register('check_custom',Custom())

    def test_independent_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'adapter.json'
            path.write_text(json.dumps(dict(id='independent',protocol='rm26-image-v1',launch=['/snapshot/program'],capabilities=['simulation_clock','session_reset','fire_advice'])))
            adapter=ProcessAdapter(path)
            self.assertIsInstance(adapter,AlgorithmAdapter)
            self.assertNotIn('scene',adapter.data)

    def test_comparison_rejects_environment_change(self):
        from .evaluation import compare
        from report import write
        with tempfile.TemporaryDirectory() as tmp:
            a,b=Path(tmp)/'a',Path(tmp)/'b';a.mkdir();b.mkdir()
            write(a/'contract.json',dict(environment={'id':'E1'}))
            write(b/'contract.json',dict(environment={'id':'legacy'}))
            with self.assertRaises(ValueError):compare(a,b)

    def test_portable_payload_and_tampering(self):
        from .core import sha
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);bundle=root/'original';bundle.mkdir();(bundle/'program').write_bytes(b'artifact')
            seal(bundle,dict(files={'program':sha(bundle/'program')},images={}))
            metadata=verify(bundle)
            changed=dict(metadata,images={'algorithm':'wrong'})
            (bundle/'bundle.json').chmod(0o600)
            (bundle/'bundle.json').write_text(json.dumps(changed))
            with self.assertRaises(ValueError):verify(bundle)
            (bundle/'bundle.json').write_text(json.dumps(metadata))
            archive=root/'portable.tgz';export_bundle(bundle,archive)
            restored=import_bundle(archive,root/'restored')
            self.assertEqual(verify(bundle),verify(restored))
            (restored/'program').chmod(0o600);(restored/'program').write_bytes(b'changed')
            with self.assertRaises(ValueError):verify(restored)

if __name__=='__main__':unittest.main()
