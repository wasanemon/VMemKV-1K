#!/usr/bin/env python3
"""準備済み密詰めcheckpointで単件GetとI/Oの対応を観測する。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'build/ltm/results/get-hit-io-20260922'
BINARY = OUT / 'inspect_get_hit_io'
SOURCE = Path(__file__).with_name('inspect_get_hit_io.cpp')
BUILD = ROOT / 'build/vmemkv-gcc13'


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    action = sys.argv[1]
    OUT.mkdir(exist_ok=True)
    if action == 'build':
        command = ['g++-13', '-std=gnu++23', '-O3', '-DNDEBUG', '-DBENCHMARK_STATIC_DEFINE',
                   '-pthread', '-Wall', '-Wextra', '-I' + str(ROOT / 'implementation/vmemkv/include'),
                   '-I' + str(ROOT / 'implementation/vmemkv/src'),
                   '-I' + str(BUILD / '_deps/google_benchmark-src/include'), str(SOURCE),
                   str(BUILD / 'libvmemkv.a'), str(BUILD / '_deps/google_benchmark-build/src/libbenchmark.a'),
                   '-ltbb', '-o', str(BINARY)]
        save(OUT / 'build-command.json', command)
        with (OUT / 'build.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        save(OUT / 'provenance.json', {
            'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'compiler': subprocess.check_output(['g++-13', '--version'], text=True).splitlines()[0],
            'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in [SOURCE, SOURCE.with_name('get_hit_1kb.cpp'), BINARY, BUILD / 'libvmemkv.a']}})
    elif action in ['stats', 'trace']:
        if os.environ.get('VMEMKV_DB_DIR') != str(ROOT / 'build/ltm/data'):
            raise SystemExit('source build/ltm/env.sh が必要です')
        dest = OUT / action
        dest.mkdir(exist_ok=False)
        command = ['systemd-run', '--user', '--scope', '--quiet', '-p', 'MemoryHigh=1073741824',
                   '-p', 'MemoryMax=2147483648', '-p', 'MemorySwapMax=1099511627776', '--',
                   str(BINARY), str(ROOT / 'build/ltm/data/get-hit-prototypes-20260922/dense'),
                   str(dest), '64', 'wait' if action == 'trace' else 'now']
        save(dest / 'command.json', {'command': command, 'environment': {
            k: v for k, v in os.environ.items() if k.startswith('VMEMKV_') or k == 'TMPDIR'}})
        with (dest / 'console.log').open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        save(dest / 'exit.json', {'code': result.returncode})
        print((dest / 'console.log').read_text())
        result.check_returncode()
    else:
        raise SystemExit('build | stats | trace')
