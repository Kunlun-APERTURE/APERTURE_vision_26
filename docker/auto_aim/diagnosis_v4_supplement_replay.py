"""Acquire diagnostic replays as bounded supplemental recordings finish."""
import json,time
from diagnosis_v4 import OUTPUT,HISTORY,replay,verify
from report import load,write

def main():
    path=OUTPUT/'supplement/replay-validation.json';done=load(path,{}).get('runs',[])
    while True:
        batch=load(OUTPUT/'supplement/batch.json',{})
        for record in batch.get('runs',[]):
            row=record['inventory']
            if any(r['run_id']==row['run_id'] for r in done):continue
            dest=replay(row);check=verify(row,dest)
            ev=[json.loads(l)['data'] for l in (HISTORY.parent/row['run_id']/'telemetry.jsonl').open() if json.loads(l)['kind']=='observation']
            rr={r['stamp_ns']:r for r in (json.loads(l) for l in (dest/'results.jsonl').open())}
            # Ignore the already-running prefix; the next session must include its first initialization.
            initial_session=ev[0]['frame']['session'];fresh=[o for o in ev if o['frame']['session']!=initial_session]
            differences=[];errors=[]
            for o in fresh:
                stamp=o['frame']['stamp_ns'];a=o['result'].get('filter_state');b=rr.get(stamp,{}).get('filter_state')
                if (a is None)!=(b is None):differences.append(dict(stamp_ns=stamp,reason='track_presence'))
                if a is not None and b is not None:
                    error=max(abs(x-y) for x,y in zip(a,b));errors.append(error)
                    if error>1e-6:differences.append(dict(stamp_ns=stamp,max_state_error=error))
            check['live_state_reproduction']=dict(fresh_frames=len(fresh),compared_states=len(errors),maximum_error=max(errors,default=None),
                passed=bool(fresh) and not differences,differences=differences[:10],tolerance=1e-6)
            done.append(check);write(path,dict(complete=False,runs=done,passed=all(r['passed'] for r in done)))
            replay(row,'no-hint',suppress=True)
            if row['scene'] in ('hf_static_8','hf_turn90_3'):
                for delay in ('original','measured'):replay(row,'delay-'+delay,delay=delay)
            print(json.dumps(dict(supplement_replayed=len(done),run_id=row['run_id'],check=check)),flush=True)
        if batch.get('complete') and len(done)==len(batch['runs']):break
        time.sleep(1)
    write(path,dict(complete=True,runs=done,passed=all(r['passed'] for r in done)))

if __name__=='__main__':main()
