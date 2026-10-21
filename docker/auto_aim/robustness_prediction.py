"""Independent future physical-plate scoring, with explicit matching coverage."""
import collections,json,math
from continuity_replay import Interpolate,q


def evaluate(folder,observations,context,output=None):
    plates=collections.defaultdict(list);ego=[]
    for line in (folder/'telemetry.jsonl').open():
        packet=json.loads(line);data=packet['data']
        if packet['kind']=='armors':
            for plate in data['armors']:
                if plate['robot_id']==1:plates[plate['id']].append((data['stamp_ns'],plate['position']))
        elif packet['kind']=='robots':
            own=next((r for r in data['robots'] if r['id']==0),None)
            if own:ego.append((data['stamp_ns'],own['position']))
    history={identity:Interpolate(rows) for identity,rows in plates.items()};own_history=Interpolate(ego)
    rows=[];position=[];lateral=[];radial=[];bearing=[];valid=0
    for observation in observations:
        frame=observation['frame'];result=observation['result'];stamp=frame['stamp_ns']
        if not context['start']<=stamp<=context['end']:continue
        track=result.get('track_estimate') or {};model=track.get('plates',[]);selected=result.get('selected_plate_id',-1)
        aim=result.get('aim_point');horizon=result.get('prediction_horizon_s')
        if not (0<=selected<len(model) and aim is not None and horizon is not None and 0<horizon<=1):continue
        valid+=1;origin=frame['gimbal_position_map'];world=[x+y for x,y in zip(model[selected],origin)]
        current={identity:h.at(stamp) for identity,h in history.items()};nearest=sorted((math.dist(world,p),identity) for identity,p in current.items() if p is not None)
        row=dict(seq=frame['seq'],stamp_ns=stamp,time_s=(stamp-context['start'])*1e-9,selected_model_plate=selected,matched=False,horizon_s=horizon)
        if not nearest or nearest[0][0]>=.25 or len(nearest)>1 and nearest[1][0]-nearest[0][0]<=.03:rows.append(row);continue
        identity=nearest[0][1];future_stamp=stamp+round(horizon*1e9);future=history[identity].at(future_stamp);now_ego=own_history.at(stamp);future_ego=own_history.at(future_stamp)
        if future is None or now_ego is None or future_ego is None:rows.append(row);continue
        reference=[p-o-(b-a) for p,o,a,b in zip(future,origin,now_ego,future_ego)];delta=[x-y for x,y in zip(aim,reference)]
        length=math.hypot(*reference[:2])
        if length<=1e-6:rows.append(row);continue
        unit=[reference[0]/length,reference[1]/length];r=delta[0]*unit[0]+delta[1]*unit[1];t=-delta[0]*unit[1]+delta[1]*unit[0]
        angle=abs(math.degrees(math.remainder(math.atan2(aim[1],aim[0])-math.atan2(reference[1],reference[0]),2*math.pi)))
        error=math.sqrt(sum(v*v for v in delta));position.append(error);radial.append(abs(r));lateral.append(abs(t));bearing.append(angle)
        row.update(matched=True,physical_armor_id=identity,current_match_error_m=nearest[0][0],prediction_error_m=error,
                   radial_error_m=r,lateral_error_m=t,bearing_error_deg=angle,expected_relative_point=reference,predicted_relative_point=aim)
        rows.append(row)
    if output:
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return dict(valid_prediction_frames=valid,matched_prediction_frames=len(position),matching_coverage=len(position)/valid if valid else None,
      future_point_error_p95_m=q(position,.95),future_radial_error_p95_m=q(radial,.95),future_lateral_error_p95_m=q(lateral,.95),future_bearing_error_p95_deg=q(bearing,.95),
      limits='Unique physical plate match <0.25 m and margin >0.03 m. Future own-origin motion uses own body translation; changing gimbal mount offset is not reconstructed. No ballistic path or new hit is inferred.')
