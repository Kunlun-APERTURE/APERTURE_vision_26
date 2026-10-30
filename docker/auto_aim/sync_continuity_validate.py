"""Final source, default capacity, protocol, evidence and smoke reconciliation."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import background_capacity as capacity
import background_jobs as jobs
import manage
from report import load,write


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(root):
    smoke=load(root/'runtime-smoke/continuity-state.json')
    if smoke['state']!='completed':raise ValueError('Finish runtime smoke first')
    bundle=Path(smoke['bundle']);meta=load(bundle/'bundle.json')
    original=load(root.parent/'lockstep-diagnosis-20261023/cadence-candidate/diagnosis-state.json')
    before=load(Path(original['bundle'])/'bundle.json')
    algorithm={name:value for name,value in before['files'].items() if name.startswith('sp_vision_25/')}
    differences=[name for name,value in algorithm.items() if meta['files'].get(name)!=value]
    protected=digest(manage.ROOT/'sp_vision_25/tests/auto_aim_test.cpp')
    expected='7c08603df343de8c503b8e44c3ec07d68f3a6fee0471d7a9666fe890bd3a1ee2'
    assert not differences and protected==expected,(differences,protected)
    runtime=('bridge.py','sync_bridge.py','sync_timing.py','session_evidence.py','experiments.py',
        'background_jobs.py','background_capacity.py','compose.yml')
    frozen={name:digest(manage.OPS/name)==meta['files']['docker/auto_aim/'+name] for name in runtime}
    assert all(frozen.values()),frozen
    default_images={name:manage.inspect(tag)['Id'] for name,tag in (
        ('simulator',manage.IMAGE),('evaluator','daedalus:rm26-evaluator'),('algorithm','daedalus:rm26-algorithm'))}
    default_meta=copy.deepcopy(meta);default_meta['images']=default_images
    with tempfile.TemporaryDirectory() as directory:
        folder=Path(directory);write(folder/'bundle.json',default_meta)
        job=dict(spec=json.dumps({'_protocol':2}),parallelism=0,exploratory=0,bundle=str(folder),
            settings=json.dumps(dict(execution_profile='backend-240-v1',session_evidence='auto')))
        default_lanes=capacity.limit(job,load(jobs.STATE/'capacity.json',{}),{})
    assert default_lanes==2,default_lanes
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in ('sync_checks','session_evidence_checks'))
    with (root/'final-unit-checks.log').open('w') as stream:
        check=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    units=dict(passed=check.wasSuccessful(),count=check.testsRun,failures=len(check.failures),errors=len(check.errors))
    write(root/'final-unit-checks.json',units);assert units['passed'],units
    protocol=load(root/'receipt-candidate/protocol-checks.json');assert protocol['passed']
    evidence=load(root/'final-full-history-audit.json');assert evidence['passed'] and evidence['runs']==24
    assert all(r['measurement']['video'] for r in evidence['results']),'Complete all measurement MP4s before final audit'
    smoke_checks=[]
    for mode,value in smoke['modes'].items():
        for entry in value['runs']:
            folder=manage.OPS/'experiments'/entry['run_id']
            health=capacity.assess(folder,reference=240)
            assert health['healthy'],health
            smoke_checks.append(dict(mode=mode,run_id=entry['run_id'],**health))
    with jobs.connect() as db:
        active=db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]
        media=db.execute("SELECT count(*) FROM artifacts WHERE state IN ('queued','running')").fetchone()[0]
        status=jobs.status(db,smoke['job_id'])
    assert active==0 and media==0,(active,media)
    lanes=[load(jobs.STATE/'attempts'/a['id']/'request.json')['effective_parallelism'] for a in status['attempts']]
    assert lanes==[1,1],lanes
    timing=[load(manage.OPS/'experiments'/r['run_id']/'timing.json')['wall_seconds'] for r in evidence['results']]
    invalid=load(root.parent/'lockstep-continuity-20261023/continuity-state.json')['reference'][0]
    assert load(manage.OPS/'experiments'/invalid['run_id']/'summary.json')['verdict']=='invalid'
    result=dict(passed=True,unit_checks=units,protocol_checks=dict(passed=True,count=len(protocol['checks'])),
        evidence_audit={k:evidence[k] for k in ('passed','runs','full_history_decoded','measurement_decoded')},
        decoded_mp4_frames=sum(int(r['measurement']['video']['nb_read_frames']) for r in evidence['results']),
        algorithm_files_unchanged=len(algorithm),algorithm_differences=differences,protected_test_sha256=protected,
        frozen_final_runtime=frozen,final_bundle=str(bundle),default_images=default_images,
        default_auto_parallelism=default_lanes,full_recording_auto_lanes=lanes,smoke_checks=smoke_checks,
        maximum_trial_wall_s=max(timing),all_valid_trials_within_40s=max(timing)<=40,
        preserved_invalid_attempt=invalid['run_id'],active_jobs=active,active_media=media,
        active_vt=Path('/sys/class/tty/tty0/active').read_text().strip(),
        visual_review='Original-image crops and timelines viewed; full human video acceptance remains incomplete',
        ui_equivalence_certified=False,new_capacity_registered=False)
    assert result['active_vt']=='tty8'
    write(root/'validation.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    print(json.dumps(validate(args.output.resolve()),indent=2))
