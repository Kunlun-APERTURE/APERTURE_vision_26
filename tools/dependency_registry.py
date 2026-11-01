#!/usr/bin/env python3
"""发布固定身份的依赖镜像，并恢复实验工具使用的本地名称。"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'release/dependencies.json'
REPOSITORY = 'Kunlun-APERTURE/APERTURE_vision_26'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
IMAGE_REPOSITORY = re.compile(r'ghcr\.io/kunlun-aperture/(?:rm26-[a-z0-9-]+|aperture_vision)')


def run(args):
    return subprocess.check_output(args, text=True)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def release_tag(value):
    if not re.fullmatch(r'\d{2}-\d{1,2}-\d{1,2}', value):
        raise ValueError('版本标签必须为 年-月-日，例如 26-11-1')
    datetime.datetime.strptime(value, '%y-%m-%d')
    return value


def image_tag(value):
    parsed = datetime.datetime.strptime(release_tag(value), '%y-%m-%d')
    return f'{parsed.year % 100:02d}-{parsed.month}-{parsed.day}'


def load(path, require_reference=True):
    data = json.loads(Path(path).read_text())
    if data.get('version') not in (2, 3) or data.get('platform') != 'linux/amd64':
        raise ValueError('不支持的镜像清单版本或平台')
    if not data.get('images'):
        raise ValueError('镜像清单不能为空')
    if data['version'] == 3 and (len(data['images']) != 1 or data['images'][0]['repository'] != 'ghcr.io/kunlun-aperture/aperture_vision'):
        raise ValueError('统一环境只允许一个 APERTURE_vision 镜像')
    names = set()
    repositories = set()
    for entry in data['images']:
        local, repository = entry['local'], entry['repository']
        if not re.fullmatch(r'[a-z0-9_./-]+:[a-zA-Z0-9_.-]+', local) or local.startswith('-'):
            raise ValueError('本地镜像名无效')
        if not IMAGE_REPOSITORY.fullmatch(repository):
            raise ValueError('镜像必须位于团队 GHCR 命名空间')
        if local in names or repository in repositories:
            raise ValueError('镜像名称重复')
        names.add(local)
        repositories.add(repository)
        if not all(DIGEST.fullmatch(entry[key]) for key in ('image_id', 'manifest_id', 'config_id')):
            raise ValueError('镜像索引、平台清单或配置身份无效')
        reference = entry.get('reference', '')
        if require_reference or reference:
            prefix = repository + '@'
            if not reference.startswith(prefix) or not DIGEST.fullmatch(reference[len(prefix):]):
                raise ValueError('缺少固定 digest 的镜像来源；请先完成首批迁移')
            if reference != prefix + entry['image_id']:
                raise ValueError('来源 digest 与固定镜像索引不一致')
    return data


def inspect_manifest(reference, anonymous=False):
    args = ['skopeo', 'inspect', '--raw']
    if anonymous:
        args.append('--no-creds')
    return run(args + [reference])


def verify_source(entry, source, anonymous=False):
    raw = inspect_manifest(source, anonymous)
    if 'sha256:' + hashlib.sha256(raw.encode()).hexdigest() != entry['image_id']:
        raise ValueError('镜像索引身份不匹配: ' + entry['local'])


def check_local(entry, reference):
    info = json.loads(run(['docker', 'image', 'inspect', reference]))[0]
    identities = {entry[key] for key in ('image_id', 'manifest_id', 'config_id')}
    if info['Id'] not in identities or info['Os'] + '/' + info['Architecture'] != 'linux/amd64':
        raise ValueError('本地镜像身份或架构不匹配: ' + reference)


def existing_reference(entry, tag):
    destination = entry['repository'] + ':' + tag
    result = subprocess.run(['skopeo', 'inspect', '--raw', 'docker://' + destination],
                            capture_output=True, text=True)
    if result.returncode:
        if any(error in result.stderr.lower() for error in ('manifest unknown', 'name unknown')):
            return None
        raise RuntimeError('无法检查已有版本，停止发布: ' + result.stderr)
    if 'sha256:' + hashlib.sha256(result.stdout.encode()).hexdigest() != entry['image_id']:
        raise ValueError('版本标签已指向其他镜像，禁止覆盖: ' + destination)
    return entry['repository'] + '@' + entry['image_id']


def pull(data):
    for entry in data['images']:
        reference = entry['reference']
        subprocess.run(['docker', 'pull', '--platform', data['platform'], reference], check=True)
        check_local(entry, reference)
        subprocess.run(['docker', 'tag', reference, entry['local']], check=True)
    print('依赖镜像已校验并恢复本地名称')


def check(data):
    for entry in data['images']:
        check_local(entry, entry['local'])
    print('本地依赖镜像身份校验通过')


def publish(data, tag, output, archive=None):
    release_tag(tag)
    if data['version'] == 3:
        acceptance = data['acceptance']
        proof = (ROOT / acceptance['path']).resolve()
        if not proof.is_relative_to(ROOT) or sha(proof) != acceptance['sha256']:
            raise ValueError('镜像验收记录缺失或校验失败')
        verified = json.loads(proof.read_text())
        if not verified.get('passed') or verified.get('image_id') != data['images'][0]['image_id']:
            raise ValueError('该统一镜像尚未通过验收')
    output = Path(output)
    result = dict(data, release=tag, images=[])
    target_tag = image_tag(tag) if data['version'] == 3 else tag
    if data['version'] == 3:
        result['image_tag'] = target_tag
        result['release_sources'] = dict(main=run(['git', 'rev-parse', 'HEAD']).strip())
        for name in ('sp_vision_25', 'vision_simulator_26'):
            result['release_sources'][name] = run(['git', 'ls-tree', 'HEAD', name]).split()[2]
    sources = []
    existing = []
    for entry in data['images']:
        source = ('oci-archive:' + str(Path(archive).resolve()) + ':' + entry['local'].rsplit(':', 1)[1]
                  if archive else 'docker://' + entry['reference'])
        verify_source(entry, source)
        sources.append(source)
        existing.append(existing_reference(entry, target_tag))
    with tempfile.TemporaryDirectory() as temporary:
        for index, (entry, source, reference) in enumerate(zip(data['images'], sources, existing)):
            if reference:
                result['images'].append(dict(entry, reference=reference))
                continue
            destination = entry['repository'] + ':' + target_tag
            digest_file = Path(temporary) / str(index)
            subprocess.run(['skopeo', 'copy', '--all', '--preserve-digests',
                            '--retry-times', '3', '--digestfile', str(digest_file),
                            source, 'docker://' + destination], check=True)
            digest = digest_file.read_text().strip()
            if digest != entry['image_id']:
                raise ValueError('镜像仓库返回的 digest 无效')
            reference = entry['repository'] + '@' + digest
            verify_source(entry, 'docker://' + reference)
            result['images'].append(dict(entry, reference=reference))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('发布清单已保存: ' + str(output))


def verify_public(data, download=False):
    for entry in data['images']:
        source = 'docker://' + entry['reference']
        verify_source(entry, source, anonymous=True)
        if download:
            with tempfile.TemporaryDirectory() as temporary:
                subprocess.run(['skopeo', 'copy', '--all', '--preserve-digests', '--src-no-creds', '--retry-times', '3',
                                source, 'dir:' + temporary], check=True)
    print('公开镜像匿名读取验证通过')


def bootstrap(path, output):
    data = json.loads(Path(path).read_text())
    release_tag(data['release'])
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        for part in data['parts']:
            if Path(part['name']).name != part['name']:
                raise ValueError('分片名称无效')
            subprocess.run(['gh', 'release', 'download', data['release'], '--repo', REPOSITORY,
                            '--pattern', part['name'], '--dir', temporary], check=True)
            p = Path(temporary) / part['name']
            if p.stat().st_size != part['size'] or sha(p) != part['sha256']:
                raise ValueError('依赖包分片校验失败')
        with output.open('wb') as stream:
            for part in data['parts']:
                with (Path(temporary) / part['name']).open('rb') as source:
                    for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
                        stream.write(block)
    if sha(output) != data['archive_sha256']:
        output.unlink()
        raise ValueError('依赖包完整校验失败')
    print('首批依赖包下载和校验完成')


def attach(data, path):
    tag = release_tag(data['release'])
    asset_name = Path(path).name
    with tempfile.TemporaryDirectory() as temporary:
        assets = json.loads(run(['gh', 'release', 'view', tag, '--repo', REPOSITORY,
                                 '--json', 'assets']))['assets']
        if any(asset['name'] == asset_name for asset in assets):
            subprocess.run(['gh', 'release', 'download', tag, '--repo', REPOSITORY,
                            '--pattern', asset_name, '--dir', temporary], check=True)
            if json.loads((Path(temporary) / asset_name).read_text()) != data:
                raise ValueError('该 Release 已有不同镜像清单，禁止覆盖')
            return
    subprocess.run(['gh', 'release', 'upload', tag, str(path), '--repo', REPOSITORY], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('pull', 'check', 'publish', 'verify-public', 'bootstrap', 'attach'))
    parser.add_argument('--manifest', type=Path, default=CATALOG, help='镜像清单路径')
    parser.add_argument('--tag', help='日期版本标签，例如 26-11-1')
    parser.add_argument('--output', type=Path, help='输出清单或归档路径')
    parser.add_argument('--archive', type=Path, help='仅首批迁移：保留 OCI 索引的已校验归档')
    parser.add_argument('--download', action='store_true', help='匿名下载全部镜像层进行验证')
    args = parser.parse_args()
    if args.action == 'bootstrap':
        if not args.output:
            parser.error('bootstrap 需要 --output')
        bootstrap(args.manifest, args.output)
        return
    data = load(args.manifest, require_reference=not (args.action == 'publish' and args.archive))
    if args.action == 'publish':
        if not args.tag or not args.output:
            parser.error('publish 需要 --tag 和 --output')
        publish(data, args.tag, args.output, args.archive)
    elif args.action == 'pull':
        pull(data)
    elif args.action == 'check':
        check(data)
    elif args.action == 'verify-public':
        verify_public(data, args.download)
    elif args.action == 'attach':
        attach(data, args.manifest)


if __name__ == '__main__':
    main()
