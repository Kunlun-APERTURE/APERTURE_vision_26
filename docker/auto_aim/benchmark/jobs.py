"""The queue mutation boundary shared by local and service callers."""
import json
import time


class JobService:
    def __init__(self,backend=None):
        if backend is None:import background_jobs as backend
        self.backend=backend

    def submit(self,identifier,bundle,rows,parallelism=1,probe=False):
        if not rows or parallelism not in (1,2):raise ValueError('Rows and supported parallelism required')
        b=self.backend
        with b.connect() as db,b.transaction(db):
            now=time.time();spec=dict(version=1,_protocol=2,_stop_on_invalid=True,benchmark=not probe,benchmark_probe=probe)
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'queued',now,now,json.dumps(spec),str(bundle),parallelism,int(probe and parallelism>1),None))
            db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',[(identifier,i,'pending',json.dumps(row)) for i,row in enumerate(rows)])
            b.event(db,identifier,'Submitted through JobService')
        return identifier

    def status(self,identifier):
        with self.backend.connect() as db:return self.backend.status(db,identifier)
