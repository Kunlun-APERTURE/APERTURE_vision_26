"""Immutable-source before/after videos; explicit exports use a durable idle queue."""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from report import load, write

OPS=Path(__file__).resolve().parent
WORKSPACE=OPS.parent.parent
QUEUE=OPS/'.runtime/review-videos'
FONT=Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
DEFAULT_STUDY=OPS/'experiments/pixel-rotation-ui-probe-20261027'
SCENES={'hf_turn90_3':'90°变向','hf_uniform_3':'匀速平移','hf_spin_pos':'正向旋转','hf_spin_neg':'反向旋转','ego_right_centered':'自车移动','hf_static_5':'静止'}
for case,label in (('variable','变速移动旋转'),('turn08','0.8秒移动变向'),('turn04','0.4秒移动变向'),('spin_startstop','移动中旋转启停'),('pursuit','己方追击')):
    for sign,direction in (('pos','正转'),('neg','反转')):
        SCENES['adv_'+case+'_'+sign]=label+' / '+direction
        SCENES['adv_'+case+'_fast_'+sign]=label+' / '+direction+' / 压力档'
for speed in (3,4):
    for sign,direction in (('pos','正转'),('neg','反转')):SCENES['basic_move'+str(speed)+'_spin_'+sign]=str(speed)+'m/s 移动旋转 / '+direction
SOURCE_FILES=('manifest.json','run-state.json','summary.json','frames.jsonl','input.mjpg','telemetry.jsonl','session-evidence/events.jsonl')

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()

def lines(path):
    with Path(path).open() as stream:
        for line in stream:
            if line.strip():yield json.loads(line)

def require(value,message):
    if not value:raise ValueError(message)

def frame_at(times,t):
    import bisect
    return bisect.bisect_right(times,t)-1

def shot_time(row,start_ns):
    terminal=row.get('terminal_stamp_ns')
    return (terminal-start_ns)*1e-9 if terminal is not None else None

def shot_snapshot(shots,t,start_ns,measurement_s,settled=False):
    fired=[s for s in shots if s['fire_time_s']<=t+1e-9]
    resolved=[s for s in fired if settled or (shot_time(s,start_ns) is not None and shot_time(s,start_ns)<=t+1e-9)]
    hits=[s for s in resolved if s['useful_hit']];n=len(fired);elapsed=min(max(t,0),measurement_s)
    return dict(shots=n,hits=len(hits),in_flight=n-len(resolved),misses=len(resolved)-len(hits),exact=len(hits)/n if n else None,
        weighted=sum(s.get('weighted_hit_score') or 0 for s in hits)/n if n else None,hits_per_s=len(hits)/elapsed if elapsed>0 else None,
        impacts=sorted((s for s in resolved if s.get('impact_x_fraction') is not None and s['useful_hit']),key=lambda s:shot_time(s,start_ns) or 0))

def validate_pair(a,b):
    for k in ('scene','sensor_seed','random_seed','terrain','input_contract','warmup_frames','algorithm_weights_sha256','visibility_model','impact_quality_version','duration_s'):
        require(a.get(k)==b.get(k),'Unpaired experiment field: '+k)
    for k in ('physics','aim_origin','bullet_speed','reported_bullet_speed','gimbal_dynamics','gimbal_profile','fire_policy','fire_rate_limit_hz','hit_method','projectile_lifetime_s'):
        require(a['shooting'].get(k)==b['shooting'].get(k),'Different shooting condition: '+k)
    for k in ('mode','timing_profile','detector_history'):
        require(a['execution'].get(k)==b['execution'].get(k),'Different execution condition: '+k)
    for k in ('simulator_image_id','bridge_image_id','evaluator_image_id'):
        require(a.get('containers',{}).get(k)==b.get('containers',{}).get(k),'Different simulator/evaluator image: '+k)

