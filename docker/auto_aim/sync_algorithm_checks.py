#!/usr/bin/env python3
"""Compare original/current adapters on the same archived original JPEGs."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from report import load, write
from sync_timing import digest


def check(study):
    state = load(study/'study.json')
    old = Path(state['original_ui']['bundle'])
    new = Path(state['bundle'])
    image = load(new/'bundle.json')['images']['algorithm']
    root = study.parent
    output = study/'algorithm-compatibility'
    output.mkdir(exist_ok=True)
    results = []
    for scene in ('hf_static_8', 'hf_turn90_3'):
        entry = next(r for r in state['reference'] if r['scene'] == scene)
        source = root/entry['run_id']
        if load(source/'evidence-policy.json', {}).get('level') == 'sampled':
            raise ValueError('Complete original JPEG evidence is required')
        stamps = {json.loads(line)['stamp_ns'] for line in (source/'frames.jsonl').read_text().splitlines()}
        observations = output/(scene+'-input.jsonl')
        count = 0
        with observations.open('w') as target, (source/'telemetry.jsonl').open() as stream:
            for line in stream:
                packet = json.loads(line)
                if packet['kind'] == 'observation' and packet['data']['frame']['stamp_ns'] in stamps:
                    target.write(line)
                    count += 1
        rows = {}
        for name, bundle in (('original', old), ('current', new)):
            work = output/(scene+'-'+name)
            work.mkdir(exist_ok=True)
            for directory in ('assets', 'configs'):
                link = work/directory
                if not link.is_symlink():
                    link.symlink_to('/reference/sp_vision_25/'+directory)
            command = ['docker', 'run', '--rm', '--network', 'none', '--cpus', '2',
                       '--user', f'{os.getuid()}:{os.getgid()}',
                       '-e', 'OPENBLAS_NUM_THREADS=1', '-e', 'OMP_NUM_THREADS=1',
                       '-v', str(old)+':/reference:ro', '-v', str(bundle)+':/candidate:ro',
                       '-v', str(root)+':/experiments:ro', '-v', str(output)+':/output',
                       '-w', '/output/'+work.name,
                       '--entrypoint', '/candidate/sp_vision_25/build/simulator/sim_auto_aim', image,
                       '--config=/reference/sp_vision_25/configs/simulator.yaml',
                       '--algorithm-profile=tracking_fire_v3', '--view=false', '--fire=off',
                       '--aim-origin=muzzle', '--replay=/output/'+observations.name,
                       '--raw-replay=/experiments/'+source.name,
                       '--replay-output=/output/'+scene+'-'+name+'.jsonl',
                       '--runtime=/output/'+work.name+'/runtime.yaml']
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
            (output/(scene+'-'+name+'.log')).write_text(completed.stdout+completed.stderr)
            completed.check_returncode()
            rows[name] = [json.loads(line) for line in (output/(scene+'-'+name+'.jsonl')).read_text().splitlines()]
        differences = sum(a != b for a, b in zip(rows['original'], rows['current']))
        passed = (count > 0 and differences == 0 and
                  all(len(values) == count and all(r.get('raw_replayed') for r in values) for values in rows.values()))
        results.append(dict(scene=scene, source_run=source.name, input_frames=count,
                            exact_different_frames=differences, passed=passed,
                            original_jpeg_sha256=digest(source/'input.mjpg'),
                            observation_sha256=digest(observations)))
        print(json.dumps(results[-1]), flush=True)
    result = dict(passed=all(r['passed'] for r in results), results=results,
                  original_binary_sha256=digest(old/'sp_vision_25/build/simulator/sim_auto_aim'),
                  current_binary_sha256=digest(new/'sp_vision_25/build/simulator/sim_auto_aim'),
                  limitation='Same-image offline replay checks numerical compatibility; live clock adaptation and closed-loop UI equivalence are evaluated separately.')
    write(study/'algorithm-compatibility.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('study', type=Path)
    args = parser.parse_args()
    if not check(args.study.resolve())['passed']:
        raise SystemExit(1)
