"""Compile production Rust math kernel and compare to independent complex oracle."""
import cmath,json,math,subprocess,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docker/auto_aim/experiments/reference-yaw-20261028'
def main():
    source='''#[path="/work/vision_simulator_26/src/systems/yaw_reference.rs"] mod model;
use model::YawReference;
fn main(){
for unit in [false,true] {for f in [0.5,1.,2.,4.,6.4,10.,20.] {
 let dt=0.0001; let w=std::f64::consts::TAU*f;let mut m=YawReference::default();let mut re=0.;let mut im=0.;let mut n=0.;
 for k in 0..200000 {let t=(k+1) as f64*dt;let u=(w*(t-dt*0.5)).cos()*0.01;
 let a=m.advance(u,dt,100.,unit); if k>=100000 {re+=a.rate*(w*t).cos();im-=a.rate*(w*t).sin();n+=1.;}}
 println!("freq,{},{},{},{}",unit,f,re*2./n/0.01,im*2./n/0.01);
}}
for hz in [250.,500.,1000.] {for kind in 0..5 {
 let dt=1./hz;let mut m=YawReference::default();let mut angle=0.;
 for k in 0..(hz*2.) as usize {let t=(k as f64+0.5)*dt;let u=match kind {0=>1.,1=>t,2=>if t<1.{1.}else{-1.},3=>if t<1.{0.}else{2.},_=>100.};angle+=m.advance(u,dt,20.,true).angle;}
 println!("trace,{},{},{},{}",hz,kind,angle,m.output(true));
}}
}
'''
    harness=OUT/'kernel_harness.rs';harness.write_text(source)
    cmd=['docker','run','--rm','-v',str(ROOT)+':/work','-w','/work','--entrypoint','sh','rust:1.98-slim-bookworm','-c','rustc --test vision_simulator_26/src/systems/yaw_reference.rs -o /tmp/tests && /tmp/tests && rustc docker/auto_aim/experiments/reference-yaw-20261028/kernel_harness.rs -O -o /tmp/check && /tmp/check']
    result=subprocess.run(cmd,capture_output=True,text=True,check=True);(OUT/'kernel-output.txt').write_text(result.stdout+result.stderr)
    frequencies=[];traces=[]
    for line in result.stdout.splitlines():
        p=line.split(',')
        if p[0]=='freq':
            unit=p[1]=='true';f=float(p[2]);observed=complex(float(p[3]),float(p[4]));s=2j*math.pi*f
            expected=(-12.65*s+1084)/(s*s+43.16*s+3975)/(1084/3975 if unit else 1)
            db=20*math.log10(abs(observed/expected));deg=math.degrees(cmath.phase(observed/expected))
            assert abs(db)<=.2 and abs(deg)<=2,(unit,f,db,deg)
            frequencies.append(dict(unit_dc=unit,hz=f,expected_magnitude=abs(expected),expected_phase_deg=math.degrees(cmath.phase(expected)),observed_magnitude=abs(observed),magnitude_error_db=db,phase_error_deg=deg))
        elif p[0]=='trace':traces.append(dict(hz=float(p[1]),kind=int(p[2]),angle=float(p[3]),rate=float(p[4])))
    for kind in range(5):
        rows=[r for r in traces if r['kind']==kind];assert max(r['angle'] for r in rows)-min(r['angle'] for r in rows)<.0001
    import sys
    sys.path.insert(0,str(ROOT/'docker/auto_aim'))
    from sync_timing import Schedule
    a=json.loads((ROOT/'docker/auto_aim/timing_profiles/ui-aligned-v7-clock.json').read_text());b=dict(a,return_delay_ns=a.get('return_delay_ns',0)+20_000_000)
    sa,sb=Schedule(a,'adv_variable_pos'),Schedule(b,'adv_variable_pos')
    for i in range(1000):
        assert sa.control_delta()==sb.control_delta() and sa.image_interval()==sb.image_interval() and sa.delays(i)==sb.delays(i) and sa.pose_delay(i)==sb.pose_delay(i)
        assert sb.return_delay(i)-sa.return_delay(i)==20_000_000
    from reference_yaw_transport_checks import run as transport_run
    transport=transport_run();(OUT/'transport-checks.json').write_text(json.dumps(transport,indent=2))
    import manage,background_jobs,tomllib
    for controller in manage.GIMBAL_PROFILES:
        for model in manage.GIMBAL_DYNAMICS:
            parsed=manage.parser().parse_args(['experiment','run','--gimbal-profile',controller,'--gimbal-dynamics',model]);assert parsed.gimbal_dynamics==model
            settings=background_jobs.validate(dict(scene='hf_static_5',terrain='flat_range',gimbal_profile=controller,gimbal_dynamics=model));assert settings['gimbal_dynamics']==model
            config=tomllib.loads(manage.render_config(settings));assert config['vehicle']['gimbal_pid'].get('dynamics','legacy')==model
    integration=None;integration_pass=False
    if (OUT/'integration-tests.json').exists():
        integration=json.loads((OUT/'integration-tests.json').read_text())
        import hashlib
        assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in integration['source_sha256'].items())
        assert hashlib.sha256((ROOT/integration['log']).read_bytes()).hexdigest()==integration['log_sha256']
        integration_pass=bool(integration['passed'] and manage.inspect('daedalus:rm26-reference-yaw-20261028')['Id']==integration['runtime_image'])
    report=dict(production_tracker_integration=integration,cli_config_orthogonal_combinations=8,delay_transport_causality=transport,kernel_pass=True,integration_pass=integration_pass,passed=integration_pass,frequency=frequencies,convergence=traces,delay_single_link_schedule_verified=True,production_reset_and_wrap='compiled production tracker checks passed' if integration_pass else 'pending integration checks',excitation='Offline midpoint-sampled ZOH sine; frequency oracle only; production inputs remain causal',limitations='6.4Hz and IHiter 80–100ms are comparison context, not acceptance thresholds')
    (OUT/'component-checks.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if __name__=='__main__':
    raise SystemExit('Historical check output is sealed and uses mis-transcribed coefficients. Run reference_yaw_correction_checks.py; historical reproduction uses its frozen bundle.')
