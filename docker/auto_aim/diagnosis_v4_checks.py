"""Checks on session fidelity, diagnostic transport and immutable source evidence."""
import json,subprocess
from diagnosis_v4 import OUTPUT,ALGORITHM,HISTORY,digest,verify,without_diagnosis,difference,replay,source
from report import load,write

def main():
    batch=load(OUTPUT/'supplement/batch.json');results=[]
    for record in batch['runs']:
        row=record['inventory'];diagnostic=replay(row,'release-diagnosis');check=verify(row,diagnostic)
        # Validate the native metadata limiter, not a Python copy of it.
        command=['docker','exec','-i','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine','build/simulator/sim_auto_aim',
            '--replay=/dev/stdin','--replay-output=/dev/stdout','--runtime=build/sim/diagnosis-runtime.yaml','--config=build/sim/diagnosis-config.yaml',
            '--algorithm-profile='+row['profile'],'--diagnosis=true','--replay-wire-diagnostics=true']
        result=subprocess.run(command,input=source(HISTORY.parent/row['run_id']),text=True,capture_output=True,check=True)
        wire=[json.loads(l) for l in result.stdout.splitlines() if l.startswith('{')]
        full=[json.loads(l) for l in (diagnostic/'results.jsonl').open()]
        max_size=0;truncated=0
        for r,a in zip(wire,full):
            assert difference(without_diagnosis(r),without_diagnosis(a)) is None
            for pose in r.get('continuity',{}).get('diagnosis_poses',[]):
                assert len(pose.get('candidates',[]))<=24
                if pose.get('candidates_truncated'):
                    truncated+=1;chosen=pose['chosen_index'];assert pose['candidate_indices'][chosen]==pose['full_chosen_index']
                    assert abs(pose['candidates'][chosen][0]-pose['chosen_yaw'])<1e-12
            wrapped={'frame':{},'result':r,'version':1,'type':'observation'}
            max_size=max(max_size,len(json.dumps(wrapped,separators=(',',':'))))
        assert len(wire)==len(full) and max_size<60000
        results.append(dict(run_id=row['run_id'],decision_check=check['passed'],native_wire_frames=len(wire),maximum_result_envelope_bytes=max_size,truncated_pose_records=truncated))
        print(json.dumps(dict(release_checked=len(results),run_id=row['run_id'],maximum_result_envelope_bytes=max_size)),flush=True)
    guard=['docker','exec','--user','1000:1000','--workdir','/workspace/sp_vision_25','sp_vision_sunshine','build/simulator/sim_auto_aim',
        '--replay-no-yaw-hint=true','--runtime=build/sim/diagnosis-intervention-guard.yaml']
    reject=subprocess.run(guard,capture_output=True,text=True)
    assert reject.returncode and 'Interventions require offline replay' in reject.stderr
    original_test=digest(ALGORITHM/'tests/auto_aim_test.cpp');assert original_test=='7c08603df343de8c503b8e44c3ec07d68f3a6fee0471d7a9666fe890bd3a1ee2'
    write(OUTPUT/'release-checks.json',dict(passed=all(r['decision_check'] for r in results),runs=results,live_intervention_rejected=True,
        original_test_sha256=original_test,final_binary_sha256=digest(ALGORITHM/'build/simulator/sim_auto_aim'),
        wire_note='Full candidates stay in offline replay. Live metadata retains at most 24 low-cost/selected candidates and includes original indices.'))

if __name__=='__main__':main()
