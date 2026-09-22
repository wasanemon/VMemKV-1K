#!/usr/bin/env python3
"""Bの追加3条件だけ。既存バイナリを再利用し、各版2回で終了する。"""
import hashlib
import json
import os
from pathlib import Path
import statistics

import run_get_read_policy as base

r = base.runner
OUT = base.OUT / 'B-extra'
DATA = base.ROOT / 'build/ltm/data/get-read-policy-extra-20260922'


def main():
    if os.environ.get('VMEMKV_DB_DIR') != str(base.ROOT / 'build/ltm/data'):
        raise SystemExit('最初に source build/ltm/env.sh を実行してください')
    OUT.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    provenance = json.loads((base.OUT / 'provenance.json').read_text())
    hashes = {}
    for variant in ['proposal1', 'adaptive']:
        binary = base.OUT / variant
        key = str(binary.relative_to(base.ROOT))
        hashes[key] = hashlib.sha256(binary.read_bytes()).hexdigest()
        assert hashes[key] == provenance['sha256'][key], 'original binary changed'
        link = OUT / variant
        if not link.exists(): link.symlink_to(binary)
    cpus16 = ','.join(str(i) for i in range(0, 32, 2))
    assert set(range(0, 32, 2)).issubset(os.sched_getaffinity(0))
    cells = [('resident', 1024, 'Get-Hit', 'Zipf', 1),
             ('ltm', 1024, 'Get-Hit', 'Uniform', 16),
             ('ltm', 1024, 'GetUpdate50', 'Zipf', 1)]
    r.save(OUT / 'plan.json', {'cells': cells, 'repetitions': 2, 'total_runs': 12,
        'metric': 'throughput', 'benchmark_min_time_seconds': 2, 'cpu_1': '0', 'cpu_16': cpus16,
        'binary_sha256': hashes, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_provenance': str(base.OUT / 'provenance.json'),
        'note': '既存A/Bバイナリのまま。混在は各回新しい専用checkpointコピーを使用。'})
    r.save(OUT / 'datasets.json', {'1024': {'path': str(base.DB), 'count': base.COUNT, 't2_bytes': 7030530024}})
    r.OUT, r.DATA = OUT, DATA
    for cell in cells:
        for rep in [1, 2]:
            for variant in (['proposal1', 'adaptive'] if rep == 1 else ['adaptive', 'proposal1']):
                r.one(cell, variant, rep, seconds=2, cpus=cpus16 if cell[-1] == 16 else '0')
    rows = []
    for cell in cells:
        raw = {v: [json.loads((OUT / f'r{rep}-{r.label(cell)}-{v}-pinned/throughput.json').read_text())['ops_per_second']
                   for rep in [1, 2]] for v in ['proposal1', 'adaptive']}
        a, b = (statistics.mean(raw[v]) for v in ['proposal1', 'adaptive'])
        rows.append(dict(zip(['scenario', 'value_bytes', 'operation', 'distribution', 'threads'], cell),
                         baseline=a, adaptive=b, change_percent=(b/a-1)*100, raw=raw))
    r.save(OUT / 'summary.json', rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
