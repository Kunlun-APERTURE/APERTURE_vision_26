"""Run actual Coordinator command queue with a deterministic acknowledged step fixture."""
import json,queue,tempfile,threading
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch
from sync_bridge import Coordinator
from sync_timing import Schedule

def exercise(profile):
    applied=[];frames=[];stop=threading.Event()
    class Driver(Coordinator):
        def tick(self,capture=False,command=None,eligibility=None):
            if command is not None:applied.append(dict(stamp_ns=self.now_ns,command=command))
            self.now_ns+=self.schedule.control_delta();self.steps+=1
            if capture:
                self.image_index+=1;h=dict(session='s',seq=self.image_index,stamp_ns=self.now_ns)
                i=self.image_index-1;delay,processing=self.schedule.delays(i)
                self.captured.append((self.now_ns+delay,processing,self.schedule.return_delay(i),self.schedule.pose_delay(i),h,b'jpeg'))
            return {}
        def own_pose(self,delay_ns=0):return dict(stamp_ns=self.now_ns-delay_ns,yaw=0,elevation=0)
    with tempfile.TemporaryDirectory() as root:
        p=Path(root);(p/'step.sock').touch();(p/'timing.json').write_text(json.dumps(dict(profile,warmup_frames=1)))
        with patch.dict('sys.modules',{'rm_interfaces.msg':N(GimbalCmd=lambda:N())}):
            d=Driver(N(scenario={'spawn_applied':True},last_error='',archive=lambda *a:None,publisher=N(publish=lambda *a:None)),p/'step.sock',p/'timing.json','adv_variable_pos')
            def consumer():
                while not stop.is_set():
                    try:h,_=d.ready.get(timeout=.05)
                    except queue.Empty:continue
                    frames.append(h);d.submit(h,dict(yaw=0.1),dict(enabled=False))
            t=threading.Thread(target=consumer);t.start()
            try:
                assert d.phase_done.wait(2);d.warm('adv_variable_pos');assert len(frames)==1 and len(applied)==1
                assert applied[0]['stamp_ns']>=frames[0]['logical_receipt_ns']
                assert applied[0]['stamp_ns']-frames[0]['logical_receipt_ns']<=round(1e9/profile['control_hz'])+1
                return dict(frame=frames[0],applied=applied[0])
            finally:
                stop.set();d.stop.set()
                with d.condition:d.condition.notify_all()
                t.join(2);d.thread.join(2)
def run():
    profile=json.loads((Path(__file__).parent/'timing_profiles/ui-aligned-v7-clock.json').read_text())
    a=exercise(profile);b=exercise(dict(profile,return_delay_ns=profile.get('return_delay_ns',0)+20_000_000))
    for key in ('stamp_ns','logical_now_ns','latest_muzzle_pose'):assert a['frame'][key]==b['frame'][key]
    assert b['frame']['logical_receipt_ns']-a['frame']['logical_receipt_ns']==20_000_000
    delta=b['applied']['stamp_ns']-a['applied']['stamp_ns'];assert abs(delta-20_000_000)<=round(1e9/profile['control_hz'])+1
    return dict(passed=True,scope='Actual production Coordinator.run/submit, fake acknowledged physical ticks; no real simulator',nominal=a,extra20ms=b,applied_difference_ns=delta)
if __name__=='__main__':print(json.dumps(run(),indent=2))