def verify_seal(root):
    require((root/'report-seal.json').is_file(),'Missing completed report seal: '+str(root))
    for n,h in load(root/'report-seal.json').items():require(digest(root/n)==h,'Changed sealed evidence: '+str(root/n))

def prepare(study,baseline='baseline',candidate=None,output=None,statistics=None,report=None):
    study=Path(study).resolve();s,c=load(study/'state.json'),load(study/'contract.json')
    require(s and c and s['state']=='completed','Complete the optimization study first');verify_seal(study)
    candidate=candidate or s.get('selected') or c.get('candidate')
    if not candidate and c.get('parent'):
        parent_state=load(Path(c['parent'])/'state.json') or {};parent_contract=load(Path(c['parent'])/'contract.json') or {}
        candidate=parent_state.get('selected') or parent_contract.get('candidate')
    require(candidate and candidate!=baseline,'Specify the final candidate; do not infer it from individual scores')
    output=Path(output).resolve() if output else study.with_name(study.name+'-visual-review')
    require(not output.is_relative_to(study),'Export must be outside the sealed study')
    if (output/'export.json').exists():
        old=load(output/'export.json');require((old['study'],old['baseline'],old['candidate'])==(str(study),baseline,candidate),'Output belongs to another comparison');verify_inputs(old);return output
    rows=[r for r in s['runs'] if r['variant'] in (baseline,candidate)]
    ui=[r for r in rows if r.get('engine')=='ui' or r['phase'].startswith('ui')]
    rows=ui or [r for r in rows if r['phase'] in ('audit','confirm','regression')]
    require(rows,'No final paired UI or independent backend evidence');paired={}
    for r in rows:
        key=(r['scene'],r['sensor_seed'],r.get('repeat_id',0),r['phase'])
        require(r['variant'] not in paired.setdefault(key,{}),'Duplicate paired case');paired[key][r['variant']]=r
    require(all(set(v)=={baseline,candidate} for v in paired.values()),'Missing before/after case')
    statistics=Path(statistics).resolve() if statistics else Path(c.get('parent',study))
    if not (statistics/'effect-result.json').exists():statistics=study
    ss,sc=load(statistics/'state.json'),load(statistics/'contract.json')
    require(ss and ss['state']=='completed','Incomplete statistics study');verify_seal(statistics)
    for v in (baseline,candidate):
        a=load(Path(c['bundles'][v])/'bundle.json');b=load(Path(sc['bundles'][v])/'bundle.json')
        for k in ('sp_vision_25/build/simulator/sim_auto_aim','sp_vision_25/configs/simulator.yaml'):
            require(a['files'][k]==b['files'][k],'Statistics use a different candidate payload')
    from decision_benchmark_report import aggregate,unified_score
    output.mkdir(parents=True,exist_ok=True);cases=[];frozen={}
    for key,pair in paired.items():
        case=dict(scene=key[0],sensor_seed=key[1],phase=key[3],sides=[]);manifests=[]
        for v in (baseline,candidate):
            r=pair[v];folder=OPS/'experiments'/r['run_id'];require(not output.is_relative_to(folder),'Cannot write into source evidence')
            m,st=load(folder/'manifest.json'),load(folder/'run-state.json');manifests.append(m)
            require(load(folder/'summary.json')['verdict']!='invalid' and load(folder/'session-evidence-audit.json')['passed'],'Invalid source experiment')
            score=unified_score(folder,output/'normalized-scores'/r['run_id'],c)['metrics']
            if 'shots' in r:
                require((score['shots_fired'],score['target_armor_hits'])==(r['shots'],r['hits']),'Registered counts changed')
                require(abs(score['weighted_hit_score_sum']-r['weight'])<1e-10,'Registered weight changed')
            hashes={n:digest(folder/n) for n in SOURCE_FILES}
            for extra in ('configuration/scenario-plan.json','motion-analysis.json'):
                if (folder/extra).exists():hashes[extra]=digest(folder/extra)
            if (folder/'annotated.avi').exists():hashes['annotated.avi']=digest(folder/'annotated.avi')
            frozen[str(folder)]=hashes;fr=list(lines(folder/'frames.jsonl'));stamps=[x['stamp_ns'] for x in fr]
            require(stamps and all(a<b for a,b in zip(stamps,stamps[1:])),'Nonincreasing camera timestamps')
            end=(st['measurement_end_stamp_ns']-st['measurement_start_stamp_ns'])*1e-9
            shots=list(lines(output/'normalized-scores'/r['run_id']/'shots.jsonl'))
            terminal=[shot_time(x,st['measurement_start_stamp_ns']) for x in shots if x.get('terminal_stamp_ns')]
            tail=(stamps[-1]-st['measurement_start_stamp_ns'])*1e-9
            require(tail>=end and (not terminal or max(terminal)<=tail),'Missing measurement/impact tail')
            case['sides'].append(dict(variant=v,run_id=r['run_id'],folder=str(folder),start_ns=st['measurement_start_stamp_ns'],measurement_s=end,tail_end_s=tail,score=score))
        validate_pair(*manifests);case['play_s']=max(x['tail_end_s'] for x in case['sides']);cases.append(case)
    rr=[r for r in ss['runs'] if r['variant'] in (baseline,candidate) and r['phase'] in ('audit','confirm','regression')];summary=[]
    for phase in dict.fromkeys(r['phase'] for r in rr):
        selected=[r for r in rr if r['phase']==phase];scenes=list(dict.fromkeys(r['scene'] for r in selected))
        if phase!='regression' and not c.get('no_algorithm_changes'):scenes.insert(0,'all')
        for scene in scenes:
            a=[r for r in selected if r['variant']==baseline and (scene=='all' or r['scene']==scene)]
            b=[r for r in selected if r['variant']==candidate and (scene=='all' or r['scene']==scene)]
            require({(r['scene'],r['sensor_seed']) for r in a}=={(r['scene'],r['sensor_seed']) for r in b},'Unpaired statistics')
            summary.append(dict(phase=phase,scene=scene,seeds=len({r['sensor_seed'] for r in a}),before=aggregate(a),after=aggregate(b)))
    names=('contract.json','state.json','report-seal.json','validation.json','report.md','effect-result.json','paired-effects.json','repeatability.json')
    for root in dict.fromkeys((study,statistics)):frozen[str(root)]={n:digest(root/n) for n in names if (root/n).exists()}
    source_dir=output/'renderer-source';source_dir.mkdir(exist_ok=True)
    source_names=('review_video.py','review_video_render.py','review_video_checks.py','report.py','shots.py','impact_quality.py','scenario_catalog.py','highspeed_scenarios.py','focus_scenarios.py','movement_scenarios.py')
    for name in source_names:shutil.copy2(OPS/name,source_dir/name)
    font=output/'review-font.ttc';shutil.copy2(FONT,font)
    frozen[str(output)]={str(p.relative_to(output)):digest(p) for p in (output/'normalized-scores').rglob('*') if p.is_file()}
    spec=dict(version=1,study=str(study),statistics=str(statistics),report=str(Path(report).resolve()) if report else str(statistics/'report.md'),baseline=baseline,baseline_label='v3' if baseline=='baseline' else baseline,candidate=candidate,
        engine='UI 实录' if ui else '后台仿真实验',capability_study=bool(c.get('no_algorithm_changes')),cases=cases,summary=summary,acceptance_passed=bool(ss.get('independently_verified') or ss.get('backend_confirmed')),
        qualification=load(statistics/'effect-result.json') or load(statistics/'confirm-result.json'),paired_effects=load(statistics/'paired-effects.json'),review_status='pending_team_review',
        render=dict(width=2560,height=1440,fps=30,codec='h264',intro_s=5,title_s=2,hold_s=2,summary_s=15),source_hashes=frozen,
        scorer_sha256={n:digest(OPS/n) for n in ('shots.py','impact_quality.py')},code_sha256={n:digest(source_dir/n) for n in source_names},renderer_source=str(source_dir),
        font_sha256=digest(font),font_path=str(font),evaluator_image=load(Path(c['bundles'][candidate])/'bundle.json')['images']['evaluator'])
    write(output/'export.json',spec);return output

