"""Meaningful temporal/scoring checks, plus source-fixture renderer validation."""
import argparse
import copy
from pathlib import Path
from review_video import frame_at,shot_snapshot,validate_pair,load,require

def checks(output=None):
    s=[dict(shot_seq=1,fire_time_s=.1,terminal_stamp_ns=400_000_000,useful_hit=True,weighted_hit_score=.8,impact_x_fraction=.2,impact_y_fraction=.1),
       dict(shot_seq=2,fire_time_s=.2,terminal_stamp_ns=None,useful_hit=False,weighted_hit_score=0,impact_x_fraction=None)]
    require(shot_snapshot([],0,0,1)['exact'] is None,'Zero shots not N/A')
    require(shot_snapshot(s,.15,0,1)['in_flight']==1,'Flight counted as miss')
    require(shot_snapshot(s,.3,0,1)['hits']==0,'Future hit leaked')
    require(shot_snapshot(s,.5,0,1)['hits']==1,'Collision missing')
    require(shot_snapshot(s,1.2,0,1,True)['misses']==1,'Unresolved miss not settled')
    require(shot_snapshot(s,1.2,0,1,True)['weighted']==.4,'Wrong weighted denominator')
    miss=dict(s[1],fire_time_s=.1)
    require(shot_snapshot([miss],2,0,1,True)['exact']==0,'All misses must score zero')
    late=dict(s[0],fire_time_s=.9,terminal_stamp_ns=1_200_000_000)
    require(shot_snapshot([late],1.1,0,1)['hits']==0 and shot_snapshot([late],1.3,0,1)['hits']==1,'Cross-boundary hit mistimed')
    require(frame_at([.05,.1,.3],0)==-1 and frame_at([.05,.1,.3],.2)==1,'Frame selection looks ahead')
    m=dict(scene='x',sensor_seed=1,duration_s=1,shooting=dict(physics='no-drag'),execution=dict(mode='realtime'))
    validate_pair(m,copy.deepcopy(m));b=copy.deepcopy(m);b['sensor_seed']=2
    try:validate_pair(m,b)
    except ValueError:pass
    else:raise AssertionError('Different seeds paired')
    import tempfile
    import review_video as host
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as temporary:
        base=Path(temporary);ops=base/'ops';queue=base/'queue';queue.mkdir();study=ops/'experiments'/'new-final';study.mkdir(parents=True)
        host.write(queue/'policy.json',dict(enabled_since=0))
        host.write(study/'state.json',dict(state='completed',independently_verified=True,runs=[],selected='R4'))
        host.write(study/'contract.json',dict(bundles={'baseline':'a','R4':'b'}))
        host.write(study/'report-seal.json',{})
        output_dir=study.with_name('new-final-visual-review')
        with patch.object(host,'OPS',ops),patch.object(host,'QUEUE',queue),patch.object(host.time,'time',return_value=10**12),patch.object(host,'prepare',return_value=output_dir) as prepare:
            host.auto_discover();host.auto_discover()
            require(prepare.call_count==0,'Completed experiment automatically prepared a video')
            requests=[host.load(x) for x in queue.glob('*.json') if x.name!='policy.json']
            require(not requests and not output_dir.exists(),'Unrequested video created artifacts or queue requests')
        with patch.object(host,'prepare',return_value=output_dir) as prepare,patch.object(host,'enqueue') as enqueue:
            host.export_completed(study,candidate='R4')
            prepare.assert_not_called();enqueue.assert_not_called()
            host.export_completed(study,candidate='R4',requested=True)
            prepare.assert_called_once_with(study,candidate='R4');enqueue.assert_called_once_with(output_dir)
        invalid=ops/'experiments'/'failed-screen';invalid.mkdir()
        host.write(invalid/'state.json',dict(state='completed',selected=None,runs=[]))
        host.write(invalid/'contract.json',dict(bundles={'baseline':'a','R4':'b'}));host.write(invalid/'report-seal.json',{})
        with patch.object(host,'OPS',ops),patch.object(host,'QUEUE',queue),patch.object(host.time,'time',return_value=10**12),patch.object(host,'prepare') as prepare:
            host.auto_discover();require(prepare.call_count==0,'Failed/unselected screen promoted to review')
    if output:
        import cv2
        import review_video_render as r
        from review_video import verify_inputs
        spec=load(output/'export.json');verify_inputs(spec);r.FONT=spec['font_path']
        seen=0
        for i,case in enumerate(spec['cases']):
            case=dict(case,engine=spec['engine']);left,right=[r.Evidence(side,output) for side in case['sides']]
            case['target_extent']=max([1.15]+[max(abs(s['impact_x_fraction']),abs(s['impact_y_fraction']))+.1 for ev in (left,right) for s in ev.shots if s.get('impact_x_fraction') is not None and s['useful_hit']])
            charts=r.Charts(left,right,max(ev.side['measurement_s'] for ev in (left,right)))
            for side,ev in zip(case['sides'],(left,right)):
                old=-1
                for stamp in (x/30 for x in range(int(case['play_s']*30)+1)):
                    snap=ev.snapshot(stamp)
                    require(snap['hits']<=snap['shots'] and snap['in_flight']+snap['misses']+snap['hits']==snap['shots'],'Counters inconsistent')
                    require(snap['shots']>=old,'Shot count decreases');old=snap['shots']
                require(ev.final['in_flight']==0,'Final unresolved shot')
                for shot in ev.shots:
                    t=r.shot_time(shot,side['start_ns'])
                    if shot['useful_hit'] and t is not None:
                        require(shot['shot_seq'] not in [x['shot_seq'] for x in ev.snapshot(t-1e-5)['impacts']],'Impact revealed early')
                seen+=len(ev.shots)
            frame=r.render_case_frame(case,left,right,charts,min(3.,case['play_s']))
            final=r.render_case_frame(case,left,right,charts,case['play_s'],True)
            require(frame.shape==(1440,2560,3),'Wrong canvas size')
            cv2.imwrite(str(output/f'preview-{i}.png'),frame);cv2.imwrite(str(output/f'preview-{i}-final.png'),final)
            left.close();right.close()
        for index in (0,1):cv2.imwrite(str(output/f'summary-{index}.png'),r.summary_card(spec,index))
        encoded=output/'codec-smoke.mp4';enc=r.Encoder(encoded)
        try:enc.push(frame,15);enc.finish()
        except BaseException:enc.abort();raise
        info=r.probe(encoded);require(info['streams'][0]['width']==2560 and info['streams'][0]['nb_frames']=='15','Codec smoke failed')
        print('All source pairs,',seen,'shot temporal checks, full/final panels and H.264 smoke passed')
    print('Temporal/empty/miss/settlement/causality and pairing checks passed')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path);a=p.parse_args();checks(a.output)
