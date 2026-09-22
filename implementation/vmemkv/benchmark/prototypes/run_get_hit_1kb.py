#!/usr/bin/env python3
"""1KB単件Getの2案を比較する小さい実験ランナー。MultiGetは対象外。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[4]
SOURCE = Path(__file__).with_name('get_hit_1kb.cpp')
BUILD = ROOT / 'build/vmemkv-gcc13'
OUT = ROOT / 'build/ltm/results/get-hit-prototypes-20260922'
DATA = ROOT / 'build/ltm/data/get-hit-prototypes-20260922'
BINARY = OUT / 'get_hit_1kb'
COUNT = 8259552
MASTER = ROOT / f'build/ltm/data/bench_VMemKV_Bloom_T1InlineValue.bin_v1024_n{COUNT}_master'


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def run_logged(command, dest):
    dest.mkdir(parents=True, exist_ok=False)
    save(dest / 'command.json', {'command': list(map(str, command)), 'cwd': str(ROOT),
         'environment': {k: v for k, v in os.environ.items() if k.startswith('VMEMKV_') or k == 'TMPDIR'}})
    start = time.monotonic()
    with (dest / 'console.log').open('w') as log:
        child = subprocess.run(list(map(str, command)), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    save(dest / 'exit.json', {'code': child.returncode, 'wall_seconds': time.monotonic() - start})
    print(f'{dest.name}: exit={child.returncode}', flush=True)
    print((dest / 'console.log').read_text().strip()[-1800:], flush=True)
    child.check_returncode()


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    command = ['g++-13', '-std=gnu++23', '-O3', '-DNDEBUG', '-DBENCHMARK_STATIC_DEFINE', '-pthread', '-Wall', '-Wextra',
        '-I' + str(ROOT / 'implementation/vmemkv/include'),
        '-I' + str(ROOT / 'implementation/vmemkv/src'),
        '-I' + str(BUILD / '_deps/google_benchmark-src/include'),
        str(SOURCE), str(BUILD / 'libvmemkv.a'),
        str(BUILD / '_deps/google_benchmark-build/src/libbenchmark.a'), '-ltbb', '-o', str(BINARY)]
    attempt = len(list(OUT.glob('build*'))) + 1
    run_logged(command, OUT / f'build-{attempt}')
    save(OUT / 'provenance.json', {
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'compiler': subprocess.check_output(['g++-13', '--version'], text=True).splitlines()[0],
        'host_environment_record': str(ROOT / 'build/ltm/results/get-hit-20260922/environment.txt'),
        'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in [SOURCE, Path(__file__), BINARY, BUILD / 'libvmemkv.a']},
        'keys': COUNT, 'key_bytes': 16, 'value_bytes': {'80%': 1024, '20%': 8},
        'zipf_alpha': 1.0, 'seed': 42, 'note': '専用harness内でA/B。旧bench_kvとの絶対値比較はしない。'})


def prepare():
    DATA.mkdir(parents=True, exist_ok=True)
    for layout in ['dense', 'padded']:
        run_logged([BINARY, 'prepare', MASTER, DATA / layout, layout], OUT / f'prepare-{layout}')


def verify():
    run_logged([BINARY, 'smoke', DATA / 'smoke'], OUT / 'smoke')
    for layout in ['dense', 'padded']:
        run_logged([BINARY, 'verify', DATA / layout, COUNT], OUT / f'verify-{layout}')


def one(label, layout, mode, dist, threads, warmup, seconds, resident=False):
    dest = OUT / label
    if dest.exists():
        if not (dest / 'case.json').exists():
            raise RuntimeError(f'{label}: incomplete previous run')
        print(f'{label}: already exists; skip', flush=True)
        return
    high, maximum = ((16 << 30, 32 << 30) if resident else (1 << 30, 2 << 30))
    command = ['systemd-run', '--user', '--scope', '--quiet',
               '-p', f'MemoryHigh={high}', '-p', f'MemoryMax={maximum}',
               '-p', 'MemorySwapMax=1099511627776', '--', BINARY, 'run', DATA / layout,
               mode, dist, threads, COUNT, warmup, seconds, dest, 'resident' if resident else 'ltm']
    run_logged(command, dest)
    save(dest / 'case.json', {'layout': layout, 'mode': mode, 'distribution': dist,
         'threads': threads, 'warmup_seconds': warmup, 'measurement_seconds': seconds,
         'resident': resident, 'memory_high': high, 'memory_max': maximum})
    for name, value in [('memory.high', high), ('memory.max', maximum)]:
        if int((dest / ('before.' + name)).read_text()) != value:
            raise RuntimeError(f'{label}: actual {name} differs')
    result = json.loads((dest / 'result.json').read_text())
    if any(result[k] for k in ['checkpoints', 'splits', 'defrag_cycles']):
        raise RuntimeError(f'{label}: background maintenance ran')


def matrix(warmup, seconds):
    choices = [('dense', 'baseline'), ('dense', 'cross-pread'), ('padded', 'baseline')]
    cells = [(layout, mode, dist, threads) for threads in [1, 16]
             for dist in ['Uniform', 'Zipf'] for layout, mode in choices]
    for rep in [1, 2]:
        for layout, mode, dist, threads in (cells if rep == 1 else list(reversed(cells))):
            label = f'ltm-r{rep}-{layout}-{mode}-{dist}-t{threads}'
            one(label, layout, mode, dist, threads, warmup, seconds)


def resident():
    choices = [('dense', 'baseline'), ('dense', 'cross-pread'), ('padded', 'baseline')]
    for rep in [1, 2]:
        for layout, mode in (choices if rep == 1 else list(reversed(choices))):
            one(f'resident-r{rep}-{layout}-{mode}-Uniform-t1', layout, mode, 'Uniform', 1, 2, 6, True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['build', 'prepare', 'verify', 'matrix', 'resident'])
    parser.add_argument('--warmup', type=float, default=12)
    parser.add_argument('--seconds', type=float, default=10)
    args = parser.parse_args()
    if os.environ.get('VMEMKV_DB_DIR') != str(ROOT / 'build/ltm/data'):
        raise SystemExit('最初に source build/ltm/env.sh を実行してください')
    if args.action == 'matrix':
        matrix(args.warmup, args.seconds)
    else:
        globals()[args.action]()