def verify_inputs(spec):
    for root,files in spec['source_hashes'].items():
        for n,h in files.items():require(digest(Path(root)/n)==h,'Source evidence changed: '+str(Path(root)/n))
    for n,h in spec['code_sha256'].items():require(digest(Path(spec['renderer_source'])/n)==h,'Frozen renderer changed: '+n)
    require(digest(Path(spec['font_path']))==spec['font_sha256'],'Font changed')

def busy():
    dbfile=OPS/'.runtime/jobs/queue.sqlite3'
    if dbfile.exists():
        with sqlite3.connect('file:'+str(dbfile)+'?mode=ro',uri=True) as db:
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:return True
            if db.execute("SELECT count(*) FROM artifacts WHERE state IN ('queued','running')").fetchone()[0]:return True
    return False

def install_worker():
    unit=Path.home()/'.config/systemd/user';unit.mkdir(parents=True,exist_ok=True)
    service='\n'.join(('[Unit]','Description=RM26 offline team review video queue','[Service]','Type=oneshot',
        'ExecStart='+sys.executable+' '+str(OPS/'review_video.py')+' worker','Nice=10','CPUQuota=200%','TimeoutStartSec=infinity',''))
    timer='[Unit]\nDescription=Run queued RM26 reviews when experiments are idle\n[Timer]\nOnBootSec=45\nOnUnitInactiveSec=30\nPersistent=true\n[Install]\nWantedBy=timers.target\n'
    (unit/'rm26-review-videos.service').write_text(service);(unit/'rm26-review-videos.timer').write_text(timer)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','rm26-review-videos.timer'],check=True,capture_output=True)

