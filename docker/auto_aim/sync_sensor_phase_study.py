"""Bounded native-render grain phase intervention; diagnostic images only.

The shader phase is changed before sensor rendering and JPEG encoding. This is
not added noise over a compressed image. Runtime/algorithm/default tags stay put.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import background_jobs as jobs
import manage
from report import load, write
from sync_first_divergence import sha
from sync_study import DEFAULTS, ingest

CONDITIONS = (('p0g05', 0, .05, 2), ('p1024g05', 1024, .05, 1),
              ('p2048g05', 2048, .05, 1), ('p0g0', 0, 0., 1), ('p1024g0', 1024, 0., 1))
SCENES = ('hf_uniform_3', 'hf_turn90_3')


def replace(path, value):
    # Bundles share immutable payload through hard links. Unlink before edits.
    path.unlink(missing_ok=True)
    path.write_text(value)


def freeze(root, base, label, phase, grain):
    destination = jobs.STATE/'bundles'/('sensor_phase_20261023_'+label)
    if destination.exists():
        meta = load(destination/'bundle.json')
        assert meta['diagnostic']['phase_offset'] == phase and meta['diagnostic']['grain'] == grain
        return destination
    original = load(base/'bundle.json')
    build = root/'images'/label
    build.mkdir(parents=True, exist_ok=True)
    shader = (manage.ROOT/'vision_simulator_26/assets/shaders/camera_lens.wgsl').read_text()
    native = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none',
        '--entrypoint', 'cat', original['images']['simulator'], '/app/assets/shaders/camera_lens.wgsl']).decode()
    if shader != native:
        raise ValueError('Native image shader differs from source; freeze the native shader explicitly')
    if phase:
        old = 'let noise = hash21(in.uv * size + vec2<f32>(lens.seed, lens.seed * 1.7)) - 0.5;'
        assert shader.count(old) == 1
        shader = shader.replace(old,
            f'let grain_seed = fract((lens.seed + {phase}.0) / 4096.0) * 4096.0;\n'
            '        let noise = hash21(in.uv * size + vec2<f32>(grain_seed, grain_seed * 1.7)) - 0.5;')
        (build/'camera_lens.wgsl').write_text(shader)
        base_tag = original['retained_images']['simulator']
        if manage.inspect(base_tag)['Id'] != original['images']['simulator']:
            raise ValueError('Frozen simulator tag changed')
        (build/'Dockerfile').write_text('FROM '+base_tag+'\n'
            'COPY camera_lens.wgsl /app/assets/shaders/camera_lens.wgsl\n')
        tag = 'daedalus:rm26-grain-phase-20261023-'+label
        with (build/'build.log').open('w') as log:
            subprocess.run(['docker', 'build', '--network', 'none', '-t', tag, str(build)],
                check=True, timeout=120, stdout=log, stderr=subprocess.STDOUT)
        image = manage.inspect(tag)['Id']
    else:
        tag, image = original['retained_images']['simulator'], original['images']['simulator']
    shutil.copytree(base, destination, copy_function=os.link)
    config = destination/'vision_simulator_26/config.toml'
    replace(config, manage.override(config.read_text(), 'capture.effect', 'grain', str(grain)))
    shader_path = destination/'vision_simulator_26/assets/shaders/camera_lens.wgsl'
    shader_path.parent.mkdir(parents=True, exist_ok=True)
    replace(shader_path, shader)
    meta = copy.deepcopy(original)
    meta.update(bundle_id=destination.name, created=time.time(), parent_bundle=str(base))
    meta['images']['simulator'] = image
    meta['retained_images']['simulator'] = tag
    meta['files'].update({str(p.relative_to(destination)): sha(p) for p in (config, shader_path)})
    meta['diagnostic'] = dict(phase_offset=phase, grain=grain,
        intervention='Native capture shader grain phase offset and declared grain-only negative control',
        interfaces_unchanged=True, evaluator_interfaces_from=original['images']['simulator'],
        default_images_changed=False, algorithm_unchanged=True, capacity_registration=False)
    replace(destination/'bundle.json', json.dumps(meta, indent=2))
    for path in destination.rglob('*'):
        if path.is_file():
            path.chmod(path.stat().st_mode & ~0o222)
    return destination


def run(root, base):
    root.mkdir(parents=True, exist_ok=True)
    path = root/'sensor-study.json'
    state = load(path)
    if not state:
        with jobs.connect() as db:
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','pausing')").fetchone()[0]:
                raise RuntimeError('Drain other live tasks first')
        state = dict(state='freezing', parent_bundle=str(base), modes={}, created=time.time(),
            conditions=CONDITIONS, maximum_trials=12, timing_profile='ui-fixed-v6', parallelism=1,
            plan='Same frozen simulator binary/model/algorithm and session-reset contract. Native grain phase '
                '0/1024/2048 at original strength .05; phase0 repeated. Grain0 phase0/1024 negative control. '
                'Two scenes each. Every result retained; no retuning or score-based phase selection.',
            certified=False, capacity_registration=False)
        write(path, state)
    for label, phase, grain, repeats in CONDITIONS:
        entry = state['modes'].setdefault(label, dict(runs=[], jobs=[]))
        if 'bundle' not in entry:
            entry['bundle'] = str(freeze(root, base, label, phase, grain))
            write(path, state)
    # Never overlap live effect measurements with offline inference replays.
    while load(root/'study.json').get('state') != 'completed':
        time.sleep(1)
    state['state'] = 'running'
    write(path, state)
    for label, phase, grain, repeats in CONDITIONS:
        entry = state['modes'][label]
        if not entry['jobs']:
            rows = []
            for repeat in range(repeats):
                for scene in SCENES:
                    settings = dict(DEFAULTS, scene=scene, max_fps=240, execution_mode='lockstep',
                        timing_profile='ui-fixed-v6', detector_history='session', session_evidence='on',
                        seed=2026102207, record_timing='off', label='sensor-phase-'+label)
                    row = jobs.validate({k:v for k,v in settings.items() if k in jobs.FIELDS})
                    row.update(repeat_id=repeat, execution_profile='lockstep-ui-fixed-v6')
                    rows.append(row)
            identifier, now = 'job_sensor_'+uuid.uuid4().hex[:12], time.time()
            with jobs.connect() as db, jobs.transaction(db):
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)', (identifier, 'queued', now, now,
                    json.dumps(dict(version=1, _protocol=2, _sensor_diagnostic=True)), entry['bundle'], 1, 0, None))
                db.executemany('INSERT INTO items(job_id,ordinal,state,settings) VALUES(?,?,?,?)',
                    [(identifier, i, 'pending', json.dumps(r)) for i, r in enumerate(rows)])
                jobs.event(db, identifier, 'Native sensor phase diagnostic; no UI/capacity qualification')
            entry['jobs'].append(identifier)
            write(path, state)
        for identifier in entry['jobs']:
            while True:
                with jobs.connect() as db:
                    status = jobs.status(db, identifier)
                if status['state'] == 'completed':
                    break
                if status['state'] in ('paused', 'cancelled'):
                    state.update(state='failed', failed_job=identifier)
                    write(path, state)
                    raise RuntimeError('Failed diagnostic retained: '+identifier)
                time.sleep(1)
            with jobs.connect() as db:
                ingest(db, identifier, entry)
                jobs.report(db, identifier)
        write(path, state)
    state.update(state='completed', ended=time.time())
    write(path, state)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--bundle', type=Path, required=True)
    args = p.parse_args()
    run(args.output.resolve(), args.bundle.resolve())
