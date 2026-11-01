import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dependency_registry', Path(__file__).resolve().parents[1] / 'tools/dependency_registry.py')
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)
RAW = json.dumps(dict(schemaVersion=2, manifests=[]))
ID = 'sha256:' + hashlib.sha256(RAW.encode()).hexdigest()
DIGEST = ID
REPO = 'ghcr.io/kunlun-aperture/rm26-development'
ENTRY = dict(local='sp_vision_dev:2024.6.0', repository=REPO, image_id=ID, manifest_id=ID, config_id=ID, reference=REPO + '@' + DIGEST)
DATA = dict(version=2, platform='linux/amd64', images=[ENTRY])


class RegistryTests(unittest.TestCase):
    def test_manifest_rejects_unpinned_foreign_or_duplicate_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'manifest.json'
            path.write_text(json.dumps(DATA))
            self.assertEqual(registry.load(path), DATA)
            for change in ({'reference': REPO + ':latest'}, {'repository': 'ghcr.io/other/image'},
                           {'image_id': 'sha256:bad'}, {'local': '--help'}):
                path.write_text(json.dumps(dict(DATA, images=[dict(ENTRY, **change)])))
                with self.assertRaises(ValueError):
                    registry.load(path)
            path.write_text(json.dumps(dict(DATA, images=[ENTRY, ENTRY])))
            with self.assertRaises(ValueError):
                registry.load(path)

    def test_date_tag(self):
        self.assertEqual(registry.release_tag('26-11-01'), '26-11-01')
        for invalid in ('latest', '26-02-30', '2026-11-01', '../tag'):
            with self.assertRaises(ValueError):
                registry.release_tag(invalid)

    @patch.object(registry, 'inspect_manifest', return_value=RAW)
    def test_source_index_must_match_exact_bytes(self, inspect):
        registry.verify_source(ENTRY, 'oci-archive:bundle.tar:2024.6.0')
        inspect.return_value = RAW + ' '
        with self.assertRaises(ValueError):
            registry.verify_source(ENTRY, 'oci-archive:bundle.tar:2024.6.0')

    @patch.object(registry, 'run')
    def test_local_identity_supports_classic_and_containerd_docker(self, run):
        entry = dict(ENTRY, manifest_id='sha256:' + 'c' * 64, config_id='sha256:' + 'd' * 64)
        for identity in (entry['image_id'], entry['manifest_id'], entry['config_id']):
            run.return_value = json.dumps([dict(Id=identity, Os='linux', Architecture='amd64')])
            registry.check_local(entry, entry['local'])
        run.return_value = json.dumps([dict(Id=ID, Os='linux', Architecture='arm64')])
        with self.assertRaises(ValueError):
            registry.check_local(entry, entry['local'])

    @patch.object(registry.subprocess, 'run')
    @patch.object(registry, 'run')
    def test_pull_checks_identity_before_changing_local_alias(self, run, command):
        run.return_value = json.dumps([dict(Id='sha256:' + 'c' * 64, Os='linux', Architecture='amd64')])
        with self.assertRaises(ValueError):
            registry.pull(DATA)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][1], 'pull')
        run.return_value = json.dumps([dict(Id=ID, Os='linux', Architecture='amd64')])
        registry.pull(DATA)
        self.assertEqual(command.call_args.args[0], ['docker', 'tag', ENTRY['reference'], ENTRY['local']])

    @patch.object(registry.subprocess, 'run')
    @patch.object(registry, 'run')
    def test_existing_tag_is_immutable_and_auth_errors_are_not_missing(self, run, command):
        command.return_value = subprocess.CompletedProcess([], 0, RAW, '')
        run.return_value = DIGEST + '\n'
        self.assertEqual(registry.existing_reference(ENTRY, '26-11-01'), ENTRY['reference'])
        command.return_value = subprocess.CompletedProcess([], 0, '{}', '')
        with self.assertRaises(ValueError):
            registry.existing_reference(ENTRY, '26-11-01')
        command.return_value = subprocess.CompletedProcess([], 1, '', 'unauthorized')
        with self.assertRaises(RuntimeError):
            registry.existing_reference(ENTRY, '26-11-01')
        command.return_value = subprocess.CompletedProcess([], 1, '', 'manifest unknown')
        self.assertIsNone(registry.existing_reference(ENTRY, '26-11-01'))

    @patch.object(registry, 'verify_source')
    @patch.object(registry, 'existing_reference', return_value=None)
    @patch.object(registry.subprocess, 'run')
    def test_failed_publish_does_not_write_release_manifest(self, command, existing, verify):
        command.side_effect = subprocess.CalledProcessError(1, 'skopeo')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'dependencies.json'
            with self.assertRaises(subprocess.CalledProcessError):
                registry.publish(DATA, '26-11-01', path)
            self.assertFalse(path.exists())

    @patch.object(registry, 'verify_source')
    @patch.object(registry, 'existing_reference', return_value=None)
    @patch.object(registry.subprocess, 'run')
    def test_publish_records_registry_digest_and_preserves_image_identity(self, command, existing, verify):
        def copy(args, **kwargs):
            Path(args[args.index('--digestfile') + 1]).write_text(DIGEST)
        command.side_effect = copy
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'dependencies.json'
            registry.publish(DATA, '26-11-01', path)
            result = json.loads(path.read_text())
            self.assertEqual(result['release'], '26-11-01')
            self.assertEqual(result['images'][0]['image_id'], ID)
            self.assertEqual(result['images'][0]['reference'], ENTRY['reference'])
            self.assertIn('--preserve-digests', command.call_args.args[0])

    @patch.object(registry, 'verify_source')
    @patch.object(registry.subprocess, 'run')
    def test_public_check_does_not_use_registry_credentials(self, command, verify):
        registry.verify_public(DATA, download=True)
        self.assertTrue(verify.call_args.kwargs['anonymous'])
        self.assertIn('--src-no-creds', command.call_args.args[0])

    @patch.object(registry.subprocess, 'run')
    def test_bootstrap_rejects_corrupt_download(self, command):
        def download(args, **kwargs):
            Path(args[args.index('--dir') + 1], 'part1').write_bytes(b'bad')
        command.side_effect = download
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bootstrap.json'
            path.write_text(json.dumps(dict(release='26-11-01', archive_sha256='0' * 64,
                                            parts=[dict(name='part1', size=3, sha256='0' * 64)])))
            output = Path(temporary) / 'images.tar'
            with self.assertRaises(ValueError):
                registry.bootstrap(path, output)
            self.assertFalse(output.exists())

    @patch.object(registry.subprocess, 'run')
    @patch.object(registry, 'run')
    def test_release_asset_cannot_be_replaced(self, run, command):
        run.return_value = json.dumps(dict(assets=[dict(name='dependencies.json')]))
        def download(args, **kwargs):
            Path(args[args.index('--dir') + 1], 'dependencies.json').write_text('{}')
        command.side_effect = download
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'dependencies.json'
            data = dict(DATA, release='26-11-01')
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                registry.attach(data, path)
            self.assertNotIn('upload', command.call_args.args[0])
