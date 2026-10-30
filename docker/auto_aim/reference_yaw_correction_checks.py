"""Source-transcription, production Rust response and model-routing checks.

Writes only a new correction study; historical evidence is read-only.
"""
import cmath
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
from unittest.mock import patch

import gimbal_dynamics as models
import manage

ROOT = manage.ROOT
OUT = manage.OPS / 'experiments/reference-yaw-correction-20261029'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def routing_checks():
    import background_jobs
    import input_contract
    import experiments
    count = 0
    for controller in manage.GIMBAL_PROFILES:
        for model in models.MODELS:
            settings = background_jobs.validate(dict(scene='hf_static_5', terrain='flat_range',
                gimbal_profile=controller, gimbal_dynamics=model))
            config = tomllib.loads(manage.render_config(settings))
            assert config['vehicle']['gimbal_pid'].get('dynamics', 'legacy') == model
            count += 1
    assert models.simulator_image(models.MODELS, 'base') == models.CORRECTED_IMAGE
    assert models.simulator_image(['legacy', models.HISTORICAL], 'base') == models.HISTORICAL_IMAGE
    assert models.simulator_image('legacy', 'base') == 'base'
    release = json.loads((manage.OPS / 'input-contract-runtime.json').read_text())
    original_inspect = manage.inspect
    for model in models.MODELS:
        with input_contract.runtime(dict(input_contract='aligned-v1', gimbal_dynamics=model), manage):
            assert os.environ['RM26_SIMULATOR_IMAGE'] == models.simulator_image(model, release['simulator']['tag'])
            assert os.environ['RM26_BRIDGE_IMAGE'] == release['simulator']['tag']
            assert manage.IMAGE == release['simulator']['tag']
        assert manage.inspect is original_inspect
    # Exercise actual compose selection without launching services.
    with tempfile.TemporaryDirectory() as tmp:
        runtime = Path(tmp)
        for model in models.MODELS:
            (runtime / 'launch.json').write_text(json.dumps(dict(gimbal_dynamics=model)))
            env = dict(RM26_GPU_UUID='fixture', RM26_INTEGRATION_IMAGE_ID='fixture')
            with patch.object(manage, 'RUNTIME', runtime), patch.object(manage, 'run') as run, patch.object(manage, 'inspect', return_value=None), patch.dict(os.environ, env, clear=True):
                manage.compose('config')
                selected = run.call_args.kwargs['env'].get('RM26_SIMULATOR_IMAGE')
                assert selected == (None if model == 'legacy' else models.simulator_image(model, 'base'))
    args=manage.parser().parse_args(['experiment','run','--matrix','highspeed-v1','--gimbal-dynamics',models.RAW])
    try:
        experiments.execute(manage,args)
    except RuntimeError as error:
        assert 'fixed gimbal dynamics' in str(error)
    else:
        raise AssertionError('Historical matrix silently ignored selected dynamics')
    return dict(cli_config_combinations=count, aligned_ui_selection=True, compose_selection=True,
                mixed_matrix_selection=True, historical_matrix_cannot_ignore_selected_model=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    image_path = manage.OPS / 'experiments/reference-yaw-20261028/sources/formula.png'
    assert digest(image_path) == models.FORMULA_SHA256
    assert models.description(models.HISTORICAL)['coefficient_sha256'] == '224f96e68a2855b76fda2c0850084cb65baa5da888aa6048c896d6397ae3f971'
    # Explicit reviewed source expectations, independent of the implementation.
    for model in (models.RAW, models.UNITDC):
        desc = models.description(model)
        assert desc['numerator'] == [-12.65, 1084] and desc['denominator'] == [1, 34.16, 975]
        assert math.isclose(desc['original_dc_gain'], 1084 / 975, abs_tol=1e-15)
    source = r'''#[path="/work/vision_simulator_26/src/systems/yaw_reference.rs"] mod model;
use model::{YawReference,ReferenceModel};
fn main() {
for id in ["rm2017-yaw-unitdc","rm2017-yaw-corrected-raw","rm2017-yaw-corrected-unitdc"] {
 let kind=ReferenceModel::from_id(id).unwrap();
 for f in [0.5,1.,2.,4.,6.4,10.,20.,40.] {
  let dt=0.0001;let w=std::f64::consts::TAU*f;let mut state=YawReference::default();
  let (mut re,mut im)=(0.,0.);
  for k in 0..200000 {let t=(k+1) as f64*dt;let u=0.01*(w*(t-0.5*dt)).cos();
   let a=kind.advance(&mut state,u,dt,100.);
   if k>=100000 {re+=a.rate*(w*t).cos();im-=a.rate*(w*t).sin();}}
  println!("freq,{id},{f},{},{}",re/500.,im/500.);
 }
 for hz in [250.,500.,1000.] {for trace in ["step","ramp","reverse","jump","saturation"] {
  let dt=1./hz;let mut state=YawReference::default();let mut angle=0.;
  for k in 0..(hz*2.) as usize {let t=(k as f64+0.5)*dt;
   let u=match trace {"step"=>1.,"ramp"=>t,"reverse"=>if t<1.{1.}else{-1.},"jump"=>if t<1.{0.}else{2.},_=>100.};
   let a=kind.advance(&mut state,u,dt,20.);angle+=a.angle;
   if hz==1000. {println!("sample,{id},{trace},{},{u},{},{angle},{}",(k+1) as f64*dt,a.rate,a.limited);}
   if k==(hz*2.) as usize-1 {println!("trace,{id},{trace},{hz},{angle},{}",a.rate);}
  }
 }}
}}
'''
    harness = OUT / 'kernel-harness.rs'
    harness.write_text(source)
    command = ['docker', 'run', '--rm', '-v', str(ROOT)+':/work:ro', '--entrypoint', 'sh',
        'rust:1.98-slim-bookworm', '-c',
        'rustc --test /work/vision_simulator_26/src/systems/yaw_reference.rs -O -o /tmp/tests && /tmp/tests && '
        'rustc /work/docker/auto_aim/experiments/reference-yaw-correction-20261029/kernel-harness.rs -O -o /tmp/check && /tmp/check']
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    (OUT / 'kernel-output.txt').write_text(result.stdout + result.stderr)
    frequency, traces, samples = [], [], ['model,trace,t_s,input_rad_s,rate_rad_s,angle_rad,limited']
    for line in result.stdout.splitlines():
        p = line.split(',')
        if p[0] == 'freq':
            model, f = p[1], float(p[2])
            s = 2j * math.pi * f
            b, c = (43.16, 3975) if model == models.HISTORICAL else (34.16, 975)
            expected = (-12.65*s+1084)/(s*s+b*s+c)
            if model != models.RAW:
                expected /= 1084/c
            actual = complex(float(p[3]), float(p[4]))
            db, phase = 20*math.log10(abs(actual/expected)), math.degrees(cmath.phase(actual/expected))
            assert abs(db) < .002 and abs(phase) < .02, (model, f, db, phase)
            frequency.append(dict(model=model, hz=f, magnitude_error_db=db, phase_error_deg=phase, expected_magnitude=abs(expected), observed_magnitude=abs(actual)))
        elif p[0] == 'trace':
            traces.append(dict(model=p[1], kind=p[2], hz=float(p[3]), angle=float(p[4]), rate=float(p[5])))
        elif p[0] == 'sample':
            samples.append(','.join(p[1:]))
    assert len(frequency) == 24 and len(traces) == 45 and len(samples) == 30001
    convergence = []
    for model in models.MODELS[1:]:
        for kind in ('step', 'ramp', 'reverse', 'jump', 'saturation'):
            rows = [r for r in traces if r['model'] == model and r['kind'] == kind]
            span = max(r['angle'] for r in rows) - min(r['angle'] for r in rows)
            assert span < .0001
            convergence.append(dict(model=model, kind=kind, angle_span_rad=span))
    (OUT / 'kernel-response.csv').write_text('\n'.join(samples)+'\n')
    # Compare historical kernel bit-for-bit to its archived source, without editing it.
    old = (manage.OPS/'experiments/reference-yaw-20261028/kernel_harness.rs').read_text()
    original = (manage.OPS/'experiments/reference-yaw-20261028/kernel-output.txt').read_text()
    (OUT/'historical-harness.rs').write_text(old)
    check = subprocess.run(['docker','run','--rm','-v',str(ROOT)+':/work:ro','--entrypoint','sh','rust:1.98-slim-bookworm','-c',
        'rustc /work/docker/auto_aim/experiments/reference-yaw-correction-20261029/historical-harness.rs -O -o /tmp/old && /tmp/old'], capture_output=True, text=True, check=True)
    numerical = lambda txt: [line for line in txt.splitlines() if line.startswith(('freq,','trace,'))]
    assert numerical(check.stdout) == numerical(original)
    (OUT/'historical-replay.txt').write_text(check.stdout)
    report = dict(passed=True, source_image_sha256=digest(image_path), models={m:models.description(m) for m in models.MODELS[1:]},
        frequency=frequency, max_magnitude_error_db=max(abs(r['magnitude_error_db']) for r in frequency),
        max_phase_error_deg=max(abs(r['phase_error_deg']) for r in frequency), convergence=convergence,
        routing=routing_checks(), historical_kernel_bit_identical=True,
        source_to_coefficients='original image independently read by root and Luna; manual transcription review, not OCR accuracy certification',
        scope='Numerical/source/routing consistency only; not real-robot identification or shooting improvement',
        excitation='Offline midpoint-sampled sine; no artificial communication timing change')
    (OUT/'component-checks.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('frequency','convergence','models')},indent=2))


if __name__ == '__main__':
    main()
