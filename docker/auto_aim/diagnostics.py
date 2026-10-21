#!/usr/bin/env python3
"""Offline shooting diagnosis. Reads frozen evidence; never controls the simulator.

Run in the evaluator image (numpy/matplotlib/OpenCV). Original run artifacts and
their verdicts are never rewritten. Positive lateral distances mean ahead in the
target's world XY movement direction, independently of camera screen direction.
"""
import argparse
import bisect
import collections
import csv
import hashlib
import html
import json
import math
from pathlib import Path
import subprocess

import numpy as np


def load(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2))


def jsonl(path):
    with path.open() as source:
        for line in source:
            yield json.loads(line)


def wrap(value):
    return math.remainder(value, math.tau)


def quantile(values, q=.5):
    values = sorted(float(v) for v in values if v is not None and math.isfinite(v))
    return values[min(len(values)-1, int((len(values)-1)*q))] if values else None


def table(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w') as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class Truth:
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda row: row['stamp_ns'])
        self.stamps = [row['stamp_ns'] for row in self.rows]

    def at(self, stamp, identity, key, tolerance_ns=100_000_000):
        i = bisect.bisect_left(self.stamps, stamp)
        if i < len(self.rows) and self.stamps[i] == stamp:
            return next((v for v in self.rows[i][key] if v['id'] == identity), None)
        if i == 0 or i == len(self.rows):
            return None
        a, b = self.rows[i-1], self.rows[i]
        if max(stamp-a['stamp_ns'], b['stamp_ns']-stamp) > tolerance_ns:
            return None
        left = next((v for v in a[key] if v['id'] == identity), None)
        right = next((v for v in b[key] if v['id'] == identity), None)
        if left is None or right is None:
            return None
        fraction = (stamp-a['stamp_ns'])/(b['stamp_ns']-a['stamp_ns'])
        result = dict(left)
        for field in ('position', 'velocity', 'normal'):
            if field in left:
                result[field] = ((1-fraction)*np.array(left[field])+fraction*np.array(right[field])).tolist()
        for field in ('yaw', 'yaw_rate'):
            if field in left:
                delta = right[field]-left[field]
                result[field] = left[field]+fraction*(wrap(delta) if field == 'yaw' else delta)
        return result


def fit_prediction(track, aim):
    """Recover plate identity/time from the published constant velocity model.

    Nearest *current* plate is insufficient: prediction may move near a different
    plate. Fit every plate after translation AND rotation. This is reconstruction,
    not an instrumented internal iteration time. Flat/ambiguous fits are flagged.
    """
    center = np.array(track['center'])
    if track.get('prediction_model') == 'plate_cv':
        identity=track['selected_plate_id']
        point=np.array(track['plates'][identity]);velocity=np.array(track['plate_velocities'][identity])
        shift=np.array(aim)-point
        speed2=float(velocity@velocity)
        horizon=float(shift@velocity/speed2) if speed2>1e-8 else 0
        error=float(np.linalg.norm(shift-velocity*horizon))
        ambiguous=speed2<=1e-8
        return {'fit_error_mm':error*1000,'selected_plate_index':identity,
            'prediction_time_estimate_s':None if ambiguous else horizon,
            'prediction_time_ambiguous':ambiguous,'prediction_fit_accepted':error<=.002 and 0<=horizon<=.5,
            '_shift':shift,'_linear':shift,'_rotation':np.zeros(3),'_plate_velocity':velocity}
    velocity = np.array(track['velocity'])
    plates = np.array(track['plates'])
    times = np.linspace(0, .5, 1001)
    phase = track['yaw_rate']*times
    c, s = np.cos(phase), np.sin(phase)
    candidates = []
    for identity, plate in enumerate(plates):
        delta = plate-center
        predicted = center+times[:, None]*velocity
        predicted[:, 0] += c*delta[0]-s*delta[1]
        predicted[:, 1] += s*delta[0]+c*delta[1]
        predicted[:, 2] += delta[2]
        errors = np.linalg.norm(predicted-np.array(aim), axis=1)
        index = int(errors.argmin())
        candidates.append((float(errors[index]), identity, index, predicted, errors))
    error, identity, index, predicted, errors = min(candidates, key=lambda row: row[0])
    near = times[errors <= error+.0005]
    ambiguous = float(near[-1]-near[0]) > .05
    # Local derivatives give the chosen plate velocity, including rotation.
    delta = predicted[index]-center-velocity*times[index]
    plate_velocity = velocity+np.array([-track['yaw_rate']*delta[1], track['yaw_rate']*delta[0], 0])
    shift = np.array(aim)-plates[identity]
    linear = velocity*times[index]
    return {'fit_error_mm': error*1000, 'selected_plate_index': identity,
        'prediction_time_estimate_s': None if ambiguous else float(times[index]),
        'prediction_time_ambiguous': ambiguous,
        'prediction_fit_accepted': error <= .002 and index not in (0, len(times)-1),
        '_shift': shift, '_linear': linear, '_rotation': shift-linear,
        '_plate_velocity': plate_velocity}


