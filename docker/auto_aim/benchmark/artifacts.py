"""Portable frozen payloads; historical origin is optional metadata only."""
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from .core import sha,identity


def payload_identity(meta):
    return identity({k:v for k,v in meta.items() if k not in ('content_id','created','bundle_id','source_workspace','retained_images','benchmark')})


def verify(bundle,expected_identity=None):
    bundle=Path(bundle);meta=json.loads((bundle/'bundle.json').read_text())
    if meta.get('content_id') and meta['content_id']!=payload_identity(meta):raise ValueError('Payload metadata changed')
    if expected_identity and meta.get('content_id')!=expected_identity:raise ValueError('Payload identity differs from frozen contract')
    actual={str(p.relative_to(bundle)) for p in bundle.rglob('*') if p.is_file() and p!=bundle/'bundle.json'}
    extra=actual-set(meta['files'])
    if set(meta['files'])-actual or any('__pycache__' not in Path(p).parts for p in extra):raise ValueError('Unexpected or missing payload files')
    for name,digest in meta['files'].items():
        p=Path(name)
        if p.is_absolute() or '..' in p.parts or (bundle/p).is_symlink() or sha(bundle/p)!=digest:raise ValueError('Frozen payload mismatch: '+name)
    return meta


def seal(bundle,meta):
    meta=dict(meta,content_id=payload_identity(meta))
    path=Path(bundle)/'bundle.json';path.unlink(missing_ok=True)
    path.write_text(json.dumps(meta,indent=2)+'\n')
    for p in Path(bundle).rglob('*'):
        if p.is_file():p.chmod(p.stat().st_mode & ~0o222)
    return verify(bundle)


def export_bundle(bundle,archive,images=False):
    bundle=Path(bundle);meta=verify(bundle);archive=Path(archive).resolve()
    if archive.is_relative_to(bundle.resolve()):raise ValueError('Archive must be outside payload')
    archive.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(archive,'w:gz',dereference=True) as out:out.add(bundle,arcname='payload')
    if images:subprocess.run(['docker','save','-o',str(archive)+'.images.tar',*sorted(set(meta['images'].values()))],check=True)
    result=dict(archive=str(archive),sha256=sha(archive),images_included=images)
    if images:result['images_sha256']=sha(str(archive)+'.images.tar')
    Path(str(archive)+'.manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def import_bundle(archive,destination,images=False):
    manifest=Path(str(archive)+'.manifest.json')
    if manifest.exists():
        expected=json.loads(manifest.read_text())
        if sha(archive)!=expected['sha256']:raise ValueError('Archive digest mismatch')
        if images and sha(str(archive)+'.images.tar')!=expected.get('images_sha256'):raise ValueError('Image archive digest mismatch')
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Import requires a new directory')
    with tarfile.open(archive) as src:
        for member in src.getmembers():
            p=Path(member.name)
            if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!='payload' or member.issym() or member.islnk():
                raise ValueError('Unsafe archive member')
        destination.mkdir(parents=True)
        src.extractall(destination,filter='data')
    bundle=destination/'payload';verify(bundle)
    if images:subprocess.run(['docker','load','-i',str(archive)+'.images.tar'],check=True)
    return bundle


def freeze(identifier,environment,adapter,artifact):
    import manage
    import background_jobs as jobs
    release=json.loads((manage.OPS/'input-contract-runtime.json').read_text())
    images=dict(simulator=release['simulator']['tag'],evaluator=release['evaluator']['tag'],algorithm='daedalus:rm26-algorithm')
    description=adapter.describe(manage.ROOT,artifact)
    config=(Path(adapter.manifest).parent/description['config']) if hasattr(adapter,'manifest') else Path(__file__).parent/'configs/B1.yaml'
    if description['config_sha256']!=sha(config):raise ValueError('Algorithm configuration checksum mismatch')
    if description.get('adapter')=='r4':
        proof=Path(artifact).resolve().parent.parent/'build-provenance.json'
        if proof.exists() and json.loads(proof.read_text())!=description:raise ValueError('B1 source or configuration differs from build provenance; rebuild')
    if description.get('image'):images['algorithm']=description['image']
    if description.get('runtime_image'):
        images=dict.fromkeys(('simulator','evaluator','algorithm'),description['runtime_image'])
    bundle=jobs.freeze(identifier,environment.gimbal_dynamics,algorithm_binary=artifact,image_tags=images)
    meta=json.loads((bundle/'bundle.json').read_text())
    target=bundle/'sp_vision_25/configs/simulator.yaml';target.unlink();shutil.copy2(config,target)
    name='sp_vision_25/configs/simulator.yaml';meta['files'][name]=sha(target)
    if description.get('resources'):
        source=Path(adapter.manifest).parent/description['resources']
        if not source.is_dir() or any(p.is_symlink() for p in source.rglob('*')):raise ValueError('Resources must be a plain directory')
        destination=bundle/'sp_vision_25/assets'
        for p in destination.rglob('*'):
            if p.is_file():p.chmod(0o600)
        shutil.rmtree(destination);shutil.copytree(source,destination)
        meta['files']={k:v for k,v in meta['files'].items() if not k.startswith('sp_vision_25/assets/')}
        meta['files'].update({str(p.relative_to(bundle)):sha(p) for p in destination.rglob('*') if p.is_file()})
    meta.update(algorithm=description,environment=environment.__dict__,benchmark_version=1)
    seal(bundle,meta)
    return bundle