def auto_discover():
    """Legacy entry point: completed experiments never request videos automatically."""
    return

def enqueue(output):
    spec=load(output/'export.json');verify_inputs(spec);QUEUE.mkdir(parents=True,exist_ok=True)
    request=QUEUE/(hashlib.sha256(str(output).encode()).hexdigest()[:20]+'.json');old=load(request) or {}
    if old.get('state')=='completed':validate_export(output);publish(output);return
    if old.get('state') in ('queued','running'):return
    write(request,dict(state='queued',output=str(output),requested=time.time(),attempts=old.get('attempts',0)))
    write(output/'render-status.json',dict(state='queued',review_status='pending_team_review'));install_worker()
    subprocess.run(['systemctl','--user','start','--no-block','rm26-review-videos.service'],check=True)

def publish(output):
    import experiments,manage
    spec=load(output/'export.json')
    for side in (s for c in spec['cases'] for s in c['sides']):
        folder=Path(side['folder']);target=manage.DESKTOP/'.local/share/rm26-sim/experiments'/folder.name
        if not (target/'report.html').exists():experiments.publish_desktop(manage,folder)
    for root in dict.fromkeys((Path(spec['study']),Path(spec['statistics']))):
        target=manage.DESKTOP/'.local/share/rm26-sim/experiments'/root.name;target.mkdir(parents=True,exist_ok=True)
        for p in root.iterdir():
            if p.is_file() and p.suffix in ('.md','.json','.html','.png'):
                dest=target/p.name
                if dest.exists() and os.path.samefile(p,dest):continue
                dest.unlink(missing_ok=True);os.link(p,dest)
    experiments.publish_desktop(manage,output)

