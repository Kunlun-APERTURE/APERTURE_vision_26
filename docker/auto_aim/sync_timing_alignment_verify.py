"""Seal the bounded timing-only study and its independent negative findings."""
import argparse
import hashlib
import json
from pathlib import Path
import re

import background_jobs as jobs
import manage
import experiments
from background_capacity import signature
from report import load,write
from sync_first_divergence import sha
from sync_timing_alignment import first
from sync_timing_alignment_report import analyze,holdout,finish


def verify(root):
    state=load(root/'study.json');release=load(root/'release-home/study.json')
    independent=load(root/'holdout.json')
    assert state['state']==release['state']==independent['state']=='completed'
    result=analyze(root);effect=holdout(root)
    audits=[load(p) for p in (root/'evidence-audit.json',root/'release-home/evidence-audit.json',root/'holdout-evidence/evidence-audit.json')]
    assert [p['runs'] for p in audits]==[18,6,20] and all(p['passed'] for p in audits)
    base=Path(state['reference_bundle']);meta=load(base/'bundle.json')
    assert all(sha(base/name)==digest for name,digest in meta['files'].items())
    protected=('docker/auto_aim/bridge.py','docker/auto_aim/evaluator.py','docker/auto_aim/background_runtime.py',
        'docker/auto_aim/experiments.py','docker/auto_aim/input_contract.py','docker/auto_aim/shots.py',
        'sp_vision_25/build/simulator/sim_auto_aim','sp_vision_25/src/sim_auto_aim.cpp',
        'sp_vision_25/tasks/auto_aim/aimer.cpp','sp_vision_25/tasks/auto_aim/aimer.hpp',
        'vision_simulator_26/src/bench/lockstep.rs','vision_simulator_26/src/capture/effect.rs',
        'vision_simulator_26/src/capture/sensor.rs','vision_simulator_26/config.toml')
    assert all(sha(manage.ROOT/name)==meta['files'][name] for name in protected)
    assert sha(manage.ROOT/'sp_vision_25/tests/auto_aim_test.cpp')=='7c08603df343de8c503b8e44c3ec07d68f3a6fee0471d7a9666fe890bd3a1ee2'
    candidate=Path(state['candidate_bundle']);rm=load(Path(release['candidate_bundle'])/'bundle.json')
    for bundle in (candidate,Path(release['candidate_bundle'])):
        current=load(bundle/'bundle.json')
        assert current['images']==meta['images']
        assert all(sha(bundle/name)==digest for name,digest in current['files'].items())
    assert sha(manage.OPS/'sync_bridge.py')==rm['files']['docker/auto_aim/sync_bridge.py']
    assert sha(manage.OPS/'sync_timing.py')==rm['files']['docker/auto_aim/sync_timing.py']
    old_validation=load(manage.OPS/'experiments/input-contract-fix-20261023/validation.json')
    assert sha(manage.OPS/'input-contract-runtime.json')==old_validation['runtime_selector_sha256']
    for name,image in old_validation['default_images'].items():
        tag='daedalus:rm26-integration' if name=='simulator' else 'daedalus:rm26-evaluator'
        assert manage.inspect(tag)['Id']==image
    cross=load(root/'first-cross.json');assert cross['state']=='completed' and len(cross['results'])==24
    sealed=[]
    for entry in cross['results']:
        replay=root/'first-cross-replays'/entry['case'];plan=load(replay/'plan.json')
        native=manage.OPS/'experiments'/entry['image_run'];source=first(native)
        assert source[0]['jpeg_sha256']==entry['jpeg_sha256']
        assert plan['binary_sha256']==meta['files']['sp_vision_25/build/simulator/sim_auto_aim']
        sealed.append(dict(case=entry['case'],files={name:sha(replay/name) for name in ('plan.json','events.jsonl','result.json')}))
    units={}
    for name in ('alignment','sync','contract','evidence'):
        log=root/('unit-'+name+'.log');text=log.read_text();assert text.rstrip().endswith('OK')
        units[name]=dict(tests=int(re.search(r'Ran (\d+) tests',text)[1]),sha256=sha(log))
    cap=load(Path(old_validation['ordinary_capacity']['benchmark']));key=signature(cap['bundle'])
    capacity=load(jobs.STATE/'capacity.json');assert capacity[key]['healthy_parallelism']==2
    with jobs.connect() as db:
        assert not db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]
        assert not db.execute("SELECT count(*) FROM artifacts WHERE state IN ('queued','running')").fetchone()[0]
    validation=dict(mechanisms_passed=result['calibration_timing_passed'] and result['preparation_release_passed'],
        first_capture_gate_passed=False,independent_effect_gate_passed=effect['aggregate_gate_passed'],
        ui_equivalence_certified=False,capacity_registration=False,
        first_frame_causal_replays=24,timing_and_release_live_trials=24,independent_holdout_trials=20,
        unit_checks=units,total_unit_checks=sum(p['tests'] for p in units.values()),
        evidence=dict(runs=44,decoded_full_session_jpegs=sum(p['decoded_full_session_jpegs'] for p in audits),
            decoded_measured_jpegs=sum(p['decoded_measured_jpegs'] for p in audits),
            decoded_mp4_frames=sum(p['decoded_mp4_frames'] for p in audits)),
        maximum_live_trial_wall_s=max(result['maximum_new_trial_wall_s'],max(p['wall_s'] for p in effect['trials'])),
        frozen_ui_algorithm_renderer_and_scoring_preserved=list(protected),
        original_default_tags_and_runtime_selector_unchanged=True,
        ordinary_auto_parallelism=2,aligned_auto_parallelism=1,
        first_replay_evidence=sealed,figures_and_native_first_frames_reviewed=True,
        complete_human_video_acceptance=False,active_jobs=0,active_media=0,
        active_vt=Path('/sys/class/tty/tty0/active').read_text().strip(),
        diagnostic_sources={p.name:sha(p) for p in manage.OPS.glob('sync_timing*') if p.suffix=='.py'})
    assert validation['maximum_live_trial_wall_s']<40
    write(root/'validation.json',validation)
    finish(root)
    print(json.dumps({k:v for k,v in validation.items() if k not in ('first_replay_evidence','diagnostic_sources','frozen_ui_algorithm_renderer_and_scoring_preserved')},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();verify(a.output.resolve())
