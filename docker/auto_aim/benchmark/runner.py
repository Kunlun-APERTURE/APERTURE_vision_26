"""Queue-backed runner; no dependency on a particular study or its results."""
import json
import time
import uuid
from pathlib import Path
from dataclasses import asdict
from .core import EnvironmentSpec, ExperimentSpec, identity
from .scenarios import selection,get
from .algorithms import get as algorithm
from .artifacts import freeze,verify,seal
from report import load,write
import manage
import background_jobs as jobs


class Runner:
    def submit(self,spec,root,artifact,parallelism=1,resume=False,bundle_path=None):
        spec.validate();root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
        with (root/'controller.lock').open('a') as lock:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            previous=load(root/'contract.json',{})
            if previous:
                if not resume or dict(previous['spec'],renderer=previous['spec'].get('renderer','auto'))!=json.loads(json.dumps(asdict(spec))):raise ValueError('Existing output requires --resume with identical specification')
                if identity(previous)!=load(root/'state.json')['contract_sha256']:raise ValueError('Frozen contract changed')
                frozen=verify(Path(previous['bundle']),previous.get('payload_identity'))
                if bundle_path and Path(bundle_path).resolve()!=Path(previous['bundle']).resolve():raise ValueError('Resume bundle differs')
                if artifact and Path(artifact).is_file() and not bundle_path:
                    from .core import sha
                    if sha(artifact)!=frozen['algorithm']['binary_sha256']:raise ValueError('Resume algorithm artifact differs')
                state=load(root/'state.json')
                with jobs.connect() as db:
                    exists=db.execute('SELECT id FROM jobs WHERE id=?',(state['job_id'],)).fetchone()
                if not exists:
                    if not previous.get('rows'):raise ValueError('Incomplete legacy submission; use a new output')
                    self.enqueue(state['job_id'],previous['bundle'],previous['rows'],previous.get('parallelism',1))
                return state
            if resume:raise ValueError('No frozen experiment to resume')
            env=EnvironmentSpec() if spec.environment=='E1' else EnvironmentSpec('legacy','legacy')
            if spec.environment not in ('E1','legacy'):raise ValueError('Unknown environment')
            adapter=None if bundle_path else algorithm(spec.algorithm)
            plans={scene:get(scene).generate(spec.trajectory_seed).manifest() for scene in spec.scenes}
            descriptor=verify(bundle_path)['algorithm'] if bundle_path else adapter.describe(manage.ROOT,artifact)
            rows=[]
            for scene in spec.scenes:
                for seed in spec.sensor_seeds:
                    for repeat in range(spec.repeats):
                        settings=env.settings(scene,seed,spec.trajectory_seed)
                        settings['algorithm_profile']=descriptor.get('algorithm_profile','image_v1')
                        row=jobs.validate(settings)
                        row.update(repeat_id=repeat,execution_profile='lockstep-'+env.timing_profile)
                        rows.append(row)

            name='benchmark_'+uuid.uuid4().hex[:12]
            if bundle_path:
                bundle=Path(bundle_path).resolve();meta=verify(bundle)
                if meta.get('environment')!=asdict(env):raise ValueError('Imported environment differs')
                requested=spec.algorithm
                if Path(requested).is_file():requested=algorithm(requested).data['id']
                if meta.get('algorithm',{}).get('id')!=requested:raise ValueError('Imported algorithm differs')
            else:bundle=freeze(name,env,adapter,artifact,renderer=spec.renderer)
            from render_backend import frozen
            rendering=frozen(verify(bundle))
            if spec.renderer!='auto' and rendering['backend']!=spec.renderer:raise ValueError('Imported renderer differs from request')
            if parallelism>1:
                from .qualification import signature
                registry=load(jobs.STATE/'benchmark-capacity.json',{})
                if any(registry.get(signature(bundle,row),{}).get('parallelism',1)<parallelism for row in rows):raise ValueError('Unqualified parallelism; run algorithm qualify on serial reference')
            contract=dict(payload_identity=verify(bundle).get('content_id'),rows=rows,parallelism=parallelism,version=1,spec=asdict(spec),plans=plans,bundle=str(bundle),algorithm=verify(bundle)['algorithm'],
                environment=asdict(env),scoring_version=2,impact_quality_version=1,
                maximum_complete_trial_s=40,trajectory_variation=False,renderer=rendering)
            contract=json.loads(json.dumps(contract))
            write(root/'contract.json',contract)
            if not bundle_path:
                meta=verify(bundle);meta['benchmark']=dict(contract_sha256=identity(contract),spec=contract['spec'])
                seal(bundle,meta)
            job_id='job_'+name
            state=dict(state='submitted',job_id=job_id,bundle=str(bundle),contract_sha256=identity(contract),items=len(rows))
            write(root/'state.json',state)
            self.enqueue(job_id,bundle,rows,parallelism)
            return state

    def enqueue(self,job_id,bundle,rows,parallelism):
        from .jobs import JobService
        return JobService().submit(job_id,bundle,rows,parallelism)

    def wait(self,root):
        root=Path(root);state=load(root/'state.json')
        while True:
            with jobs.connect() as db:status=jobs.status(db,state['job_id'])
            if status['state']=='completed':
                from .evaluation import report
                return report(root)
            if status['state'] in ('paused','cancelled'):raise RuntimeError('Inspect retained attempts: '+state['job_id'])
            if time.time()-load(jobs.STATE/'daemon-status.json',{}).get('heartbeat',0)>15:raise RuntimeError('Scheduler unavailable; start sim.sh jobs daemon or jobs install')
            time.sleep(1)


def execute(args):
    if args.list:return print(json.dumps(selection(args.tier,args.cases,args.pressure),indent=2))
    if not args.output:raise ValueError('--output required')
    seeds=tuple(int(x) for x in args.sensor_seeds.split(','))
    spec=ExperimentSpec(args.algorithm,args.environment,tuple(selection(args.tier,args.cases,args.pressure)),seeds,args.trajectory_seed,args.repeats,getattr(args,'renderer','auto'))
    if args.algorithm!='B1' and not args.artifact and not args.bundle and not args.resume:raise ValueError('--artifact required for independent algorithm')
    artifact=args.artifact or manage.OPS/'.runtime/benchmark/B1/build/sim_auto_aim'
    result=Runner().submit(spec,args.output,artifact,int(args.parallelism),args.resume,args.bundle)
    print(json.dumps(result,indent=2))
    if args.wait:Runner().wait(args.output)
