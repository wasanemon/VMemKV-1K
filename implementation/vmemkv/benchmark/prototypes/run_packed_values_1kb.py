#!/usr/bin/env python3
"""値・メタデータ分離の最小比較。Uniform 1/16スレッド、各版2回だけ。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess

from run_get_hit_1kb import ROOT, BUILD, COUNT, run_logged, save

HERE = Path(__file__).resolve().parent
OUT = ROOT / 'build/ltm/results/packed-values-1kb-20260922'
DATA = ROOT / 'build/ltm/data/packed-values-1kb-20260922'
SOURCE_DB = ROOT / 'build/ltm/data/get-hit-prototypes-20260922/dense'
BINARY = OUT / 'packed_values_1kb'
VALUES = DATA / 'values.bin'
METADATA = DATA / 'metadata.bin'


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    command = ['g++-13', '-std=gnu++23', '-O3', '-DNDEBUG', '-DBENCHMARK_STATIC_DEFINE', '-pthread',
               '-I' + str(ROOT / 'implementation/vmemkv/include'),
               '-I' + str(ROOT / 'implementation/vmemkv/src'),
               '-I' + str(BUILD / '_deps/google_benchmark-src/include'),
               str(HERE / 'packed_values_1kb.cpp'), str(BUILD / 'libvmemkv.a'),
               str(BUILD / '_deps/google_benchmark-build/src/libbenchmark.a'), '-ltbb', '-o', str(BINARY)]
    run_logged(command, OUT / 'build')
    save(OUT / 'provenance.json', {
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'compiler': subprocess.check_output(['g++-13', '--version'], text=True).splitlines()[0],
        'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                   [Path(__file__), HERE / 'packed_values_1kb.cpp', HERE / 'get_hit_1kb.cpp',
                    HERE / 'run_get_hit_1kb.py', BINARY, BUILD / 'libvmemkv.a']},
        'source': str(SOURCE_DB), 'keys': COUNT, 'key_bytes': 16, 'value_bytes': {'80%': 1024, '20%': 8},
        'distribution': 'Uniform', 'warmup_seconds': 12, 'measurement_seconds': 10,
        'note': '同一T1・生成器・seed・全バイト走査。固定checkpoint限定。製品コード変更なし。'})
    (OUT / 'product-source.diff').write_text(subprocess.check_output(
        ['git', 'diff', 'HEAD', '--', 'implementation/vmemkv/src', 'implementation/vmemkv/include'],
        cwd=ROOT, text=True))


def prepare():
    DATA.mkdir(parents=True, exist_ok=True)
    run_logged([BINARY, 'prepare', SOURCE_DB, VALUES, METADATA], OUT / 'prepare')
    data = json.loads((OUT / 'prepare/console.log').read_text().strip().splitlines()[-1])
    data['packed_total_bytes'] = VALUES.stat().st_size + METADATA.stat().st_size
    data['packed_allocated_bytes'] = sum(p.stat().st_blocks * 512 for p in [VALUES, METADATA])
    data['dense_allocated_bytes'] = Path(str(SOURCE_DB) + '.t2chk').stat().st_blocks * 512
    data['note'] = 'T1は共通。元の密詰めDBは比較元・T1読込用に別途保持し、変換後容量には重複計上しない。'
    save(OUT / 'layout.json', data)


def verify():
    run_logged([BINARY, 'verify', SOURCE_DB, VALUES, COUNT], OUT / 'verify')


def matrix():
    for threads in [1, 16]:
        for repetition in [1, 2]:
            for mode in (['baseline', 'packed'] if repetition == 1 else ['packed', 'baseline']):
                dest = OUT / f'r{repetition}-{mode}-t{threads}'
                command = ['systemd-run', '--user', '--scope', '--quiet',
                           '-p', 'MemoryHigh=1073741824', '-p', 'MemoryMax=2147483648',
                           '-p', 'MemorySwapMax=1099511627776', '--',
                           BINARY, 'run', SOURCE_DB, VALUES, COUNT, mode, threads, dest]
                run_logged(command, dest)
                save(dest / 'case.json', {'variant': mode, 'threads': threads, 'repetition': repetition})
    summarize()


def summarize():
    rows = []
    for threads in [1, 16]:
        raw = {mode: [json.loads((OUT / f'r{r}-{mode}-t{threads}/result.json').read_text())['ops_per_second']
                      for r in [1, 2]] for mode in ['baseline', 'packed']}
        a, b = (statistics.mean(raw[m]) for m in ['baseline', 'packed'])
        rows.append({'threads': threads, 'baseline': a, 'packed': b,
                     'change_percent': (b/a-1)*100, 'raw': raw})
    save(OUT / 'summary.json', rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['build', 'prepare', 'verify', 'matrix', 'summarize'])
    args = parser.parse_args()
    if os.environ.get('VMEMKV_DB_DIR') != str(ROOT / 'build/ltm/data'):
        raise SystemExit('最初に source build/ltm/env.sh を実行してください')
    globals()[args.action]()