def phase(scene, t):
    if scene in ('ego_sweep','ego_sweep_reverse'):
        if 2 <= t < 14 and 1 <= (t-2)%4 <= 3.5: return 'steady'
        if t < 2: return 'initial_static'
        if t >= 14.5: return 'final_static'
        return 'transition'
    if scene in ('ego_left','ego_right','ego_right_centered','ego_sweep','ego_sweep_reverse'):
        if 3 <= t <= 5.5: return 'steady'
        if t < 2: return 'initial_static'
        if t >= 6.5: return 'final_static'
        return 'transition'
    if scene != 'line':
        return scene
    if t < 1:
        return 'startup_0_1s'
    if t >= 4 and t % 4 < 1:
        return 'reversal_0_1s'
    return 'steady'


def summarize(rows):
    result = {'samples': len(rows)}
    for field in ('estimated_yaw_rate', 'yaw_rate_error', 'prediction_shift_cm',
        'prediction_linear_cm', 'prediction_rotation_cm', 'command_lead_current_cm',
        'muzzle_lead_current_cm', 'command_ahead_of_muzzle_deg', 'image_age_ms',
        'command_age_ms', 'prediction_time_estimate_s', 'fit_error_mm',
        'front_plane_error_cm', 'front_surface_error_cm', 'signed_yaw_error_deg',
        'pitch_error_at_fire_deg', 'fire_pulse_to_launch_ms',
        'selected_point_front_error_capture_cm', 'future_front_error_cm', 'prediction_motion_error_cm'):
        values = [row.get(field) for row in rows]
        result[field+'_median'] = quantile(values)
        result[field+'_abs_median'] = quantile([abs(v) for v in values if v is not None])
        result[field+'_abs_p95'] = quantile([abs(v) for v in values if v is not None], .95)
    for field in ('wrong_center_velocity', 'negative_prediction', 'temp_lost', 'front_hit',
                  'target_hit', 'command_ahead_of_muzzle', 'muzzle_behind_current',
                  'large_rotation_error', 'negative_radius'):
        available = [row[field] for row in rows if row.get(field) is not None]
        result[field+'_count'] = sum(available)
        result[field+'_denominator'] = len(available)
    return result


