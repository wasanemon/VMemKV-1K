#!/usr/bin/env python3
"""案1と両ページcold限定Direct I/Oの最小比較。"""
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
OUT = ROOT / 'build/ltm/results/cold-direct-1kb-20260923'
DB = ROOT / 'build/ltm/data/get-hit-prototypes-20260922/dense'
COUNT = 8259552
VARIANTS = ['proposal1', 'cold-direct']


def replace_once(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new)


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    assert (OUT / 'direct-probe.json').exists(), '先にprobe_direct_1kb.pyを実行'
    baseline = (SRC / 'src/vmemkv/read_path.hpp').read_text()
    assert 'if (is_small && size_hint <= kPageSize - offset % kPageSize)' in baseline
    for variant in VARIANTS:
        dest = OUT / 'source' / variant
        for folder in ['src', 'include']:
            shutil.copytree(SRC / folder, dest / folder, dirs_exist_ok=True)
        bench = replace_once((SRC / 'benchmark/bench_kv.cpp').read_text(),
            'using BenchmarkTypes = vmemkv::variants::AllPossibleTypes;',
            'using BenchmarkTypes = std::tuple<vmemkv::VMemKVStore>;')
        (dest / 'bench_kv.cpp').write_text(bench)
        # 既存の値照合部分だけ再利用し、A/B用fd設定・状態遷移テストは入れない。
        verify = (HERE / 'get_read_policy_verify.inc').read_text()
        verify = verify.replace('    read_policy_prototype::configure_fd(store.t2().get_memory()->read_fd);\n', '')
        verify = verify.replace('    read_policy_prototype::verify_policy();\n', '')
        verify = verify.replace('--verify-policy', '--verify').replace('; adaptive transitions', '')
        harness = replace_once((HERE / 'regression_throughput.cpp').read_text(),
                               '  const int threads =', verify + '\n  const int threads =')
        if variant == 'cold-direct':
            shutil.copy2(HERE / 'cold_direct_1kb.hpp', dest / 'src/cold_direct_1kb.hpp')
            harness = replace_once(harness, '    prepare_cache(store, false);',
                                  (HERE / 'cold_direct_verify.inc').read_text() + '\n    prepare_cache(store, false);')
            harness = replace_once(harness, '    prepare_cache(*store, resident);',
                                  '    cold_direct_prototype::configure(store->t2().get_memory()->read_fd);\n    prepare_cache(*store, resident);')
            text = replace_once(baseline, '#include "vmemkv/hooks.hpp"',
                                '#include "vmemkv/hooks.hpp"\n#include "cold_direct_1kb.hpp"')
            text = replace_once(text,
                '                                          uint64_t base_boundary) -> std::optional<T2RecordView> {',
                '                                          uint64_t base_boundary,\n                                          bool *both_cold = nullptr) -> std::optional<T2RecordView> {\n  if (both_cold) *both_cold = false;')
            text = replace_once(text,
                """  for (unsigned char page_status : tl_mincore_vec) {
    if ((page_status & 1) == 0) {
      return std::nullopt;  // Not resident -- let the caller's pread() fetch it instead.
    }
  }
""",
                """  bool all_resident = true;
  bool all_cold = true;
  for (unsigned char page_status : tl_mincore_vec) {
    all_resident &= (page_status & 1) != 0;
    all_cold &= (page_status & 1) == 0;
  }
  if (both_cold) *both_cold = tl_mincore_vec.size() == 2 && all_cold;
  if (!all_resident) return std::nullopt;
""")
            text = replace_once(text,
                """  if (mem->base_mmap_scan != nullptr) {
    if (auto resident = try_read_resident_base_record(mem->base_mmap_scan, offset, read_len, base_boundary);
        resident.has_value()) {
      return resident;
    }
  }
  if (mem->read_fd < 0 || cold_buf == nullptr) {""",
                """  bool both_cold = false;
  if (mem->base_mmap_scan != nullptr) {
    if (auto resident = try_read_resident_base_record(mem->base_mmap_scan, offset, read_len, base_boundary, &both_cold);
        resident.has_value()) {
      return resident;
    }
  }
  if (size_hint <= 4096 && both_cold) {
    if (auto direct = cold_direct_prototype::read(offset, read_len, base_boundary); direct.has_value())
      return direct;
  }
  if (mem->read_fd < 0 || cold_buf == nullptr) {""")
            (dest / 'src/vmemkv/read_path.hpp').write_text(text)
            (OUT / 'cold-direct.patch').write_text(''.join(difflib.unified_diff(
                baseline.splitlines(True), text.splitlines(True), fromfile='proposal1/read_path.hpp',
                tofile='cold-direct/read_path.hpp')))
        (dest / 'cold_direct_benchmark.cpp').write_text(harness)
        command = ['g++-13', '-std=gnu++23', '-O3', '-DNDEBUG', '-DBENCHMARK_STATIC_DEFINE', '-pthread',
                   '-I' + str(dest / 'include'), '-I' + str(dest / 'src'),
                   '-I' + str(BUILD / '_deps/google_benchmark-src/include'),
                   str(dest / 'cold_direct_benchmark.cpp'), str(BUILD / 'libvmemkv.a'),
                   str(BUILD / '_deps/google_benchmark-build/src/libbenchmark.a'), '-ltbb', '-o', str(OUT / variant)]
        runner.save(OUT / f'build-{variant}.json', command)
        with (OUT / f'build-{variant}.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        print('built ' + variant, flush=True)
    (OUT / 'proposal1-working-tree.patch').write_text(subprocess.check_output(
        ['git', 'diff', 'main', '--', 'implementation/vmemkv/src', 'implementation/vmemkv/include'], text=True))
    files = [Path(__file__), HERE / 'regression_throughput.cpp', HERE / 'run_regression_throughput.py',
             HERE / 'get_read_policy_verify.inc', HERE / 'cold_direct_1kb.hpp',
             HERE / 'cold_direct_verify.inc', HERE / 'probe_direct_1kb.py', BUILD / 'libvmemkv.a'] + [OUT / v for v in VARIANTS]
    runner.save(OUT / 'provenance.json', {
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip(),
        'compiler': subprocess.check_output(['g++-13', '--version'], text=True).splitlines()[0],
        'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'note': '案1適用済み作業ツリーのコピーと両ページcold限定DIO。通常ソースは案1のみ。固定pread・A/Bなし。'})


def verify():
    for variant in VARIANTS:
        command = [str(OUT / variant), '--verify']
        env = runner.environment(DB, 1024, COUNT)
        runner.save(OUT / f'verify-{variant}-command.json', {'command': command,
            'environment': {k: v for k, v in env.items() if k.startswith(('REGRESSION_', 'VMEMKV_'))}})
        with (OUT / f'verify-{variant}.log').open('w') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        print(variant + ': ' + (OUT / f'verify-{variant}.log').read_text().strip(), flush=True)


def matrix(threads):
    runner.OUT = OUT
    runner.save(OUT / 'datasets.json', {'1024': {'path': str(DB), 'count': COUNT, 't2_bytes': 7030530024}})
    cpus = '0' if threads == 1 else ','.join(str(i) for i in range(0, 32, 2))
    assert set(map(int, cpus.split(','))).issubset(os.sched_getaffinity(0))
    runner.save(OUT / f'plan-t{threads}.json', {'threads': threads, 'distributions': ['Uniform', 'Zipf'],
        'cpu': cpus, 'repetitions': 2, 'runs': 8, 'benchmark_min_time_seconds': 2, 'metric': 'throughput'})
    for distribution in ['Uniform', 'Zipf']:
        for rep in [1, 2]:
            for variant in (VARIANTS if rep == 1 else VARIANTS[::-1]):
                runner.one(('ltm', 1024, 'Get-Hit', distribution, threads), variant, rep, seconds=2, cpus=cpus)
    summarize()


def summarize():
    groups = {}
    for p in OUT.glob('r*/throughput.json'):
        d = json.loads(p.read_text())
        groups.setdefault((d['threads'], d['distribution']), {}).setdefault(d['variant'], []).append(d['ops_per_second'])
    rows = []
    for (threads, dist), raw in sorted(groups.items()):
        assert all(len(raw[v]) == 2 for v in VARIANTS)
        a, b = (statistics.mean(raw[v]) for v in VARIANTS)
        rows.append(dict(threads=threads, distribution=dist, proposal1=a, cold_direct=b,
                         change_percent=(b/a-1)*100, raw=raw))
    runner.save(OUT / 'summary.json', rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['build', 'verify', 'matrix', 'summarize'])
    parser.add_argument('--threads', type=int, choices=[1, 16], default=1)
    args = parser.parse_args()
    if os.environ.get('VMEMKV_DB_DIR') != str(ROOT / 'build/ltm/data'):
        raise SystemExit('最初に source build/ltm/env.sh を実行してください')
    if args.action == 'matrix': matrix(args.threads)
    else: globals()[args.action]()
