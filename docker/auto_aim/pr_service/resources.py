"""Remove only explicitly owned remote resources. Never use system prune."""
import json
import subprocess


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