def analyze(folder, output):
    manifest, state, summary = (load(folder/name) for name in ('manifest.json', 'run-state.json', 'summary.json'))
    start, end = state['measurement_start_stamp_ns'], state['measurement_end_stamp_ns']
    moving_ego = manifest['scene'] in ('ego_left','ego_right','ego_right_centered','ego_sweep','ego_sweep_reverse')
    robots, armors, observations, commands = [], [], [], []
    traces = collections.defaultdict(list)
    for packet in jsonl(folder/'telemetry.jsonl'):
        kind, data = packet['kind'], packet['data']
        if kind == 'robots': robots.append(data)
        elif kind == 'armors': armors.append(data)
        elif kind == 'observation': observations.append(data)
        elif kind == 'command': commands.append(data)
        elif kind == 'projectile_trace':
            for value in data['projectiles']:
                traces[value['shot_seq']].append(dict(value, stamp_ns=data['stamp_ns']))
    robots, armors = Truth(robots), Truth(armors)
    measured = {row['stamp_ns']: row for row in jsonl(folder/'frames.jsonl') if row.get('measurement')}
    first_armors = armors.rows[0]['armors']
    front_id = next(v['id'] for v in first_armors if v['robot_id'] == 1 and v['slot'] == 0)
    frame_rows, by_sequence = [], {}
    valid_obs = []
    previous = None
    last_lost_stamp = None
    for observation in sorted(observations, key=lambda row: row['frame']['stamp_ns']):
        frame, result = observation['frame'], observation['result']
        stamp = frame['stamp_ns']
        track = result.get('track_estimate')
        if result.get('tracker') == 'lost':
            last_lost_stamp = stamp
        if observation.get('command_emitted') and result.get('valid') and track and result.get('aim_point'):
            valid_obs.append(observation)
        if stamp not in measured or not track or not result.get('valid'):
            continue
        truth = robots.at(stamp, 1, 'robots')
        front = armors.at(stamp, front_id, 'armors')
        if truth is None or front is None:
            continue
        velocity = np.array(truth['velocity'])
        ego_truth = robots.at(stamp, 0, 'robots')
        own_velocity = np.array(ego_truth['velocity']) if ego_truth else np.zeros(3)
        if moving_ego:
            if ego_truth is None: continue
            velocity = velocity-own_velocity
        speed = float(np.linalg.norm(velocity[:2]))
        direction = velocity[:2]/speed if speed > .3 else None
        fit = fit_prediction(track, result['aim_point'])
        qx, qy, qz, qw = frame['q_gimbal2world']
        actual_yaw = math.atan2(2*(qw*qz+qx*qy), 1-2*(qy*qy+qz*qz))
        error = wrap(result['yaw']-actual_yaw)*180/math.pi
        row = {'run_id': folder.name, 'scene': manifest['scene'], 'physics': manifest['shooting']['physics'],
            'seq': frame['seq'], 'stamp_ns': stamp, 'time_s': (stamp-start)/1e9,
            'phase': phase(manifest['scene'], (stamp-start)/1e9),
            'tracker': result['tracker'], 'temp_lost': result['tracker'] == 'temp_lost',
            'seconds_since_last_lost': (stamp-last_lost_stamp)/1e9 if last_lost_stamp else None,
            'command_emitted': observation['command_emitted'],
            'ground_truth_vy': float(velocity[1]), 'estimated_vy': track['velocity'][1],
            'ego_vy':float(own_velocity[1]),'velocity_reference':'relative_to_translating_ego_origin' if moving_ego else 'world',
            'estimated_yaw_rate': track['yaw_rate'], 'ground_truth_yaw_rate': truth['yaw_rate'],
            'yaw_rate_error': track['yaw_rate']-truth['yaw_rate'],
            'large_rotation_error': abs(track['yaw_rate']-truth['yaw_rate']) > .5,
            'signed_yaw_error_deg': error,
            'command_yaw_rad': result['yaw'], 'actual_gimbal_yaw_rad': actual_yaw,
            'image_age_ms': (observation['receipt_ns']-stamp)/1e6,
            'wrong_center_velocity': bool(np.dot(np.array(track['velocity'])[:2], direction) <= 0) if direction is not None else None,
            'direction': ('plus_y' if velocity[1] > 0 else 'minus_y') if direction is not None else 'stationary',
            **{k: v for k, v in fit.items() if not k.startswith('_')},
            'correct_tracking': measured[stamp]['correct_tracking']}
        # Radius can be signed: a negative EKF radius reverses the plate phase.
        delta = np.array(track['plates'][0])-np.array(track['center'])
        row['estimated_radius_0_m'] = float(-delta[0]*math.cos(track['yaw'])-delta[1]*math.sin(track['yaw']))
        row['negative_radius'] = row['estimated_radius_0_m'] < 0
        if direction is not None and fit['prediction_fit_accepted']:
            for key, label in (('_shift', 'prediction_shift_cm'), ('_linear', 'prediction_linear_cm'), ('_rotation', 'prediction_rotation_cm')):
                row[label] = float(np.dot(fit[key][:2], direction)*100)
            row['negative_prediction'] = row['prediction_shift_cm'] < -.1
            horizon = fit['prediction_time_estimate_s']
            if (manifest['scene'] == 'line' or moving_ego) and horizon is not None:
                future_front = armors.at(stamp+round(horizon*1e9), front_id, 'armors')
                if future_front:
                    origin = np.array(frame['gimbal_position_map'])
                    current_point = np.array(track['plates'][fit['selected_plate_index']])+origin
                    aim_world = np.array(result['aim_point'])+origin
                    front_motion = np.array(future_front['position'])-np.array(front['position'])
                    if moving_ego:
                        aim_world = aim_world+own_velocity*horizon
                        front_motion = front_motion-own_velocity*horizon
                    row['selected_point_front_error_capture_cm'] = float(np.dot((current_point-np.array(front['position']))[:2], direction)*100)
                    row['future_front_error_cm'] = float(np.dot((aim_world-np.array(future_front['position']))[:2], direction)*100)
                    row['prediction_motion_error_cm'] = float(np.dot((fit['_shift']-front_motion)[:2], direction)*100)
        if previous is not None:
            gap = (stamp-previous['stamp_ns'])/1e9
            row['tracking_gap_s'] = gap
            row['plate_index_changed'] = row['selected_plate_index'] != previous['selected_plate_index']
            row['tracker_state_changed'] = row['tracker'] != previous['tracker']
            if gap > 0:
                row['command_yaw_rate_rad_s'] = wrap(row['command_yaw_rad']-previous['command_yaw_rad'])/gap
                row['actual_yaw_rate_rad_s'] = wrap(row['actual_gimbal_yaw_rad']-previous['actual_gimbal_yaw_rad'])/gap
        previous = row
        frame_rows.append(row)
        by_sequence[frame['seq']] = row
    valid_obs.sort(key=lambda row: row['receipt_ns'])
    receipts = [row['receipt_ns'] for row in valid_obs]
    pulses = sorted((row for row in observations if row.get('fire_forwarded')), key=lambda row: row['receipt_ns'])
    pulse_times = [row['receipt_ns'] for row in pulses]
    shot_rows = []
    for shot in jsonl(folder/'shots.jsonl'):
        stamp = shot['fire_stamp_ns']
        i = bisect.bisect_right(receipts, stamp)-1
        row = {'run_id': folder.name, 'scene': manifest['scene'], 'physics': manifest['shooting']['physics'],
            **shot, 'target_hit': shot['outcome'] == 'target_armor',
            'front_hit': (shot['outcome'] == 'target_armor' and shot['armor_id'] == front_id) if manifest['scene'] != 'spin' else None,
            'phase': phase(manifest['scene'], shot['fire_time_s'])}
        if i >= 0:
            observation = valid_obs[i]
            frame, result = observation['frame'], observation['result']
            diagnostic = by_sequence.get(frame['seq'])
            if diagnostic:
                row.update({k: v for k, v in diagnostic.items() if k not in ('time_s', 'phase', 'stamp_ns')})
            row['image_age_ms'] = (stamp-frame['stamp_ns'])/1e6
            row['command_age_ms'] = (stamp-observation['receipt_ns'])/1e6
            row['fire_advice_at_latest_image'] = result.get('fire_advice', False)
            velocity = shot.get('muzzle_relative_velocity',shot['initial_velocity'])
            row['pitch_error_at_fire_deg'] = (-result['pitch']-math.atan2(velocity[2], math.hypot(velocity[0], velocity[1])))*180/math.pi
            pulse = bisect.bisect_right(pulse_times, stamp)-1
            if pulse >= 0:
                row['fire_pulse_to_launch_ms'] = (stamp-pulse_times[pulse])/1e6
                row['fire_pulse_image_age_ms'] = (stamp-pulses[pulse]['frame']['stamp_ns'])/1e6
            truth = robots.at(stamp, 1, 'robots')
            front = armors.at(stamp, front_id, 'armors')
            ego_truth = robots.at(stamp,0,'robots')
            if truth and front and (manifest['scene'] == 'line' or moving_ego):
                vy = truth['velocity'][1]-(ego_truth['velocity'][1] if moving_ego and ego_truth else 0)
                if abs(vy) > .3:
                    sign = math.copysign(1, vy)
                    muzzle, velocity = shot['muzzle_position'], shot.get('muzzle_relative_velocity',shot['initial_velocity'])
                    dx = front['position'][0]-muzzle[0]
                    current_y = front['position'][1]
                    if abs(velocity[0]) > 1:
                        actual_y = muzzle[1]+velocity[1]/velocity[0]*dx
                        desired_y = muzzle[1]+math.tan(result['yaw'])*dx
                        row['command_lead_current_cm'] = (desired_y-current_y)*sign*100
                        row['muzzle_lead_current_cm'] = (actual_y-current_y)*sign*100
                        row['relative_forward_x_sign'] = math.copysign(1, dx)
                        row['command_ahead_of_muzzle_deg'] = wrap(result['yaw']-math.atan2(velocity[1], velocity[0]))*180/math.pi*sign*row['relative_forward_x_sign']
                        row['command_ahead_of_muzzle'] = row['command_ahead_of_muzzle_deg'] > 0
                        row['muzzle_behind_current'] = row['muzzle_lead_current_cm'] < 0
                        row['direction_at_fire'] = 'plus_y' if vy > 0 else 'minus_y'
                        if row['front_hit'] and shot.get('contact_point') and shot.get('terminal_stamp_ns'):
                            surface = armors.at(shot['terminal_stamp_ns'], front_id, 'armors')
                            if surface:
                                row['front_surface_error_cm'] = (shot['contact_point'][1]-surface['position'][1])*sign*100
                        # Observed sphere-centre crossing of front-plate plane.
                        # Timestamp is the published ROS frame stamp, not exact
                        # per-substep impact time; retain this alignment limit.
                        for point in traces[shot['shot_seq']]:
                            plate = armors.at(point['stamp_ns'], front_id, 'armors')
                            if plate is None: continue
                            normal, center = np.array(plate['normal']), np.array(plate['position'])
                            a, b = np.array(point['previous']), np.array(point['position'])
                            da, db = float(np.dot(a-center, normal)), float(np.dot(b-center, normal))
                            if da >= 0 and db <= 0 and da-db > 1e-9:
                                cross = a+(b-a)*da/(da-db)
                                row['front_plane_error_cm'] = float((cross[1]-center[1])*sign*100)
                                row['front_plane_stamp_ns'] = point['stamp_ns']
                                break
        # Keep the shot's phase/time even when the last image precedes reversal.
        row['phase'] = phase(manifest['scene'], shot['fire_time_s'])
        shot_rows.append(row)
    output.mkdir(parents=True, exist_ok=True)
    table(output/'frames.csv', frame_rows)
    table(output/'shots.csv', shot_rows)
    write(output/'shots.json', shot_rows)
    plot(output, frame_rows, shot_rows, manifest)
    clips = evidence_clips(folder, output, frame_rows, shot_rows)
    duration = manifest['duration_s']
    result = {'run_id': folder.name, 'scene': manifest['scene'], 'physics': manifest['shooting']['physics'],
        'gimbal_profile': manifest.get('gimbal_profile','legacy'),
        'verdict': summary['verdict'], 'duration_s': duration,
        'wall_seconds': load(folder/'timing.json')['wall_seconds'],
        'shots': len(shot_rows), 'target_hits': sum(row['target_hit'] for row in shot_rows),
        'front_hits': sum(row['front_hit'] for row in shot_rows if row['front_hit'] is not None) if manifest['scene'] != 'spin' else None,
        'firing_hz': len(shot_rows)/duration,
        'target_hits_per_s': sum(row['target_hit'] for row in shot_rows)/duration,
        'front_hits_per_s': sum(row['front_hit'] for row in shot_rows)/duration if manifest['scene'] != 'spin' else None,
        'clips': clips,
        'frames': summarize(frame_rows), 'at_fire': summarize(shot_rows),
        'phases': {key: summarize([row for row in shot_rows if row['phase'] == key]) for key in sorted({row['phase'] for row in shot_rows})},
        'directions': {key: summarize([row for row in shot_rows if row.get('direction_at_fire') == key]) for key in ('plus_y', 'minus_y')},
        'source_sha256': {name: hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in ('manifest.json', 'summary.json', 'telemetry.jsonl', 'shots.jsonl', 'timing.json')}}
    write(output/'summary.json', result)
    return result, frame_rows, shot_rows


