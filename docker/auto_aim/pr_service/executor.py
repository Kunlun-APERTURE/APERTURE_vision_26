"""Trusted harness, candidate compilation in disposable containers."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from dependency_image import build_image


class Executor:
    def __init__(self,config,run=subprocess.run):self.config=config;self.run=run

    def command(self,argv,log,cwd=None,timeout=1200):
        with log.open('ab') as stream:
            if self.run is not subprocess.run:
                self.run(argv,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout,check=True)
                return
            process=subprocess.Popen(argv,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            started=time.monotonic()
            try:
                while process.poll() is None:
                    if not getattr(self,'valid',lambda:True)():raise RuntimeError('Approval revoked or PR changed')
                    if time.monotonic()-started>timeout:raise TimeoutError('Command exceeded budget')
                    time.sleep(1)
                if process.returncode:raise subprocess.CalledProcessError(process.returncode,argv)
            finally:
                if process.poll() is None:
                    import signal
                    os.killpg(process.pid,signal.SIGTERM)
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()

    def __call__(self,request,work,valid):
        self.valid=valid
        import manage
        lockpath=manage.OPS/'.runtime/pr-resource.lock';lockpath.parent.mkdir(parents=True,exist_ok=True)
        with lockpath.open('a') as lease:
            # Scheduler takes a shared lease during local experiments.
            while True:
                try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if not valid():raise ValueError('Cancelled while waiting for local experiments')
                    time.sleep(1)
            return self.execute(request,work,valid)

    def execute(self,request,work,valid):
        import manage
        start=time.monotonic();log=work/'execution.log';rows=[]
        # Only GitHub HTTPS repositories can be fetched; PR shell text is never executed on host.
        url='https://github.com/'+request.repository+'.git'
        checkout=work/'checkout'
        self.command(['git','clone','--no-checkout','--',url,str(checkout)],log)
        self.command(['git','fetch','origin',request.base,request.head],log,cwd=checkout)
        for variant in ('base','candidate'):
            if not valid():raise ValueError('Approval cancelled')
            source=work/variant
            self.command(['git','clone','--shared',str(checkout),str(source)],log)
            self.command(['git','checkout','--detach',request.base],log,cwd=source)
            if variant=='candidate':self.command(['git','-c','user.name=experiment','-c','user.email=experiment@localhost','merge','--no-commit','--no-ff',request.head],log,cwd=source)
            # Candidate changes to harness/scoring/simulator need platform regression, not algorithm scores.
            if variant=='candidate':
                diff=subprocess.check_output(['git','diff','--name-only',request.base],cwd=source,text=True).splitlines()
                if any(p!='sp_vision_25' and not p.startswith(('docs/','.github/','README')) for p in diff):raise ValueError('Platform change: algorithm scoring requires unchanged harness and simulator')
            for name in ('sp_vision_25','vision_simulator_26'):
                suburl=subprocess.check_output(['git','config','-f','.gitmodules','--get','submodule.'+name+'.url'],cwd=source,text=True).strip()
                if not suburl.startswith('https://github.com/') or any(c in suburl for c in ('\n','\r',' ')):raise ValueError('Unsupported submodule URL')
            self.command(['git','-c','protocol.file.allow=never','submodule','update','--init'],log,cwd=source)
            # Execute only installed harness. Candidate top-level Python/workflow code is not imported.
            shutil.rmtree(source/'docker',ignore_errors=True)
            shutil.copytree(manage.OPS,source/'docker/auto_aim',ignore=shutil.ignore_patterns('.runtime','experiments','logs','__pycache__'))
            output=source/'output';output.mkdir()
            build=['docker','run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges','--label','aperture.pr='+request.key(),
                '--user',f'{os.getuid()}:{os.getgid()}','-v',str(source/'sp_vision_25')+':/source:ro','-v',str(output)+':/work','--entrypoint','bash',self.config.get('build_image',build_image()),'-c',
                'set -e; source /opt/intel/openvino/setupvars.sh >/dev/null; cmake -S /source -B /work/build -DBUILD_SIMULATOR_ADAPTER=ON -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/lib/x86_64-linux-gnu/cmake/opencv4; cmake --build /work/build --target sim_auto_aim -j4']
            self.command(build,log,timeout=1200)
            script=Path(__file__).with_name('harness.py')
            self.command(['python3',str(script),str(source),str(output/'build/sim_auto_aim'),request.key(),json.dumps(request.choices),json.dumps(self.config.get('experiment_selection',{}))],log,timeout=max(1,int(3600-(time.monotonic()-start))))
            result=json.loads((source/'scores.json').read_text());rows.append(result)
        if not valid():raise ValueError('Approval changed during experiments')
        base={r['scene']:r for r in rows[0]['groups']};candidate={r['scene']:r for r in rows[1]['groups']}
        return dict(base=request.base,head=request.head,valid=all(r['valid'] for r in rows),scores=[dict(scene=s,base=base[s],candidate=candidate[s]) for s in base],log=log.read_text(errors='replace')[-20000:])
