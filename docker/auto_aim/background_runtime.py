#!/usr/bin/env python3
"""One frozen experiment attempt. Never imports code from the mutable workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

import experiments
import manage
from report import load, write


class Worker:
    def __init__(self, request):
        self.attempt_started=time.monotonic()
        self.request = request
        self.bundle = Path(request['bundle'])
        self.ROOT = self.bundle
        self.OPS = Path(request['ops'])
        self.source_ops = self.bundle/'docker/auto_aim'
        self.DESKTOP = Path(request['desktop'])
        self.RUNTIME = Path(request['runtime'])
        self.RUNTIME.mkdir(parents=True, exist_ok=True)
        self.slot = request['slot']
        self.port = 18826+self.slot
        self.domain = 70+self.slot
        self.project = 'rm26-worker-'+str(self.slot)
        self.SIMULATOR = self.project+'-simulator'
        self.BRIDGE = self.project+'-bridge'
        self.DESKTOP_CONTAINER = self.project+'-algorithm'
        self.metadata = load(self.bundle/'bundle.json')
        from render_backend import frozen
        self.renderer=frozen(self.metadata)
        self.IMAGE = self.metadata['images']['simulator']
        self.gpu_uuid=self.metadata.get('gpu_uuid')
        self.settings = dict(request['settings'])
        self.lockstep=self.settings.get('execution_mode')=='lockstep'
        self.PIPELINE_VERSION=request.get('protocol',1)
        self.compose_path = self.RUNTIME/'compose.json'
        self.current_folder = None
        self.resources_stop = threading.Event()

    run = staticmethod(manage.run)

    def inspect(self, name):
        if name == 'daedalus:rm26-evaluator':
            name = self.metadata['images']['evaluator']
        return manage.inspect(name)

    def profile(self):
        return dict(self.settings)

    def save_profile(self, value):
        self.settings = dict(value)
        write(self.RUNTIME/'profile.json', value)

    def compose(self, *args, capture=False):
        return self.run(['docker','compose','--project-name',self.project,
            '--file',str(self.compose_path),*args], capture=capture)

    def rpc(self, op, **fields):
        body = json.dumps(dict(version=1, type='admin', op=op, **fields)).encode()
        with socket.create_connection(('127.0.0.1',self.port), timeout=2) as conn:
            from render_backend import wall_budget
            conn.settimeout(wall_budget(self.renderer,True) if self.lockstep else 12)
            conn.sendall(struct.pack('!II',len(body),0)+body)
            def read(n):
                result = b''
                while len(result)<n:
                    chunk = conn.recv(n-len(result))
                    if not chunk:
                        raise RuntimeError('Worker bridge disconnected')
                    result += chunk
                return result
            length, payload = struct.unpack('!II',read(8))
            if not 0<length<=65536 or payload:
                raise RuntimeError('Invalid worker bridge envelope')
            response = json.loads(read(length))
            if not response.get('ok'):
                raise RuntimeError(response.get('error','Worker bridge rejected request'))
            return response['result']

    def definition(self):
        s = self.settings
        if self.renderer['backend']=='nvidia':
            from render_backend import nvidia_devices
            available=nvidia_devices()
            if self.gpu_uuid not in available:raise ValueError('Frozen GPU unavailable; submit a new experiment with --renderer cpu')
        labels = {'rm26.background':'true','rm26.slot':str(self.slot),'rm26.workspace':str(self.OPS.parent.parent)}
        if os.environ.get('RM26_PR_TASK'):labels['aperture.pr']=os.environ['RM26_PR_TASK']
        common = dict(init=True, restart='no', network_mode='host', ipc='private',
            labels=labels, logging={'driver':'json-file','options':{'max-size':'20m','max-file':'2'}})
        if self.renderer['backend']=='cpu':common['runtime']='runc'
        ros = {'ROS_DOMAIN_ID':str(self.domain),'RMW_IMPLEMENTATION':'rmw_fastrtps_cpp',
            'FASTDDS_BUILTIN_TRANSPORTS':'UDPv4','ROS_LOCALHOST_ONLY':'1',
            'FASTRTPS_DEFAULT_PROFILES_FILE':'/opt/rm26/fastdds-udp.xml',
            'FASTDDS_DEFAULT_PROFILES_FILE':'/opt/rm26/fastdds-udp.xml'}
        volumes = [str(self.source_ops)+':/opt/rm26:ro',str(self.RUNTIME)+':/run/rm26-sim']
        simulator = dict(common, image=self.IMAGE, container_name=self.SIMULATOR, working_dir='/app',
            environment=dict(ros, WGPU_BACKEND='vulkan', HOME='/tmp/daedalus-home',
                XDG_RUNTIME_DIR='/tmp/daedalus-runtime',
                NVIDIA_DRIVER_CAPABILITIES='graphics,utility,compute',
                RUST_LOG='info', DAEDALUS_AUTO_AIM='1', DAEDALUS_INTEGRATION='1',
                DAEDALUS_PROJECTILE_VISIBLE=s['projectile_visible'],
                DAEDALUS_INTEGRATION_FIRE=s['fire'],DAEDALUS_BENCH_FIRE_POLICY=s['fire_policy'],
                DAEDALUS_ALGORITHM_PROFILE=s['algorithm_profile'],
                DAEDALUS_INTEGRATION_FIRE_RATE_HZ=str(s['fire_rate_hz']),
                DAEDALUS_GIMBAL_CMD_TIMEOUT_MS='250',RAYON_NUM_THREADS='2'),
            volumes=[str(self.RUNTIME/'config.toml')+':/app/config.toml:ro',
                str(self.source_ops)+':/opt/rm26:ro',
                str(self.source_ops/'scenarios')+':/scenarios:ro',
                str(self.RUNTIME/'scenarios')+':/run/rm26-scenarios:ro'],
            deploy={'resources':{'reservations':{'devices':[{'driver':'nvidia',
                'device_ids':[self.gpu_uuid],'capabilities':['gpu']}]}}},
            command=['bash','-c','mkdir -p "$$HOME" "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; exec /app/daedalus'])
        if self.lockstep:
            simulator['environment'].update(DAEDALUS_EXECUTION_MODE='lockstep',DAEDALUS_STEP_SOCKET='/run/rm26-sim/lockstep.sock')
            simulator['volumes'].append(str(self.RUNTIME)+':/run/rm26-sim')
        if self.renderer['backend']=='cpu':
            from render_backend import SOFTWARE_ICD
            simulator.pop('deploy',None)
            simulator['runtime']='runc'
            simulator['environment'].pop('NVIDIA_DRIVER_CAPABILITIES',None)
            simulator['environment'].update(VK_ICD_FILENAMES=SOFTWARE_ICD,
                VK_DRIVER_FILES=SOFTWARE_ICD,LIBGL_ALWAYS_SOFTWARE='1',NVIDIA_VISIBLE_DEVICES='void',LP_NUM_THREADS='4')
        bridge = dict(common, image=self.IMAGE,container_name=self.BRIDGE,environment=ros,
            volumes=volumes,command=['python3','-u','/opt/rm26/bridge.py','--port',str(self.port),
                '--fps','30','--bullet-speed',str(s['reported_bullet_speed']),
                '--actual-bullet-speed',str(s['bullet_speed']),'--fire',s['fire'],
                '--physics',s['physics'],'--fire-policy',s['fire_policy'],
                '--fire-rate-hz',str(s['fire_rate_hz']),'--algorithm-profile',s['algorithm_profile']])
        bridge['environment']=dict(ros,RM26_RECORD_TIMING='1' if s.get('record_timing')=='on' else '0')
        if self.metadata.get('algorithm'):
            bridge['environment']['RM26_ALGORITHM_PROTOCOL']=self.metadata['algorithm']['protocol']
        if self.lockstep:
            bridge['environment'].update(DAEDALUS_EXECUTION_MODE='lockstep',RM26_SCENE=s['scene'],
                RM26_TIMING_PROFILE='/opt/rm26/timing_profiles/'+s['timing_profile']+'.json')
            if s.get('preview')=='on':
                preview=self.DESKTOP/'.local/share/rm26-sim/lockstep-preview'/self.request['job_id']/str(self.slot)
                preview.mkdir(parents=True,exist_ok=True)
                bridge['volumes']=[*bridge['volumes'],str(preview)+':/preview']
                bridge['environment']['RM26_SYNC_PREVIEW']='/preview'
        algorithm = dict(common,image=self.metadata['images']['algorithm'],container_name=self.DESKTOP_CONTAINER,
            user=f'{os.getuid()}:{os.getgid()}',working_dir='/work',
            volumes=[str(self.bundle)+':/snapshot:ro',str(self.RUNTIME/'algorithm')+':/work'],
            entrypoint=['/bin/bash','-c'],
            command=['source /opt/intel/openvino/setupvars.sh >/dev/null && exec '
                '/snapshot/sp_vision_25/build/simulator/sim_auto_aim '
                '--config=/snapshot/sp_vision_25/configs/simulator.yaml '
                '--runtime=/work/calibration.yaml --view=false '
                f'--port={self.port} --fire={s["fire"]} --aim-origin={s["aim_origin"]} '
                f'--algorithm-profile={s["algorithm_profile"]} --execution-mode={s.get("execution_mode","realtime")}'
                +(' --detector-history=session' if s.get('detector_history')=='session' else '')])
        if self.metadata.get('algorithm'):
            import shlex
            descriptor=self.metadata['algorithm']
            argv=[a.format(port=self.port,**s) for a in descriptor['launch']]
            setup=descriptor.get('setup','source /opt/intel/openvino/setupvars.sh >/dev/null')
            algorithm['command']=[(setup+' && ' if setup else '')+'exec '+shlex.join(argv)]
        evaluator = dict(common, image=self.metadata['images']['evaluator'],
            profiles=['experiment'],user=f'{os.getuid()}:{os.getgid()}',
            environment=dict(ros, MPLCONFIGDIR='/tmp/matplotlib',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1'),
            volumes=[str(self.source_ops)+':/opt/rm26:ro',str(self.RUNTIME)+':/run/rm26-sim:ro',
                str(self.OPS/'experiments')+':/experiments'])
        return dict(services=dict(daedalus=simulator,bridge=bridge,algorithm=algorithm,evaluator=evaluator))

    def start(self):
        cold_start = time.monotonic()
        text = (self.bundle/'vision_simulator_26/config.toml').read_text()
        # render_config reads manage.ROOT: use the frozen module's already-correct ROOT.
        text = manage.render_config(self.settings)
        for section,key,value in [('window','headless','true'),('preview','enabled','false'),
                ('debug','egui','false'),('debug','inspector','false'),('camera','pip_enabled','false')]:
            text = manage.override(text,section,key,value)
        (self.RUNTIME/'scenarios').mkdir(exist_ok=True)
        algorithm_work = self.RUNTIME/'algorithm'
        algorithm_work.mkdir(exist_ok=True)
        for name in ('assets','configs'):
            link = algorithm_work/name
            if link.is_symlink():
                link.unlink()
            link.symlink_to('/snapshot/sp_vision_25/'+name)
        definition = self.definition()
        fingerprint = hashlib.sha256(json.dumps(dict(definition=definition,config=text),sort_keys=True).encode()).hexdigest()
        previous = load(self.RUNTIME/'environment.json',{})
        reuse = not self.lockstep and previous.get('fingerprint')==fingerprint and all(
            (self.inspect(n) or {}).get('State',{}).get('Running') for n in (self.SIMULATOR,self.BRIDGE))
        if not reuse:
            if self.compose_path.exists():
                self.compose('down','--remove-orphans')
            (self.RUNTIME/'config.toml').write_text(text)
            write(self.compose_path,definition)
            write(self.RUNTIME/'environment.json',{'fingerprint':fingerprint})
            self.compose('up','--detach','daedalus','bridge')
        write(self.RUNTIME/'profile.json',self.settings)
        algorithm_before = self.inspect(self.DESKTOP_CONTAINER) or {}
        algorithm_reused = (reuse and self.settings.get('algorithm_lifecycle','session') == 'session'
            and algorithm_before.get('State',{}).get('Running',False))
        if not algorithm_reused:
            self.compose('up','--detach','--force-recreate','algorithm')
        if self.lockstep and self.settings.get('preview')=='on':
            preview=self.DESKTOP/'.local/share/rm26-sim/lockstep-preview'/self.request['job_id']/str(self.slot)
            (preview/'ended.json').unlink(missing_ok=True)
            script=self.DESKTOP/'.local/share/rm26-sim/sync_view.py'
            script.unlink(missing_ok=True)
            shutil.copy2(self.source_ops/'sync_view.py',script)
            directory='/home/openvino/.local/share/rm26-sim/lockstep-preview/'+self.request['job_id']+'/'+str(self.slot)
            self.run(['docker','exec','--detach','--user','1000:1000','--env','DISPLAY=:20',
                '--env','XAUTHORITY=/run/user/1000/Xauthority',manage.DESKTOP_CONTAINER,
                'python3','/home/openvino/.local/share/rm26-sim/sync_view.py',directory])
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                status = self.rpc('status')
                ready=(status.get('synchronization') or {}).get('mode')=='hold' if self.lockstep else status.get('frame_age_ms') is not None and status['frame_age_ms']<250
                if status.get('connected') and ready:
                    algorithm_now = self.inspect(self.DESKTOP_CONTAINER)
                    write(self.RUNTIME/'provisioning.json',dict(seconds=time.monotonic()-cold_start,reused=reuse,
                        algorithm_reused=algorithm_reused,algorithm_container_id=algorithm_now['Id'],
                        algorithm_started_at=algorithm_now['State']['StartedAt'],
                        algorithm_lifecycle=self.settings.get('algorithm_lifecycle','session')))
                    return
            except (OSError, RuntimeError):
                pass
            for name in (self.SIMULATOR,self.BRIDGE,self.DESKTOP_CONTAINER):
                info = self.inspect(name)
                if info and not info['State']['Running']:
                    raise RuntimeError('Worker component exited: '+name)
            time.sleep(.3)
        raise RuntimeError('Worker readiness exceeded 60 seconds')

    def on_run_created(self, folder):
        self.current_folder = folder
        self.basic_started=self.attempt_started if self.lockstep else time.monotonic()
        self.logs_since=time.time()
        write(Path(self.request['result']),{'run_id':folder.name,'state':'running'})
        self.sampler = threading.Thread(target=self.sample_resources,daemon=True)
        self.sampler.start()

    def sample_resources(self):
        with (self.current_folder/'resources.jsonl').open('a',buffering=1) as output:
            while not self.resources_stop.is_set():
                row = dict(time_ns=time.time_ns(),loadavg=os.getloadavg(),
                    memory_available_kib=next(int(l.split()[1]) for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')),
                    requested_parallelism=self.request['parallelism'],slot=self.slot,renderer=self.renderer['backend'],
                    disk_free_bytes=shutil.disk_usage(self.OPS).free)
                try:
                    if self.renderer['backend']=='nvidia':
                        stats = subprocess.run(['nvidia-smi','--query-gpu=utilization.gpu,memory.used,memory.free','--format=csv,noheader,nounits'],
                            capture_output=True,text=True,timeout=2)
                        row['gpu'] = stats.stdout.strip()
                        row['gpu_free_mib']=float(stats.stdout.strip().splitlines()[0].split(',')[-1])
                    containers = subprocess.run(['docker','ps','--filter','label=rm26.background=true','--filter','name=algorithm','--format','{{.Names}}'],
                        capture_output=True,text=True,timeout=2)
                    row['active_algorithm_containers'] = containers.stdout.splitlines()
                except (OSError,subprocess.TimeoutExpired,ValueError,IndexError):
                    row['sampling_timeout'] = True
                output.write(json.dumps(row)+'\n')
                self.resources_stop.wait(2)

    def snapshot(self, folder, scene, args):
        proxy = SimpleNamespace(OPS=self.source_ops,ROOT=self.bundle,RUNTIME=self.RUNTIME,
            inspect=self.inspect,profile=self.profile,run=self.run,
            renderer=self.renderer,
            SIMULATOR=self.SIMULATOR,BRIDGE=self.BRIDGE,
            FROZEN_REPOSITORIES=self.metadata['repositories'],PIPELINE_VERSION=self.PIPELINE_VERSION)
        # Existing scoring snapshot with the worker's calibration and immutable inputs.
        calibration = self.bundle/'sp_vision_25/build/sim/calibration.yaml'
        # Frozen bundle is never modified; copy calibration directly into evidence below.
        manifest = experiments.snapshot(proxy,folder,scene,args)
        actual = self.RUNTIME/'algorithm/calibration.yaml'
        if actual.exists():
            shutil.copy2(actual,folder/'configuration/runtime-calibration.yaml')
        shutil.copy2(self.compose_path,folder/'configuration/worker-compose.json')
        shutil.copy2(self.bundle/'bundle.json',folder/'configuration/bundle.json')
        manifest.update(execution_gpu_uuid=self.gpu_uuid,renderer=self.renderer,benchmark=self.metadata.get('benchmark'),algorithm_identity=self.metadata.get('algorithm'),environment_identity=self.metadata.get('environment'),ros_domain_id=self.domain,background={
            'job_id':self.request['job_id'],'item_id':self.request['item_id'],
            'attempt':self.request['attempt'],'slot':self.slot,'bridge_port':self.port,
            'headless':True,'requested_parallelism':self.request['parallelism'],
            'effective_parallelism':self.request.get('effective_parallelism',self.request['parallelism']),
            'exploratory':self.request['exploratory'],'bundle_id':self.metadata['bundle_id'],
            'provisioning':load(self.RUNTIME/'provisioning.json')})
        manifest['containers']['algorithm_image_id']=self.metadata['images']['algorithm']
        active=self.run(['docker','ps','--filter','label=rm26.background=true','--filter','name=algorithm','--format','{{.Names}}'],capture=True).splitlines()
        manifest['background']['active_algorithm_containers_at_prepare']=active
        manifest['configuration_sha256']={str(p.relative_to(folder/'configuration')):experiments.digest(p)
            for p in (folder/'configuration').rglob('*') if p.is_file()}
        write(folder/'manifest.json',manifest)
        return manifest


def main():
    p=argparse.ArgumentParser();p.add_argument('request',type=Path);args=p.parse_args()
    request=load(args.request);worker=Worker(request)
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    keep_algorithm = False
    try:
        # Verify the frozen payload before any component can execute it.
        if worker.metadata.get('benchmark_version'):
            from benchmark.artifacts import verify
            verify(worker.bundle)
        for name,digest in worker.metadata['files'].items():
            if experiments.digest(worker.bundle/name)!=digest:
                raise RuntimeError('Frozen bundle checksum mismatch: '+name)
        worker.start()
        options=argparse.Namespace(**request['settings'])
        folder=experiments.run_one(worker,options.scene,options)
        summary=load(folder/'summary.json',{})
        keep_algorithm = (summary.get('verdict','invalid') != 'invalid'
            and worker.settings.get('algorithm_lifecycle','session') == 'session')
        write(Path(request['result']),dict(run_id=folder.name,state='finished',verdict=summary.get('verdict','invalid')))
    except BaseException as error:
        write(Path(request['result']),dict(run_id=worker.current_folder.name if worker.current_folder else None,
            state='failed',verdict='invalid',error=type(error).__name__+': '+str(error)))
        raise
    finally:
        if worker.lockstep and worker.settings.get('preview')=='on':
            preview=worker.DESKTOP/'.local/share/rm26-sim/lockstep-preview'/worker.request['job_id']/str(worker.slot)
            if preview.exists():write(preview/'ended.json',{'ended_ns':time.time_ns()})
        worker.resources_stop.set()
        if hasattr(worker,'sampler'):
            worker.sampler.join(timeout=5)
        try:
            worker.rpc('control',value='off')
        except (OSError,RuntimeError):
            pass
        if worker.compose_path.exists() and not keep_algorithm:
            worker.compose('stop','--timeout','2','algorithm')
        if worker.current_folder:
            folder=worker.current_folder
            for name,label in ((worker.DESKTOP_CONTAINER,'algorithm'),(worker.SIMULATOR,'simulator'),(worker.BRIDGE,'bridge')):
                result=subprocess.run(['docker','logs','--timestamps','--since',str(worker.logs_since),name],capture_output=True,text=True)
                (folder/(label+'.log')).write_text(result.stdout+result.stderr)
            evidence=load(folder/'evidence-sha256.json',{})
            for name in ('resources.jsonl','algorithm.log','simulator.log','bridge.log'):
                if (folder/name).exists():evidence[name]=experiments.digest(folder/name)
            write(folder/'evidence-sha256.json',evidence)

            if worker.DESKTOP.exists():experiments.publish_desktop(worker,folder)
            if worker.PIPELINE_VERSION==2:
                from render_backend import wall_budget
                budget=wall_budget(worker.renderer,worker.lockstep)
                elapsed=time.monotonic()-worker.basic_started
                timing=load(folder/'timing.json',{})
                timing.update(wall_seconds=elapsed,within_budget=elapsed<=budget,
                    provisioning_seconds=load(worker.RUNTIME/'provisioning.json',{}).get('seconds'),
                    completion_boundary='basic_results_and_sealed_evidence')
                write(folder/'timing.json',timing)
                summary=load(folder/'summary.json',{})
                summary.setdefault('metrics',{})['total_wall_s']=elapsed
                for rule in summary.get('rules',[]):
                    if rule['name']=='wall_time_budget':rule.update(passed=elapsed<=budget,actual=elapsed)
                if elapsed>budget:summary.update(verdict='invalid',automatic_verdict='invalid')
                write(folder/'summary.json',summary)
                from report import render
                render(folder,summary)
                # Refresh only changed small files after the evidence was exposed.
                for name in ('timing.json','summary.json','report.html','report.md'):
                    source=folder/name;target=worker.DESKTOP/'.local/share/rm26-sim/experiments'/folder.name/name

                    if target.parent.exists():
                        target.unlink(missing_ok=True);os.link(source,target)
                result=load(Path(request['result']),{})
                if elapsed>budget:result['verdict']='invalid'
                write(Path(request['result']),result)


if __name__=='__main__':
    main()
