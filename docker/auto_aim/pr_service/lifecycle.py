"""Only live work is persisted. Terminal requests leave bounded text logs only."""
import json
import shutil
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Lifecycle:
    root: Path
    logs: Path
    resources: object
    max_log_bytes: int=2*1024*1024

    def workspace(self,key):
        if len(key)!=24 or any(c not in '0123456789abcdef' for c in key):raise ValueError('Invalid task key')
        return Path(self.root).resolve()/key

    def log(self,key,message):
        self.workspace(key)
        self.logs.mkdir(parents=True,exist_ok=True)
        path=self.logs/(key+'.log')
        room=self.max_log_bytes-(path.stat().st_size if path.exists() else 0)
        if room>0:
            with path.open('ab') as f:f.write((message+'\n').encode()[:room])

    def run(self,request,execute,publish,valid=lambda:True):
        key=request.key();work=self.workspace(key)
        if (self.logs/(key+'.log')).exists():return 'already-attempted'
        work.mkdir(parents=True,exist_ok=False)
        result=None
        try:
            self.log(key,'request '+json.dumps(request.data()))
            if not valid():raise ValueError('Approval no longer valid')
            result=execute(request,work,valid)
            publish(request,result)
            self.log(key,'finished; score delivered')
            return 'completed'
        except Exception as error:
            self.log(key,'failed '+type(error).__name__+': '+str(error))
            try:publish(request,dict(base=request.base,head=request.head,valid=False,scores=[],message=type(error).__name__+': '+str(error),log='Experiment failed; see service text log.'))
            except Exception as publish_error:self.log(key,'failure report unavailable '+str(publish_error))
            return 'failed'
        finally:
            # Never keep results for upload retry; cleanup must also run on interruption.
            try:
                execution_log=work/'execution.log'
                if execution_log.exists():
                    with execution_log.open('rb') as stream:
                        self.log(key,stream.read(self.max_log_bytes).decode(errors='replace'))
            finally:
                try:
                    self.resources.remove(key)
                finally:
                    if work.exists():shutil.rmtree(work)
                    self.log(key,'workspace removed')

    def recover(self):
        self.root.mkdir(parents=True,exist_ok=True)
        # Logs are the only terminal record; retry removal of any leaked labelled resources.
        if self.logs.exists():
            for log in self.logs.glob('*.log'):
                try:self.resources.remove(log.stem)
                except Exception as error:self.log(log.stem,'startup resource cleanup failed '+str(error))
        for work in self.root.iterdir():
            if not work.is_dir():continue
            self.workspace(work.name)
            try:self.resources.remove(work.name)
            finally:
                shutil.rmtree(work)
                self.log(work.name,'interrupted; startup cleanup complete; no automatic rerun')
