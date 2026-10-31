#!/usr/bin/env python3
"""Read-only checkout identity check; never updates source or fetches branches."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('sp_vision_25', 'vision_simulator_26')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()


def main():
    results = []
    for name in NAMES:
        row = dict(repository=name, initialized=False)
        try:
            entry = git(ROOT, 'ls-tree', 'HEAD', '--', name).split()
            if len(entry) < 3 or entry[0] != '160000':
                raise ValueError('Main commit does not record this submodule')
            row['recorded_commit'] = entry[2]
            path = ROOT / name
            if not (path / '.git').exists():
                raise ValueError('Submodule is not initialized; use git submodule update --init --recursive')
            row.update(initialized=True, actual_commit=git(path, 'rev-parse', 'HEAD'),
                       branch=git(path, 'branch', '--show-current') or '(detached; normal for pinned checkout)',
                       changes=git(path, 'status', '--porcelain'))
            row['matches_parent'] = row['actual_commit'] == row['recorded_commit']
            row['passed'] = row['matches_parent'] and not row['changes']
        except (subprocess.CalledProcessError, ValueError) as error:
            row.update(passed=False, error=str(error))
        results.append(row)
    passed = all(row['passed'] for row in results)
    print(json.dumps(dict(passed=passed, repositories=results), ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