def evidence_clips(folder, output, frames, shots):
    """Timestamp-preserving selections from the original annotated recording."""
    if not shots or not frames or not (folder/'annotated.avi').exists():
        return []
    candidates = []
    if shots[0]['scene'] in ('line','ego_left','ego_right','ego_right_centered','ego_sweep','ego_sweep_reverse'):
        front_hits = [row for row in shots if row.get('front_hit')]
        if front_hits:
            candidates.append(('front_hit_reference', front_hits[len(front_hits)//2]))
        for name, selected, field in (
            ('steady_lag', [r for r in shots if r['phase'] == 'steady'], 'command_ahead_of_muzzle_deg'),
            ('rotation_estimate', shots, 'estimated_yaw_rate'),
            ('reversal', [r for r in shots if r['phase'] == 'reversal_0_1s'], 'muzzle_lead_current_cm')):
            usable = [r for r in selected if r.get(field) is not None]
            if usable: candidates.append((name, max(usable, key=lambda r: abs(r[field]))))
        if shots[0]['scene'] in ('ego_left','ego_right','ego_right_centered','ego_sweep','ego_sweep_reverse'):
            for shot in [r for r in shots if not r.get('front_hit')][:2]:
                candidates.append(('missed_front', shot))
    else:
        candidates.append(('reference', shots[len(shots)//2]))
        for shot in [r for r in shots if r['outcome'] != 'target_armor'][:2]:
            candidates.append(('missed', shot))
    import cv2
    indices = list(jsonl(folder/'frames.jsonl'))
    stamps = [r['stamp_ns'] for r in indices]
    capture = cv2.VideoCapture(str(folder/'annotated.avi'))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30
    clips = []
    for name, shot in candidates:
        stamp = shot['fire_stamp_ns']
        first = max(0, bisect.bisect_left(stamps, stamp-800_000_000))
        last = min(len(indices)-1, bisect.bisect_right(stamps, stamp+1_200_000_000))
        if last <= first: continue
        video_first = indices[first]['video_index']
        video_last = indices[last]['video_index']
        filename = name+f"_shot_{shot['shot_seq']}.mp4"
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-ss', str(video_first/fps), '-i', str(folder/'annotated.avi'),
            '-t', str((video_last-video_first+1)/fps), '-an', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-crf', '24', '-pix_fmt', 'yuv420p',
            str(output/filename)], check=True)
        nearest = min(indices, key=lambda r: abs(r['stamp_ns']-stamp))
        capture.set(cv2.CAP_PROP_POS_FRAMES, nearest['video_index'])
        ok, image = capture.read()
        if ok:
            cv2.imwrite(str(output/(name+'.jpg')), image)
        clips.append({'name': name, 'file': filename, 'shot_seq': shot['shot_seq'],
            'fire_time_s': shot['fire_time_s'], 'first_stamp_ns': stamps[first],
            'last_stamp_ns': stamps[last], 'first_video_frame': video_first,
            'last_video_frame': video_last, 'source_fps': fps})
    capture.release()
    write(output/'clips.json', clips)
    return clips


def plot(output, frames, shots, manifest):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=True)
    for field, label in (('ground_truth_vy', 'Truth vy'), ('estimated_vy', 'Estimated vy')):
        axes[0].plot([r['time_s'] for r in frames], [r[field] for r in frames], label=label, linewidth=.8)
    for field, label in (('ground_truth_yaw_rate', 'Truth rotation'), ('estimated_yaw_rate', 'Estimated rotation')):
        axes[1].plot([r['time_s'] for r in frames], [r[field] for r in frames], label=label, linewidth=.8)
    for field, label in (('prediction_linear_cm', 'Translation'), ('prediction_rotation_cm', 'Rotation'), ('prediction_shift_cm', 'Net prediction')):
        points = [r for r in frames if r.get(field) is not None]
        axes[2].plot([r['time_s'] for r in points], [r[field] for r in points], label=label, linewidth=.8)
    for field, label in (('command_lead_current_cm', 'Command vs current plate'), ('muzzle_lead_current_cm', 'Muzzle vs current plate'), ('front_plane_error_cm', 'Observed front-plane crossing')):
        points = [r for r in shots if r.get(field) is not None]
        axes[3].plot([r['fire_time_s'] for r in points], [r[field] for r in points], '.-', label=label, linewidth=.7)
    for axis, units in zip(axes, ('m/s', 'rad/s', 'cm (+ ahead)', 'cm (+ ahead)')):
        axis.axhline(0, color='grey', linewidth=.5)
        axis.set_ylabel(units)
        axis.legend(loc='upper right', fontsize=8)
        axis.grid(alpha=.2)
        if manifest['scene'] == 'line':
            for t in (4, 8, 12, 16): axis.axvspan(t, t+1, color='orange', alpha=.12)
    axes[-1].set_xlabel('Scenario time (s); orange = first second after reversal')
    fig.suptitle(manifest['run_id'])
    fig.tight_layout()
    fig.savefig(output/'diagnosis.png', dpi=140)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--render-only', action='store_true', help='Rebuild aggregate reports from existing diagnostic artifacts')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runs, all_frames, all_shots = [], [], []
    excluded = {}
    seen = set()
    for batch in args.batch:
        record = load(batch)
        if not record['complete']:
            raise RuntimeError('Batch incomplete: '+str(batch))
        ancestor = record.get('resumed_from')
        visited = set()
        while ancestor and ancestor not in visited:
            visited.add(ancestor)
            prior = load(batch.parent/(ancestor+'.json'))
            for entry in prior['runs']:
                if entry['verdict'] == 'invalid':
                    invalid = load(batch.parent/entry['run_id']/'summary.json')
                    excluded[entry['run_id']] = [rule for rule in invalid['rules'] if not rule['passed']]
            ancestor = prior.get('resumed_from')
        for entry in record['runs']:
            if entry['run_id'] in seen: raise RuntimeError('Duplicate run')
            seen.add(entry['run_id'])
            folder = batch.parent/entry['run_id']
            if load(folder/'summary.json')['verdict'] == 'invalid':
                raise RuntimeError('Invalid evidence: '+str(folder))
            if args.render_only:
                cached = args.output/folder.name
                result, frames, shots = load(cached/'summary.json'), [], load(cached/'shots.json')
                for filename, expected in result['source_sha256'].items():
                    if hashlib.sha256((folder/filename).read_bytes()).hexdigest() != expected:
                        raise RuntimeError('Source changed after analysis: '+str(folder/filename))
            else:
                result, frames, shots = analyze(folder, args.output/folder.name)
            runs.append(result)
            all_frames.extend(frames)
            all_shots.extend(shots)
            print('Diagnosed', folder.name, result['shots'], flush=True)
    table(args.output/'runs.csv', [{k:v for k,v in r.items() if not isinstance(v, dict)} for r in runs])
    table(args.output/'all-shots.csv', all_shots)
    groups = load(args.output/'diagnosis.json')['groups'] if args.render_only else {}
    for scene in (() if args.render_only else ('static', 'line', 'spin')):
        for physics in ('no-drag', 'drag'):
            selected = [r for r in runs if r['scene'] == scene and r['physics'] == physics]
            shots = [r for r in all_shots if r['scene'] == scene and r['physics'] == physics]
            frames = [r for r in all_frames if r['scene'] == scene and r['physics'] == physics]
            n, hits = sum(r['shots'] for r in selected), sum(r['target_hits'] for r in selected)
            fronts = sum(r['front_hits'] for r in selected) if scene != 'spin' else None
            groups[scene+'/'+physics] = {'runs': len(selected), 'shots': n, 'target_hits': hits,
                'target_hit_rate': hits/n if n else None, 'front_hits': fronts,
                'front_hit_rate': fronts/n if fronts is not None and n else None,
                'firing_hz_median': quantile([r['firing_hz'] for r in selected]),
                'frames': summarize(frames), 'at_fire': summarize(shots),
                'phases': {key: summarize([r for r in shots if r['phase'] == key]) for key in sorted({r['phase'] for r in shots})},
                'directions': {key: summarize([r for r in shots if r.get('direction_at_fire') == key]) for key in ('plus_y', 'minus_y')}}
    invariants = {
        'algorithm_binaries': sorted({load(batch.parent/run['run_id']/'manifest.json')['algorithm_binary_sha256'] for batch in args.batch for run in load(batch)['runs']}),
        'simulator_images': sorted({load(batch.parent/run['run_id']/'manifest.json')['containers']['simulator_image_id'] for batch in args.batch for run in load(batch)['runs']}),
        'all_within_40_seconds': all(run['wall_seconds'] <= 40 for run in runs),
        'wall_min_s': min(run['wall_seconds'] for run in runs),
        'wall_max_s': max(run['wall_seconds'] for run in runs),
    }
    write(args.output/'diagnosis.json', {'runs': runs, 'groups': groups, 'invariants': invariants, 'excluded_runs': excluded,
        'method': 'offline constant-velocity/rotation reconstruction; no GT enters algorithm',
        'alignment_limits': 'GT interpolation brackets <=100 ms. Receipt time approximates publication, not simulator acceptance. Trace ROS stamps have render-frame resolution. Plane errors are reconstructed sphere-centre crossings, not additional hit events.'})
    markdown = ['# 射击诊断实验', '', '保持算法、模型、PID、开火门限及 v3 判定不变；所有结果等待人工视觉验收。', '',
        '| 场景/物理 | 重复 | 任意目标装甲 | 正面 slot 0 | 发射 Hz 中位数 |', '|---|---:|---:|---:|---:|']
    for key, group in groups.items():
        front = f"{group['front_hits']}/{group['shots']}" if group['front_hits'] is not None else '不适用'
        hz = group['firing_hz_median']
        markdown.append(f"| {key} | {group['runs']} | {group['target_hits']}/{group['shots']} | {front} | {hz:.2f} |" if hz is not None else f'| {key} | 无样本 | | | |')
    markdown += ['', f'本轮纳入 {len(runs)} 个有效实验；另保留 {len(excluded)} 个因既定有效性门槛失败的记录，按相同条件补采，未按命中率挑选结果。']
    for name, failures in excluded.items():
        markdown.append('- '+name+'：'+'；'.join(f"{r['name']} = {r['actual']}（要求 {r['expected']}）" for r in failures))
    markdown += ['', '## 平移诊断摘要', '',
        '| 物理 | 反向预测/可重建运动帧 | 估计旋转绝对值中位数 | 出膛指令超前枪口中位数 | 指令提前量中位数 | 枪口提前量中位数 |',
        '|---|---:|---:|---:|---:|---:|']
    for physics in ('no-drag', 'drag'):
        group = groups['line/'+physics]
        f, s = group['frames'], group['at_fire']
        def number(value): return f'{value:.3f}' if value is not None else '无样本'
        markdown.append(f"| {physics} | {f['negative_prediction_count']}/{f['negative_prediction_denominator']} | {number(f['estimated_yaw_rate_abs_median'])} rad/s | {number(s['command_ahead_of_muzzle_deg_median'])}° | {number(s['command_lead_current_cm_median'])} cm | {number(s['muzzle_lead_current_cm_median'])} cm |")
    line_shots = [row for row in all_shots if row['scene'] == 'line']
    line_stats = summarize(line_shots)
    markdown += ['', '## 原因排序与修复选项', '',
        f"1. **执行落后证据最强**：明显运动且可对齐的射击中，{line_stats['command_ahead_of_muzzle_count']}/{line_stats['command_ahead_of_muzzle_denominator']} 发的指令比实际枪口更靠运动前方，{line_stats['muzzle_behind_current_count']}/{line_stats['muzzle_behind_current_denominator']} 发的实际枪口仍落后目标当前位置。优先做固定频率控制与角速度前馈的独立对照；本轮没有改控制器，不能证明某一控制改动一定有效。",
        f"2. **预测存在局部异常，但并非统一反号**：可重建的运动射击图像中有 {line_stats['negative_prediction_count']}/{line_stats['negative_prediction_denominator']} 个净提前量反向样本。运动速度、选板、重建和虚假旋转需要一起检查，不应整体反转 yaw 或按左右方向加固定偏角。",
        '3. **开火门控容许未跟上的枪口出膛**：比较当前指令与实际出膛角度可直接观察该现象。建议在跟随修复对照之后，再单独比较门控收紧，持续检查发射频率与单位时间正面命中数。',
        '', '## 预测误差与时序边界', '',
        '| 物理 | 出膛对应图像的未来正面位置误差中位数 | 运动外推误差中位数 | 换向后第一秒枪口落后中位数 |',
        '|---|---:|---:|---:|']
    for physics in ('no-drag', 'drag'):
        group = groups['line/'+physics]
        s = group['at_fire']
        reversal = group['phases'].get('reversal_0_1s', {})
        markdown.append(f"| {physics} | {number(s['future_front_error_cm_median'])} cm | {number(s['prediction_motion_error_cm_median'])} cm | {number(reversal.get('muzzle_lead_current_cm_median'))} cm |")
    markdown += ['',
        '- 上表是不同诊断量的量级比较，不能把各自中位数直接相加作为因果分解。',
        '- 虚假旋转在静止样本中也存在，而静止正面命中仍正常。平移与旋转分量可能互相抵消；不能仅凭非零估计角速度决定禁用旋转模型。',
        '- 开火脉冲到出膛约 102 ms，但角度指令持续更新；不能直接把整段时间重复加入预测延迟。',
        '- 左右方向和换向分组的完整数值见 diagnosis.json；每次重复的范围见 runs.csv 与各次曲线。']
    markdown += ['', f"完整用时 {invariants['wall_min_s']:.2f}–{invariants['wall_max_s']:.2f} 秒；算法二进制种类 {len(invariants['algorithm_binaries'])}，模拟器镜像种类 {len(invariants['simulator_images'])}。", '',
        '## 诊断口径', '',
        '- 正方向为目标在世界坐标中的运动方向；静止不计算提前量。',
        '- 预测通过同一装甲板的平移和旋转重建，拟合残差超过 2 mm 或时间落在搜索边界时不计入方向统计；时间不确定时保留空值。',
        '- 图像时刻状态与出膛时刻真值分别统计；换向后的第一秒单列，不能混淆两个时刻的运动方向。',
        '- 平移场景针对预期正面 slot 0 分解当前选中点位置误差、未来位置误差及运动外推误差；这些量同时包含估计与选板影响，不能只看角速度就断言其导致脱靶。',
        '- 指令接收时延以桥接器发布时间近似，不代表模拟器内部精确接收时间。',
        '- 前装甲平面误差来自已记录球心轨迹及真值插值，具有渲染帧时间戳误差，不替代 v3 实际碰撞判定。正面命中时球心会在平面之前终止，因此平面穿越误差只统计确实穿过的弹丸；真实正面命中表面误差另列，不补造弹道。',
        '- 侧面和背面命中单独识别；原车身碰撞层仍有已知限制。',
        '- 相关性不代表因果；本轮没有控制器 A/B，不能声称 PID、渲染调度或传输分别贡献了多少误差。', '', '## 各次记录', '']
    for run in runs:
        markdown.append(f"- {run['run_id']}：{run['wall_seconds']:.2f} s，{run['target_hits']}/{run['shots']}；曲线与逐发 CSV 在同名子目录。")
    (args.output/'report.md').write_text('\n'.join(markdown)+'\n')
    body = '<pre>'+html.escape('\n'.join(markdown))+'</pre>'
    for run in runs:
        name = run['run_id']
        body += f'<h2>{html.escape(name)}</h2><p><a href="{name}/shots.csv">逐发诊断</a> · <a href="{name}/frames.csv">逐帧诊断</a> · <a href="../{name}/report.html">原始实验报告与录像</a></p><img src="{name}/diagnosis.png" style="max-width:100%">'
        for clip in run['clips']:
            body += f'<p>{html.escape(clip["name"])} · shot {clip["shot_seq"]} · t={clip["fire_time_s"]:.3f}s</p><video controls preload="none" width="720" src="{name}/{clip["file"]}"></video>'
    (args.output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>平移射击诊断</title><style>body{font:15px system-ui;max-width:1200px;margin:2em auto}pre{white-space:pre-wrap}</style>'+body)
    write(args.output/'analysis-source.json', {'diagnostics.py_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


if __name__ == '__main__':
    main()
