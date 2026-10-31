#!/usr/bin/env python3
"""Explicit offline dependency distribution for Ubuntu/NVIDIA workstations."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

IMAGES=('sp_vision_dev:2024.6.0','daedalus:rm26-algorithm','daedalus:rm26-input-contract-v2','daedalus:rm26-evaluator-input-contract-v2','daedalus:rm26-reference-yaw-corrected-20261029')


def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()


def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=('export','import','check'));p.add_argument('directory',type=Path);a=p.parse_args();root=a.directory.resolve()
 if a.action=='export':
  root.mkdir(parents=True,exist_ok=True)
  if (root/'images.tar').exists():raise ValueError('Use a new dependency output directory')
  identities={tag:json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id'] for tag in IMAGES}
  subprocess.run(['docker','save','-o',str(root/'images.tar'),*IMAGES],check=True)
  (root/'manifest.json').write_text(json.dumps(dict(version=1,architecture='linux/amd64',images=identities,archive_sha256=digest(root/'images.tar')),indent=2))
 else:
  manifest=json.loads((root/'manifest.json').read_text())
  if digest(root/'images.tar')!=manifest['archive_sha256']:raise ValueError('Dependency archive checksum mismatch')
  if a.action=='import':subprocess.run(['docker','load','-i',str(root/'images.tar')],check=True)
  for tag,expected in manifest['images'].items():
   actual=json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']
   if actual!=expected:raise ValueError('Image identity mismatch: '+tag)
 print('Dependency bundle '+a.action+' complete')

if __name__=='__main__':main()
