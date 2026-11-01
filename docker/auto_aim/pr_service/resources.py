"""Remove only explicitly owned remote resources. Never use system prune."""
import json
import subprocess
from dependency_image import unified_image


class DockerResources:
    def __init__(self,run=subprocess.run):self.run=run

    def remove(self,key):
        label='aperture.pr='+key
        for kind,query,remove in [('container',['ps','-aq'],['rm','-f']),('volume',['volume','ls','-q'],['volume','rm']),('network',['network','ls','-q'],['network','rm']),('image',['image','ls','-q'],['image','rm','-f'])]:
            p=self.run(['docker',*query,'--filter','label='+label],capture_output=True,text=True,check=True)
            ids=list(dict.fromkeys(p.stdout.split()))
            if ids:self.run(['docker',*remove,*ids],check=True,capture_output=True,text=True)

        # Frozen tags point at shared images; remove the task tags, never the digest.
        tags=self.run(['docker','image','ls','--format','{{.Repository}}:{{.Tag}}'],capture_output=True,text=True,check=True).stdout.split()
        owned=[t for t in tags if t.startswith('daedalus:rm26-frozen-') and t.endswith('-pr_'+key)]
        if owned:self.run(['docker','image','rm',*owned],check=True,capture_output=True,text=True)

    def purge_workspace(self,work):
        from pathlib import Path
        work=Path(work).resolve()
        if len(work.name)!=24 or any(c not in '0123456789abcdef' for c in work.name):raise ValueError('Not a task directory')
        script="""import os,pathlib,shutil
root=pathlib.Path('/task')
for directory,dirs,files in os.walk(root,followlinks=False):os.chmod(directory,0o700)
for path in root.iterdir():
 if path.is_symlink() or path.is_file():path.unlink()
 else:shutil.rmtree(path)
"""
        self.run(['docker','run','--rm','--network','none','--user','0:0','--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--cap-add','FOWNER',
            '--security-opt','no-new-privileges','--label','aperture.pr='+work.name,'-v',str(work)+':/task',
            '--entrypoint','python3',unified_image() or 'daedalus:rm26-algorithm','-c',script],check=True,capture_output=True,text=True)
