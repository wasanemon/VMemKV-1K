#!/usr/bin/env python3
"""常駐Getの4条件だけ。元実装・案1・同一案1バイナリのmincore省略を比較。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess

import run_fixed_pread_1kb as base

r = base.runner
HERE = Path(__file__).resolve().parent
OUT = base.ROOT / 'build/ltm/results/resident-mincore-diagnosis-20260923'
VARIANTS = ['original', 'proposal1', 'mincore-stub']


def build():
    OUT.mkdir(exist_ok=True)
    original = OUT / 'source/original'
    shutil.copytree(base.OUT / 'source/proposal1', original, dirs_exist_ok=True)
    header = original / 'src/vmemkv/read_path.hpp'
    text = header.read_text()
    old = 'if (is_small && size_hint <= kPageSize - offset % kPageSize)'
    assert text.count(old) == 1
    header.write_text(text.replace(old, 'if (is_small)'))
    cmd = json.loads((base.OUT / 'build-proposal1.json').read_text())
    cmd = [s.replace(str(base.OUT / 'source/proposal1'), str(original)) for s in cmd]
    cmd[-1] = str(OUT / 'original')
    r.save(OUT / 'build-original-command.json', cmd)
    with (OUT / 'build-original.log').open('w') as log:
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=True)
    stub = HERE / 'resident_mincore_stub.c'
    cmd = ['gcc-13', '-O2', '-shared', '-fPIC', str(stub), '-o', str(OUT / 'mincore-stub.so')]
    r.save(OUT / 'build-stub-command.json', cmd)
    subprocess.run(cmd, check=True)
    for name in ['proposal1', 'mincore-stub']:
        link = OUT / name
        if not link.exists(): link.symlink_to(base.OUT / 'proposal1')
    previous = json.loads((base.OUT / 'provenance.json').read_text())
    key = str((base.OUT / 'proposal1').relative_to(base.ROOT))
    assert hashlib.sha256((base.OUT / 'proposal1').read_bytes()).hexdigest() == previous['sha256'][key]
    files = [OUT / v for v in VARIANTS] + [OUT / 'mincore-stub.so', stub, Path(__file__)]
    r.save(OUT / 'provenance.json', {'commit': previous['commit'],
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip(),
        'source_provenance': str(base.OUT / 'provenance.json'),
        'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'note': '案1とstubは同じバイナリ。stubはLD_PRELOADでmincoreだけ置換する診断用、製品化不可。'})
    print('built original and diagnostic mincore interposer', flush=True)


def matrix():
    r.OUT = OUT
    r.save(OUT / 'datasets.json', {'1024': {'path': str(base.DB), 'count': base.COUNT, 't2_bytes': 7030530024}})
    cells = [('resident', 1024, 'Get-Hit', dist, threads)
             for threads in [1, 16] for dist in ['Uniform', 'Zipf']]
    cpus = ','.join(str(i) for i in range(0, 32, 2))
    assert set(range(0, 32, 2)).issubset(os.sched_getaffinity(0))
    r.save(OUT / 'plan.json', {'cells': cells, 'variants': VARIANTS, 'repetitions': 2,
        'total_runs': 24, 'min_time_seconds': 5, 'cpu_affinity_all_cases': cpus,
        'note': '元の退行追加比較と同じCPU集合・min_time。全T2とmmapを事前に温める。'})
    environment = r.environment
    for cell in cells:
        for rep in [1, 2]:
            for variant in (VARIANTS if rep == 1 else VARIANTS[::-1]):
                def env(*args, **kwargs):
                    values = environment(*args, **kwargs)
                    values.pop('LD_PRELOAD', None)
                    if variant == 'mincore-stub': values['LD_PRELOAD'] = str(OUT / 'mincore-stub.so')
                    return values
                r.environment = env
                r.one(cell, variant, rep, seconds=5, cpus=cpus)
                dest = OUT / f'r{rep}-{r.label(cell)}-{variant}-pinned'
                r.save(dest / 'diagnostic-environment.json', {'LD_PRELOAD': str(OUT / 'mincore-stub.so') if variant == 'mincore-stub' else None})
                if variant == 'mincore-stub':
                    assert 'diagnostic: mincore replaced' in (dest / 'console.log').read_text()
    rows = []
    for cell in cells:
        raw = {v: [json.loads((OUT / f'r{rep}-{r.label(cell)}-{v}-pinned/throughput.json').read_text())['ops_per_second']
                   for rep in [1, 2]] for v in VARIANTS}
        means = {v: statistics.mean(raw[v]) for v in VARIANTS}
        rows.append({'distribution': cell[3], 'threads': cell[4], 'mean': means, 'raw': raw,
                     'proposal1_vs_original_percent': (means['proposal1']/means['original']-1)*100,
                     'stub_vs_original_percent': (means['mincore-stub']/means['original']-1)*100,
                     'stub_vs_proposal1_percent': (means['mincore-stub']/means['proposal1']-1)*100})
    r.save(OUT / 'summary.json', rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['build', 'matrix'])
    args = parser.parse_args()
    if os.environ.get('VMEMKV_DB_DIR') != str(base.ROOT / 'build/ltm/data'):
        raise SystemExit('最初に source build/ltm/env.sh を実行してください')
    globals()[args.action]()
