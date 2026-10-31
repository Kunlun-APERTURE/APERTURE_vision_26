"""Locate the first behavioral divergence without relaxing comparison rules."""
import json
from pathlib import Path

IGNORED={'session','reset_id','sensor_reset_id','receipt_ns','pipeline_ms','evaluation_wall_ns'}


def normalize(value):
    if isinstance(value,dict):return {k:normalize(v) for k,v in value.items() if k not in IGNORED}
    if isinstance(value,list):return [normalize(v) for v in value]
    return value


def events(folder):
    with (Path(folder)/'session-evidence/events.jsonl').open() as stream:
        for line in stream:
            row=json.loads(line)
            if row['kind']=='input':yield dict(kind='input',jpeg=row['jpeg_sha256'],data=normalize(row['data']))
            elif row['kind']=='result':yield dict(kind='result',data=normalize(row['data']['result']))


def first_difference(left,right):
    import itertools
    for i,(a,b) in enumerate(itertools.zip_longest(list(events(left)),list(events(right)))):
        if a!=b:
            def changed(x,y,prefix=''):
                if isinstance(x,dict) and isinstance(y,dict):
                    return sum((changed(x.get(k),y.get(k),prefix+'.'+k) for k in sorted(set(x)|set(y))),[])
                return [] if x==y else [dict(field=prefix,left=x,right=y)]
            return dict(event_index=i,kind=(a or b)['kind'],changes=changed(a,b))
    return None


def relative_events(folder):
    """Diagnostic only: explicitly remove one global session epoch offset."""
    from report import load
    end=load(Path(folder)/'run-state.json')['measurement_end_stamp_ns']
    source=[r for r in events(folder) if r['data'].get('stamp_ns',end)<=end]
    if not source:return source
    origin=source[0]['data']['stamp_ns'];step=source[0]['data'].get('step_id',0)
    def adjust(v,key=''):
        if isinstance(v,dict):return {k:adjust(x,k) for k,x in v.items()}
        if isinstance(v,list):return [adjust(x,key) for x in v]
        if key=='last_good_age_s' and isinstance(v,(int,float)) and v>1e9:return 'uninitialized_clock_origin'
        if key=='step_id' and isinstance(v,int):return v-step
        if key.endswith('_ns') and isinstance(v,int) and v>=1_000_000_000_000_000_000:return v-origin
        return v
    return adjust(source)


def relative_shots(folder):
    from report import load
    folder=Path(folder);origin=load(folder/'run-state.json')['measurement_start_stamp_ns']
    rows=[json.loads(l) for l in (folder/'shots.jsonl').read_text().splitlines()]
    for row in rows:
        for key in ('fire_stamp_ns','terminal_stamp_ns'):
            if row.get(key) is not None:row[key]-=origin
        # The native flight-time diagnostic uses a separate simulator time basis.
        # Preserve its original value in source evidence; recompute for this diagnostic.
        if row.get('terminal_stamp_ns') is not None:row['flight_time_s']=(row['terminal_stamp_ns']-row['fire_stamp_ns'])/1e9
    return rows
