import json,math,bisect
from pathlib import Path
import numpy as np

root=Path('/project');out=root/'exports/a1-root-cause-20261101'
source=root/'docker/auto_aim/experiments/20261101T123108_basic_move3_spin_pos_shoot_muzzle_no-drag_25p0_25p0_feedforward_5p0hz_tracking_fire_v3_14a332f5'
read=lambda p:[json.loads(l) for l in p.read_text().splitlines()]
packets=read(source/'telemetry.jsonl');truth=[r['data'] for r in packets if r['kind']=='armors'];truth.sort(key=lambda x:x['stamp_ns']);stamps=[r['stamp_ns'] for r in truth]
observations={r['data']['frame']['seq']:r['data'] for r in packets if r['kind']=='observation'}
baseline={r['seq']:r for r in read(out/'observations-baseline.jsonl')}
intervention={r['seq']:r for r in read(out/'observations-no-hint.jsonl')}
def rotation(q):
 x,y,z,w=np.asarray(q)/np.linalg.norm(q)
 return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
rows=[]
for seq in range(60,76):
 o=observations[seq];f=o['frame'];r=o['result'];c=r['continuity'];index=bisect.bisect_left(stamps,f['stamp_ns']);near=min(truth[max(0,index-1):index+1],key=lambda x:abs(x['stamp_ns']-f['stamp_ns']))
 gap=abs(near['stamp_ns']-f['stamp_ns'])/1e6;assert gap<25
 cam=f['camera_in_map'];R=rotation(cam['quaternion']);K=np.asarray(f['calibration']['k']).reshape(3,3);origin=np.asarray(f['gimbal_position_map'])
 gt=[]
 for plate in near['armors']:
  if plate['robot_id']!=1:continue
  pos=np.asarray(plate['position']);optical=(pos-np.asarray(cam['position']))@R
  if optical[2]<=0:continue
  uv=optical@K.T;uv=uv[:2]/uv[2]
  gt.append((plate,uv,pos-origin))
 for assoc in c.get('associations') or []:
  oi=assoc['observation'];pid=assoc['plate_id'];pose=c['diagnosis_poses'][oi];chosen=np.asarray(pose['xyz'])
  plate,uv,gtpos=min(gt,key=lambda x:np.linalg.norm(x[1]-pose['image_center']))
  filter=c['filter'];x=filter['state_before'];pl=filter['plates'][pid]
  if pl['initialized']:pred=np.asarray(pl['state'])[::2]
  else:
   angle=x[6]+pid*math.pi/2;radius=x[8]+(x[9] if pid%2 else 0)
   pred=np.array([x[0]-radius*math.cos(angle),x[2]-radius*math.sin(angle),x[4]+(x[10] if pid%2 else 0)])
  # Analyze only rejected frames: their plate filter received no accepted update.
  if assoc['reason']!='association_distance':continue
  assert not filter['updates']
  best=min(pose['candidates'],key=lambda v:v[1]);bestpos=np.array(best[3:6]);distance=float(np.linalg.norm(chosen-pred));assert distance>.8
  rows.append(dict(seq=seq,observation=oi,model_plate_id=pid,truth_armor_id=plate['id'],truth_alignment_ms=gap,image_match_distance_px=float(np.linalg.norm(uv-pose['image_center'])),predicted_xyz=pred.tolist(),chosen_xyz=chosen.tolist(),image_minimum_xyz=bestpos.tolist(),truth_xyz=gtpos.tolist(),chosen_distance_m=distance,image_minimum_distance_m=float(np.linalg.norm(bestpos-pred)),prediction_error_m=float(np.linalg.norm(pred-gtpos)),chosen_error_m=float(np.linalg.norm(chosen-gtpos)),image_minimum_error_m=float(np.linalg.norm(bestpos-gtpos)),hint=pose['supplied_hint'],chosen_yaw=pose['yaw'],image_minimum_yaw=best[0],chosen_cost=pose['chosen_cost'],best_cost=pose['best_cost'],pixel_rotation_active=c.get('pixel_rotation',{}).get('active')))
result=dict(source_run=source.name,rows=rows,baseline_functional_parity={k:sum(baseline[s].get(k)!=observations[s]['result'].get(k) for s in baseline) for k in ('tracker','filter_state','selected_plate_id')},intervention_window=[dict(seq=s,baseline=baseline[s]['tracker'],no_hint=intervention[s]['tracker'],baseline_reset=baseline[s]['continuity'].get('reset_reason'),no_hint_reset=intervention[s]['continuity'].get('reset_reason')) for s in range(65,77)],timeout=dict(seq=75,last_accepted_seq=67,elapsed_s=(observations[75]['frame']['stamp_ns']-observations[67]['frame']['stamp_ns'])/1e9,threshold_s=.25),limitations=['Truth is matched offline by projected armor centre using nearest telemetry stamp; no truth enters replay.','No-hint intervention applies to the complete prefix and is not an isolated one-frame change.','Image minimum counterfactual uses recorded candidates at the same state; it does not prove overall algorithm improvement.'])
(out/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
for r in rows:print(r['seq'],r['model_plate_id'],'dist',round(r['chosen_distance_m'],3),'bestdist',round(r['image_minimum_distance_m'],3),'errors(pred/chosen/best)',*[round(r[k],3) for k in ('prediction_error_m','chosen_error_m','image_minimum_error_m')],'pixelmatch',round(r['image_match_distance_px'],2))
print(result['timeout'])
