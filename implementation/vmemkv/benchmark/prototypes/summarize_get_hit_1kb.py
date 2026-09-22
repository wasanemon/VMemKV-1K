#!/usr/bin/env python3
"""単件Get試作の保存済み結果を集計する。測定は行わない。"""
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'build/ltm/results/get-hit-prototypes-20260922'


def pairs(path):
    return {k: int(v) for k, v in (line.split() for line in path.read_text().splitlines())}


cases = []
for path in sorted(OUT.glob('*/case.json')):
    case = json.loads(path.read_text())
    result = json.loads(path.with_name('result.json').read_text())
    memory = pairs(path.with_name('after.memory.stat'))
    before_memory = pairs(path.with_name('before.memory.stat'))
    ops = result['operations']
    result.update(label=path.parent.name,
                  variant='padded' if case['layout'] == 'padded' else case['mode'],
                  read_bytes_per_get=result['read_bytes'] / ops,
                  major_faults_per_get=result['major_faults'] / ops,
                  cpu_per_thread_pct=100 * result['cpu_seconds'] / result['elapsed_seconds'] / case['threads'],
                  file_mib=memory['file'] / 2**20,
                  before_file_mib=before_memory['file'] / 2**20,
                  anon_mib=memory['anon'] / 2**20,
                  swap_mib=int(path.with_name('after.memory.swap.current').read_text()) / 2**20)
    cases.append(case | result)

groups = {}
for case in cases:
    key = ('resident' if case['resident'] else 'ltm', case['distribution'], case['threads'], case['variant'])
    groups.setdefault(key, []).append(case)

rows = []
for key, group in sorted(groups.items()):
    scenario, dist, threads, variant = key
    row = dict(scenario=scenario, distribution=dist, threads=threads, variant=variant, repetitions=len(group))
    for metric in ['ops_per_second', 'read_bytes_per_get', 'major_faults_per_get', 'cpu_per_thread_pct',
                   'file_mib', 'before_file_mib', 'anon_mib', 'swap_mib']:
        row[metric] = statistics.mean(x[metric] for x in group)
    row['ops_min'] = min(x['ops_per_second'] for x in group)
    row['ops_max'] = max(x['ops_per_second'] for x in group)
    control = groups.get((scenario, dist, threads, 'baseline'))
    if control:
        row['relative_to_baseline'] = row['ops_per_second'] / statistics.mean(x['ops_per_second'] for x in control)
    rows.append(row)

(OUT / 'summary.json').write_text(json.dumps({'cases': cases, 'summary': rows}, indent=2, ensure_ascii=False) + '\n')
lines = ['| 条件 | 分布 | threads | 構成 | 回数 | 平均ops/s | 現行比 | 読込B/Get | major fault/Get |',
         '|---|---|---:|---|---:|---:|---:|---:|---:|']
for row in rows:
    lines.append(f"| {row['scenario']} | {row['distribution']} | {row['threads']} | {row['variant']} | "
                 f"{row['repetitions']} | {row['ops_per_second']:,.0f} | {row.get('relative_to_baseline', 0):.3f} | "
                 f"{row['read_bytes_per_get']:,.0f} | {row['major_faults_per_get']:.3f} |")
(OUT / 'summary.md').write_text('\n'.join(lines) + '\n')
print('\n'.join(lines))
