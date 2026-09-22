#!/usr/bin/env python3
"""未コミットの案1を基準にA/Bを独立比較。既存ランナーとデータを再利用。"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess

import run_regression_throughput as runner

ROOT, SRC, BUILD = runner.ROOT, runner.SRC, runner.BUILD
HERE = Path(__file__).resolve().parent
OUT = ROOT / 'build/ltm/results/get-read-policy-20260922'
VARIANTS = ['proposal1', 'random-fd', 'adaptive']
DB = ROOT / 'build/ltm/data/get-hit-prototypes-20260922/dense'
COUNT = 8259552


def replace_once(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new)


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    baseline = (SRC / 'src/vmemkv/read_path.hpp').read_text()
    assert 'if (is_small && size_hint <= kPageSize - offset % kPageSize)' in baseline
    for variant in VARIANTS:
        dest = OUT / 'source' / variant
        for folder in ['src', 'include']:
            shutil.copytree(SRC / folder, dest / folder, dirs_exist_ok=True)
        shutil.copy2(HERE / 'get_read_policy.hpp', dest / 'src/vmemkv/get_read_policy.hpp')
        bench = (SRC / 'benchmark/bench_kv.cpp').read_text()
        bench = replace_once(bench, 'using BenchmarkTypes = vmemkv::variants::AllPossibleTypes;',
                             'using BenchmarkTypes = std::tuple<vmemkv::VMemKVStore>;')
        (dest / 'bench_kv.cpp').write_text(bench)
        harness = (HERE / 'regression_throughput.cpp').read_text()
        harness = '#include "src/vmemkv/get_read_policy.hpp"\n' + harness
        harness = replace_once(harness, '    prepare_cache(*store, resident);',
            '    read_policy_prototype::configure_fd(store->t2().get_memory()->read_fd);\n'
            '    prepare_cache(*store, resident);')
        harness = replace_once(harness, '  const int threads =',
            (HERE / 'get_read_policy_verify.inc').read_text() + '\n  const int threads =')
        (dest / 'policy_benchmark.cpp').write_text(harness)
        if variant == 'adaptive':
            text = replace_once(baseline, '#pragma once', '#pragma once\n#include "get_read_policy.hpp"')
            text = replace_once(text,
                '''  const uint64_t read_len = std::min(size_hint, base_boundary - offset);
  if (mem->base_mmap_scan != nullptr) {
    if (auto resident = try_read_resident_base_record(mem->base_mmap_scan, offset, read_len, base_boundary);
        resident.has_value()) {
      return resident;
    }
  }''',
                '''  const uint64_t read_len = std::min(size_hint, base_boundary - offset);
  read_policy_prototype::ResidencyPolicy *policy = nullptr;
  if (size_hint <= 4096 && size_hint > 4096 - offset % 4096) {
    policy = &read_policy_prototype::policy_for(mem);
  }
  const bool probe = policy == nullptr || policy->probe();
  if (!probe && policy->mode == read_policy_prototype::ResidencyPolicy::Mmap) {
    return read_base_record_via(mem->base_mmap_scan, offset, base_boundary);
  }
  if (probe) {
    const auto resident = mem->base_mmap_scan != nullptr
        ? try_read_resident_base_record(mem->base_mmap_scan, offset, read_len, base_boundary)
        : std::nullopt;
    if (policy != nullptr) policy->observe(resident.has_value());
    if (resident.has_value()) return resident;
  }''')
            (dest / 'src/vmemkv/read_path.hpp').write_text(text)
            (OUT / 'adaptive.patch').write_text(''.join(difflib.unified_diff(
                baseline.splitlines(True), text.splitlines(True), fromfile='proposal1/read_path.hpp',
                tofile='adaptive/read_path.hpp')))
        command = ['g++-13', '-std=gnu++23', '-O3', '-DNDEBUG', '-DBENCHMARK_STATIC_DEFINE', '-pthread',
                   *(['-DGET_READ_RANDOM_FD'] if variant == 'random-fd' else []),
                   '-I' + str(dest / 'include'), '-I' + str(dest / 'src'),
                   '-I' + str(BUILD / '_deps/google_benchmark-src/include'),
                   str(dest / 'policy_benchmark.cpp'), str(BUILD / 'libvmemkv.a'),
                   str(BUILD / '_deps/google_benchmark-build/src/libbenchmark.a'), '-ltbb', '-o', str(OUT / variant)]
        runner.save(OUT / f'build-{variant}.json', command)
        with (OUT / f'build-{variant}.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        print('built ' + variant, flush=True)
    (OUT / 'proposal1-working-tree.patch').write_text(subprocess.check_output(
        ['git', 'diff', 'main', '--', 'implementation/vmemkv/src', 'implementation/vmemkv/include'], text=True))
    files = [HERE / name for name in ['get_read_policy.hpp', 'get_read_policy_verify.inc',
             'run_get_read_policy.py', 'regression_throughput.cpp', 'run_regression_throughput.py']]
    files += [OUT / v for v in VARIANTS] + [BUILD / 'libvmemkv.a']
    runner.save(OUT / 'provenance.json', {
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip(),
        'compiler': subprocess.check_output(['g++-13', '--version'], text=True).splitlines()[0],
        'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'note': 'mainの未コミット作業ツリーへ案1を適用し、それを複製した3版。AとBは独立。'})


def verify():
    for variant in VARIANTS:
        command = [str(OUT / variant), '--verify-policy']
        env = runner.environment(DB, 1024, COUNT)
        runner.save(OUT / f'verify-{variant}-command.json', {'command': command,
            'environment': {k: v for k, v in env.items() if k.startswith(('REGRESSION_', 'VMEMKV_'))}})
        with (OUT / f'verify-{variant}.log').open('w') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        print(variant + ': ' + (OUT / f'verify-{variant}.log').read_text().strip(), flush=True)


def matrix():
    cpu = str(min(os.sched_getaffinity(0)))
    plan = [('A', 'random-fd', [('ltm', 'Uniform')]),
            ('B', 'adaptive', [('ltm', 'Uniform'), ('resident', 'Uniform'), ('ltm', 'Zipf')])]
    runner.save(OUT / 'plan.json', {'comparisons': plan, 'cpu': cpu, 'repetitions': 2,
                                   'benchmark_min_time_seconds': 2, 'total_runs': 16})
    for stage, candidate, conditions in plan:
        runner.OUT = OUT / stage
        runner.OUT.mkdir(exist_ok=True)
        for variant in ['proposal1', candidate]:
            link = runner.OUT / variant
            if not link.exists(): link.symlink_to(OUT / variant)
        runner.save(runner.OUT / 'datasets.json', {'1024': {'path': str(DB), 'count': COUNT, 't2_bytes': 7030530024}})
        for scenario, distribution in conditions:
            cell = (scenario, 1024, 'Get-Hit', distribution, 1)
            for repetition in [1, 2]:
                order = ['proposal1', candidate] if repetition == 1 else [candidate, 'proposal1']
                for variant in order:
                    runner.one(cell, variant, repetition, seconds=2, cpus=cpu)
    summarize()


def summarize():
    groups = {}
    for p in OUT.glob('*/r*/throughput.json'):
        d = json.loads(p.read_text())
        key = (p.parent.parent.name, d['scenario'], d['distribution'])
        groups.setdefault(key, {}).setdefault(d['variant'], []).append(d['ops_per_second'])
    rows = []
    for (stage, scenario, dist), group in sorted(groups.items()):
        candidate = 'random-fd' if stage == 'A' else 'adaptive'
        assert len(group['proposal1']) == len(group[candidate]) == 2
        a, b = statistics.mean(group['proposal1']), statistics.mean(group[candidate])
        rows.append(dict(stage=stage, scenario=scenario, distribution=dist, baseline=a, candidate=b,
                         change_percent=(b/a-1)*100, raw=group))
    runner.save(OUT / 'summary.json', rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['build', 'verify', 'matrix', 'summarize'])
    args = parser.parse_args()
    if os.environ.get('VMEMKV_DB_DIR') != str(ROOT / 'build/ltm/data'):
        raise SystemExit('最初に source build/ltm/env.sh を実行してください')
    globals()[args.action]()
