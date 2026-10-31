#!/usr/bin/env python3
"""Run the same no-GPU tests locally and on GitHub."""
import os
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1];os.chdir(root)
out=root/'.coverage-reports';out.mkdir(exist_ok=True)
env=dict(os.environ,PYTHONPATH=str(root/'docker/auto_aim'),COVERAGE_FILE=str(out/'.coverage'))
# The legacy research controllers remain separately tested; report all production
# benchmark/service code, including uncovered modules, without exclusions to inflate coverage.
for i,command in enumerate([['-m','unittest','discover','-s','tests'],['-m','benchmark.checks']]):
 subprocess.run([sys.executable,'-m','coverage','run',*(['--append'] if i else []),'--branch','--source=docker/auto_aim/benchmark,docker/auto_aim/pr_service','--omit=*/checks.py',*command],env=env,check=True)
for args in (['json','-o',str(out/'coverage.json')],['html','-d',str(out/'html')],['report','-m']):subprocess.run([sys.executable,'-m','coverage',*args],env=env,check=True)
import json
summary=json.loads((out/'coverage.json').read_text())['totals']
lines=100*summary['covered_lines']/summary['num_statements']
branches=100*summary['covered_branches']/summary['num_branches'] if summary['num_branches'] else 100
print(f'Coverage gate: statements={lines:.2f}%, branches={branches:.2f}%')
if lines<80 or branches<70:raise SystemExit('Coverage requires statements >=80% and branches >=70%')