def worker():
    QUEUE.mkdir(parents=True,exist_ok=True)
    with (QUEUE/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        if busy():return
        with (OPS/'.runtime/operation.lock').open('a') as operation:
            try:fcntl.flock(operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            for request in sorted(QUEUE.glob('*.json')):
                item=load(request)
                if not item or item.get('state') not in ('queued','running'):continue
                if busy():return
                output=Path(item['output'])
                try:
                    spec=load(output/'export.json');verify_inputs(spec)
                    item.update(state='running',started=time.time(),attempts=item['attempts']+1);write(request,item)
                    write(output/'render-status.json',dict(state='running',review_status='pending_team_review'))
                    cmd=['docker','run','--rm','--init','--network','none','--cpus','2','--memory','4g','--user',f'{os.getuid()}:{os.getgid()}',
                        '--env','OPENBLAS_NUM_THREADS=1','--env','OMP_NUM_THREADS=1','--volume',str(WORKSPACE)+':'+str(WORKSPACE)+':ro',
                        '--volume',str(output)+':'+str(output),
                        '--entrypoint','python3',spec['evaluator_image'],'-B',str(Path(spec['renderer_source'])/'review_video_render.py'),str(output)]
                    name='rm26-review-'+request.stem
                    cmd[3:3]=['--name',name,'--label','rm26.review=true','--label','rm26.review.output='+str(output)]
                    existing=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
                    if existing.returncode==0:
                        record=json.loads(existing.stdout)[0]
                        require(record['Config'].get('Labels',{}).get('rm26.review.output')==str(output),'Unexpected existing render container')
                        if record['State']['Running']:
                            code=subprocess.check_output(['docker','wait',name],text=True).strip()
                            require(code=='0','Retained render container failed')
                        else:subprocess.run(['docker','rm',name],check=True,capture_output=True)
                    if not (output/'export-seal.json').exists():
                        with (output/'render.log').open('a') as log:
                            process=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT);paused=False
                            while process.poll() is None:
                                should_pause=busy()
                                if should_pause!=paused:
                                    subprocess.run(['docker','pause' if should_pause else 'unpause',name],check=True,stdout=log,stderr=log)
                                    paused=should_pause
                                time.sleep(2)
                            require(process.returncode==0,'Offline video renderer failed; source trials retained')
                    validate_export(output);publish(output);item.update(state='completed',ended=time.time(),error=None)
                    write(output/'render-status.json',dict(state='completed',review_status='pending_team_review'))
                except Exception as error:
                    item.update(state='failed',ended=time.time(),error=repr(error))
                    write(output/'render-status.json',dict(state='failed',error=repr(error),review_status='pending_team_review'))
                write(request,item)

def validate_export(output):
    spec=load(output/'export.json');verify_inputs(spec);v=load(output/'validation.json');require(v and v['passed'],'Missing export validation')
    for n,h in load(output/'export-seal.json').items():require(digest(output/n)==h,'Changed export artifact: '+n)
    return v

def export_completed(study,candidate=None,*,requested=False):
    """Compatibility hook; export only when the user explicitly requested a video."""
    if not requested:return
    enqueue(prepare(study,candidate=candidate))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('export','prepare','worker','validate','status','retry'))
    p.add_argument('--study',type=Path,default=DEFAULT_STUDY);p.add_argument('--baseline',default='baseline');p.add_argument('--candidate')
    p.add_argument('--statistics',type=Path);p.add_argument('--report',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    if a.action=='worker':worker()
    elif a.action in ('export','prepare'):
        out=prepare(a.study,a.baseline,a.candidate,a.output,a.statistics,a.report)
        if a.action=='export':enqueue(out)
        print(out)
    elif a.action=='retry':require(a.output,'--output required');enqueue(a.output.resolve())
    elif a.action=='validate':require(a.output,'--output required');print(json.dumps(validate_export(a.output.resolve()),ensure_ascii=False,indent=2))
    else:print(json.dumps([load(x) for x in sorted(QUEUE.glob('*.json')) if x.name!='policy.json'],ensure_ascii=False,indent=2))
if __name__=='__main__':main()
