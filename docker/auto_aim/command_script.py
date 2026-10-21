"""Fixed control playback for renderer diagnostics; never supplies target state."""
import bisect,json,math
from pathlib import Path

class CommandScript:
    def __init__(self,path):
        data=json.loads(Path(path).read_text());self.rows=data['commands'];self.fires=data['fire_times_s']
        self.times=[r['time_s'] for r in self.rows]
        if not self.rows or any(b<=a for a,b in zip(self.times,self.times[1:])):raise ValueError('Command times must increase')
        for row in self.rows:
            if not all(math.isfinite(row[k]) for k in ('time_s','yaw','pitch','distance')):raise ValueError('Nonfinite command')
        if any(not math.isfinite(t) or t<0 for t in self.fires) or any(b-a<.199 for a,b in zip(self.fires,self.fires[1:])):raise ValueError('Invalid script fire schedule')
        self.reset()
    def reset(self):self.next_fire=0;self.expired_fires=0
    def at(self,t):
        i=bisect.bisect_right(self.times,t)-1
        if i<0:return dict(valid=False,yaw=0.,pitch=0.,distance=-1.,fire_advice=False)
        row=dict(self.rows[min(i,len(self.rows)-1)])
        while self.next_fire<len(self.fires) and t-self.fires[self.next_fire]>.1:self.next_fire+=1;self.expired_fires+=1
        row['fire_advice']=self.next_fire<len(self.fires) and self.fires[self.next_fire]<=t
        return row
    def fired(self):self.next_fire+=1
