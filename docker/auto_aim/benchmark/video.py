"""Build the existing renderer's input from explicit paired runs."""
from pathlib import Path
import shutil
from .core import sha
from .evaluation import Evaluator
from report import load,write


def prepare(left,right,output,left_label='reference',right_label='B1'):
    import manage
    import review_video as video
    output=Path(output).resolve()
    if output.exists():raise ValueError('Video export needs a new directory')
    output.mkdir(parents=True)
    folders=[Path(p).resolve() for p in (left,right)]
    manifests=[load(p/'manifest.json') for p in folders]
    # Algorithms may use different weights; all environmental conditions must pair.
    pair=[dict(m,algorithm_weights_sha256={},shooting=dict(m['shooting'])) for m in manifests]
    for m in pair:
        if m['shooting'].get('gimbal_dynamics') is None:m['shooting']['gimbal_dynamics']='legacy'
    video.validate_pair(*pair)
    sides=[];frozen={};scores=[]
    for folder,label in zip(folders,(left_label,right_label)):
        result=Evaluator().evaluate(folder,output/'normalized-scores'/folder.name)
        if not result['evidence_valid']:raise ValueError('Invalid source evidence')
        state=load(folder/'run-state.json');frames=list(video.lines(folder/'frames.jsonl'))
        start=state['measurement_start_stamp_ns'];end=(state['measurement_end_stamp_ns']-start)/1e9
        tail=(frames[-1]['stamp_ns']-start)/1e9
        if tail<end:raise ValueError('Missing settlement tail')
        sides.append(dict(variant=label,run_id=folder.name,folder=str(folder),start_ns=start,measurement_s=end,tail_end_s=tail,score=result['metrics']))
        frozen[str(folder)]={n:sha(folder/n) for n in video.SOURCE_FILES}
        scores.append(result['metrics'])
    source=output/'renderer-source';source.mkdir()
    names=('review_video.py','review_video_render.py','review_video_checks.py','report.py','shots.py','impact_quality.py','scenario_catalog.py','highspeed_scenarios.py','focus_scenarios.py','movement_scenarios.py')
    for name in names:shutil.copy2(manage.OPS/name,source/name)
    font=output/'review-font.ttc';shutil.copy2(video.FONT,font)
    frozen[str(output)]={str(p.relative_to(output)):sha(p) for p in (output/'normalized-scores').rglob('*') if p.is_file()}
    scene=manifests[0]['scene'];case=dict(scene=scene,sensor_seed=manifests[0]['sensor_seed'],phase='migration',sides=sides,play_s=max(s['tail_end_s'] for s in sides))
    summary=[dict(scene=scene,seeds=1,before=scores[0],after=scores[1])]
    spec=dict(version=1,study=str(output),statistics=str(output),report=str(output/'report.md'),baseline=left_label,baseline_label=left_label,candidate=right_label,
        engine='后台迁移一致性检查',capability_study=False,cases=[case],summary=summary,acceptance_passed=False,qualification={},paired_effects=None,review_status='pending_team_review',
        render=dict(width=2560,height=1440,fps=30,codec='h264',intro_s=5,title_s=2,hold_s=2,summary_s=15),source_hashes=frozen,
        scorer_sha256={n:sha(manage.OPS/n) for n in ('shots.py','impact_quality.py')},code_sha256={n:sha(source/n) for n in names},renderer_source=str(source),
        font_sha256=sha(font),font_path=str(font),evaluator_image=load(folders[1]/'configuration/bundle.json')['images']['evaluator'])
    write(output/'export.json',spec)
    return output
