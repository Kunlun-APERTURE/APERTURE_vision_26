import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import render_backend
from benchmark.core import ExperimentSpec


class RendererTests(unittest.TestCase):
    @patch.object(render_backend, 'nvidia_devices')
    def test_cpu_never_probes_nvidia(self, devices):
        self.assertEqual(render_backend.select('cpu')['backend'], 'cpu')
        devices.assert_not_called()

    @patch.object(render_backend.subprocess, 'run', side_effect=FileNotFoundError)
    def test_auto_falls_back_when_nvidia_smi_is_missing(self, command):
        self.assertEqual(render_backend.select()['backend'], 'cpu')
        with self.assertRaises(ValueError):
            render_backend.select('nvidia')

    @patch.object(render_backend, 'nvidia_devices', return_value=['GPU-test'])
    def test_auto_selects_available_hardware(self, devices):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(render_backend.select()['gpu_uuid'], 'GPU-test')
        with patch.dict('os.environ', {'RM26_BENCHMARK_GPU': 'GPU-other'}):
            with self.assertRaises(ValueError):
                render_backend.select('nvidia')
        with self.assertRaises(ValueError):
            render_backend.select('invalid')

    def test_legacy_payload_keeps_hardware_renderer(self):
        self.assertEqual(render_backend.frozen({'gpu_uuid': 'GPU-old'})['backend'], 'nvidia')
        with self.assertRaises(ValueError):
            ExperimentSpec('B1','E1',('hf_static_5',),(1,),renderer='invalid').validate()

    def test_cpu_budget_and_score_baseline_are_separate(self):
        self.assertEqual(render_backend.wall_budget({'backend':'cpu'},True),900)
        self.assertEqual(render_backend.wall_budget({'backend':'nvidia'},True),180)
        self.assertEqual(render_backend.wall_budget({'backend':'cpu'},False),40)
        from shots import baseline_key
        manifest=dict(scene='hf_static_5',mode='track')
        self.assertNotEqual(baseline_key(manifest),baseline_key(dict(manifest,renderer={'backend':'cpu'})))

    def test_cpu_compose_has_no_device_reservation_and_uses_lavapipe(self):
        from background_runtime import Worker
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            worker=Worker.__new__(Worker)
            worker.settings=dict(projectile_visible='on',fire='advice',fire_policy='advice',
                                 algorithm_profile='tracking_fire_v3',fire_rate_hz=5,
                                 reported_bullet_speed=25,bullet_speed=25,physics='no-drag',aim_origin='muzzle')
            worker.renderer=render_backend.select('cpu')
            worker.gpu_uuid=None;worker.slot=1;worker.domain=71;worker.port=18827
            worker.OPS=root;worker.source_ops=root;worker.RUNTIME=root;worker.bundle=root
            worker.IMAGE='unified';worker.SIMULATOR='sim';worker.BRIDGE='bridge';worker.DESKTOP_CONTAINER='algorithm'
            worker.metadata=dict(images=dict(simulator='unified',algorithm='unified',evaluator='unified'))
            worker.lockstep=False
            with patch.object(Worker,'run',side_effect=AssertionError('must not query NVIDIA')):
                services=worker.definition()['services']
            self.assertNotIn('deploy',services['daedalus'])
            self.assertEqual(services['daedalus']['environment']['VK_DRIVER_FILES'],render_backend.SOFTWARE_ICD)
            self.assertEqual(services['daedalus']['environment']['NVIDIA_VISIBLE_DEVICES'],'void')
            for service in services.values():
                self.assertEqual(service['runtime'],'runc')
            worker.renderer=dict(backend='nvidia',gpu_uuid='GPU-test')
            worker.gpu_uuid='GPU-test'
            with patch.object(render_backend,'nvidia_devices',return_value=['GPU-test']):
                hardware=worker.definition()['services']['daedalus']
            self.assertEqual(hardware['deploy']['resources']['reservations']['devices'][0]['device_ids'],['GPU-test'])
            self.assertNotIn('VK_DRIVER_FILES',hardware['environment'])
            with patch.object(render_backend,'nvidia_devices',return_value=[]):
                with self.assertRaisesRegex(ValueError,'Frozen GPU unavailable'):
                    worker.definition()

    def test_cpu_resource_assessment_does_not_inherit_vram_guard(self):
        from background_capacity import assess
        from report import write
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            write(root/'summary.json',dict(metrics={},rules=[]))
            write(root/'manifest.json',dict(execution={'mode':'lockstep'}))
            sample=dict(renderer='cpu',memory_available_kib=4*1024**2,disk_free_bytes=10*1024**3)
            (root/'resources.jsonl').write_text(json.dumps(sample)+'\n')
            with patch('background_capacity.control_hz',return_value=500):
                self.assertTrue(assess(root)['healthy'])
                sample['renderer']='nvidia'
                (root/'resources.jsonl').write_text(json.dumps(sample)+'\n')
                self.assertIn('vram_guard',assess(root)['issues'])

    def test_final_sealing_uses_the_same_cpu_budget_as_capture(self):
        import background_runtime as runtime
        from types import SimpleNamespace
        from unittest.mock import Mock
        from report import write,load
        for backend,expected in (('cpu','passed'),('nvidia','invalid')):
            with self.subTest(backend=backend),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);folder=root/'run';folder.mkdir()
                write(folder/'summary.json',dict(verdict='passed',rules=[dict(name='wall_time_budget',passed=True)]))
                write(folder/'timing.json',dict(budget_seconds=render_backend.wall_budget({'backend':backend},True)))
                request=dict(settings=dict(scene='hf_static_5'),result=str(root/'result.json'))
                write(root/'request.json',request)
                worker=SimpleNamespace(metadata={'files':{}},bundle=root,settings={},lockstep=True,
                    renderer={'backend':backend},start=Mock(),resources_stop=Mock(),rpc=Mock(),
                    compose_path=root/'absent-compose',current_folder=folder,DESKTOP=root/'absent-desktop',
                    DESKTOP_CONTAINER='algorithm',SIMULATOR='simulator',BRIDGE='bridge',logs_since=0,
                    PIPELINE_VERSION=2,basic_started=0,RUNTIME=root)
                with patch.object(runtime,'Worker',return_value=worker),patch('sys.argv',['worker',str(root/'request.json')]),patch.object(runtime.signal,'signal'),patch.object(runtime.experiments,'run_one',return_value=folder),patch.object(runtime.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')),patch.object(runtime.time,'monotonic',return_value=500),patch('report.render'):
                    runtime.main()
                self.assertEqual(load(root/'result.json')['verdict'],expected)
                self.assertEqual(load(folder/'timing.json')['within_budget'],backend=='cpu')

    def test_legacy_resume_keeps_original_contract_hash(self):
        from benchmark.runner import Runner
        from benchmark.core import identity
        from test_execution import payload
        from report import write,load
        import background_jobs as jobs
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);bundle=payload(root/'payload');out=root/'output'
            spec=ExperimentSpec('B1','E1',('hf_static_5',),(1,))
            with patch.object(jobs,'STATE',root/'queue'),patch.object(jobs,'DB',root/'queue/db'):
                state=Runner().submit(spec,out,None,bundle_path=bundle)
                contract=load(out/'contract.json');contract['spec'].pop('renderer')
                write(out/'contract.json',contract)
                state['contract_sha256']=identity(contract);write(out/'state.json',state)
                restored=Runner().submit(spec,out,None,bundle_path=bundle,resume=True)
                self.assertEqual(restored['contract_sha256'],identity(contract))
                self.assertNotIn('renderer',load(out/'contract.json')['spec'])

    @patch('background_capacity.subprocess.run',side_effect=FileNotFoundError)
    def test_cpu_scheduler_health_does_not_require_vram(self, command):
        from background_capacity import resource_health
        with tempfile.TemporaryDirectory() as temporary:
            health=resource_health(temporary)
        self.assertIn('healthy',health)
        self.assertFalse(health['nvidia_healthy'])
        self.assertEqual(health['gpu_free_bytes'],0)

    def test_cross_renderer_algorithm_comparison_is_rejected(self):
        from benchmark.evaluation import compare
        from test_execution import payload
        from report import write,load
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            a=payload(root/'a');b=payload(root/'b')
            from benchmark.artifacts import seal
            meta=load(b/'bundle.json');meta['renderer']=render_backend.select('cpu');seal(b,meta)
            for label,bundle in (('left',a),('right',b)):
                out=root/label;out.mkdir()
                write(out/'contract.json',dict(environment={},plans={},scoring_version=2,impact_quality_version=1,
                    spec=dict(scenes=['hf_static_5'],sensor_seeds=[1],trajectory_seed=1,repeats=1),bundle=str(bundle)))
            with self.assertRaisesRegex(ValueError,'Renderer differs'):
                compare(root/'left',root/'right')
