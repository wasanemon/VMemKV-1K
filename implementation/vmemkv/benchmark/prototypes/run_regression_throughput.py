#!/usr/bin/env python3
"""案1のthroughput比較。製品ツリーを変えず同じベンチ本体を2版で実行する。"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / 'implementation/vmemkv'
BUILD = ROOT / 'build/vmemkv-gcc13'
OUT = ROOT / 'build/ltm/results/throughput-regression-20260922'
DATA = ROOT / 'build/ltm/data/throughput-regression-20260922'
VARIANTS = ['baseline', 'cross-pread']


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def build():
    for variant in VARIANTS:
        dest = OUT / 'source' / variant
        for folder in ['src', 'include']:
            shutil.copytree(SRC / folder, dest / folder, dirs_exist_ok=True)
        bench = (SRC / 'benchmark/bench_kv.cpp').read_text()
        bench = bench.replace('using BenchmarkTypes = vmemkv::variants::AllPossibleTypes;',
                              'using BenchmarkTypes = std::tuple<vmemkv::VMemKVStore>;')
        (dest / 'bench_kv.cpp').write_text(bench)
        source = SRC / 'benchmark/prototypes/regression_throughput.cpp'
        shutil.copy2(source, dest / source.name)
        if variant == 'cross-pread':
            header = dest / 'src/vmemkv/read_path.hpp'
            old = 'case BaseReader::kGet:\n      if (is_small) {'
            new = 'case BaseReader::kGet:\n      if (is_small && size_hint <= kPageSize - offset % kPageSize) {'
            text = header.read_text()
            assert text.count(old) == 1
            header.write_text(text.replace(old, new))
        command = ['g++-13', '-std=gnu++23', '-O3', '-DNDEBUG', '-DBENCHMARK_STATIC_DEFINE',
                   '-pthread', '-Wall', '-Wextra', '-I' + str(dest / 'include'), '-I' + str(dest / 'src'),
                   '-I' + str(BUILD / '_deps/google_benchmark-src/include'), str(dest / source.name),
                   str(BUILD / 'libvmemkv.a'), str(BUILD / '_deps/google_benchmark-build/src/libbenchmark.a'),
                   '-ltbb', '-o', str(OUT / variant)]
        save(OUT / ('build-' + variant + '.json'), command)
        with (OUT / ('build-' + variant + '.log')).open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        print('built ' + variant, flush=True)
    files = [OUT / v for v in VARIANTS] + [Path(__file__), SRC / 'benchmark/prototypes/regression_throughput.cpp',
             SRC / 'benchmark/bench_kv.cpp', SRC / 'src/vmemkv/read_path.hpp', BUILD / 'libvmemkv.a']
    save(OUT / 'provenance.json', {
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'compiler': subprocess.check_output(['g++-13', '--version'], text=True).splitlines()[0],
        'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'note': '製品側の差分は実験用read_path.hppのGet分岐1箇所だけ。既存libvmemkv.aは両版で共通。'})


def environment(path, size, count, scenario='resident', threads=1):
    env = os.environ.copy()
    env.update(REGRESSION_DB=str(path), REGRESSION_VALUE=str(size), REGRESSION_COUNT=str(count),
               REGRESSION_SCENARIO=scenario, REGRESSION_THREADS=str(threads),
               VMEMKV_DB_DIR=str(DATA), VMEMKV_BENCH_RESULTS_DIR=str(OUT), VMEMKV_BENCH_SKIP_CLEANUP='1',
               VMEMKV_BENCH_LTM='1' if scenario == 'ltm' else '0',
               VMEMKV_CONTEXT_memory_budget_bytes=str(1 << 30),
               VMEMKV_BENCH_INSERT_TIME_BUDGET_SECONDS='3', VMEMKV_BENCH_DELETE_TIME_BUDGET_SECONDS='3')
    return env


def prepare():
    datasets = {}
    for size, count, path in [
        (1024, 8259552, ROOT / 'build/ltm/data/get-hit-prototypes-20260922/dense'),
        (8, 20000000, DATA / 'inline'), (65536, (8 << 30) // (16 + 65536), DATA / 'large')]:
        command = [str(OUT / 'baseline'), '--prepare']
        env = environment(path, size, count)
        save(OUT / f'prepare-{size}-command.json', {'command': command, 'environment': {
            k: v for k, v in env.items() if k.startswith(('REGRESSION_', 'VMEMKV_'))}})
        with (OUT / f'prepare-{size}.log').open('w') as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        result = json.loads((OUT / f'prepare-{size}.log').read_text().strip().splitlines()[-1])
        datasets[str(size)] = dict(result, path=str(path))
        print('prepared ' + str(size), flush=True)
    save(OUT / 'datasets.json', datasets)


def clone(source, target, used):
    # このランナーの作業コピーだけを置換。元checkpointは書き換えない。
    for path in target.parent.glob(target.name + '*'):
        if path.is_file():
            path.unlink()
    shutil.copy2(str(source) + '.manifest', str(target) + '.manifest')
    os.link(str(source) + '.t1chk', str(target) + '.t1chk')
    input_fd = os.open(str(source) + '.t2chk', os.O_RDONLY)
    output_fd = os.open(str(target) + '.t2chk', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        os.ftruncate(output_fd, os.fstat(input_fd).st_size)
        copied = 0
        while copied < used:
            n = os.copy_file_range(input_fd, output_fd, min(64 << 20, used - copied))
            if n <= 0:
                raise RuntimeError('short clone')
            copied += n
        os.fsync(output_fd)
    finally:
        os.close(input_fd)
        os.close(output_fd)


def cells():
    result = []
    for scenario, size in [('resident', 1024), ('ltm', 1024), ('resident', 8), ('ltm', 65536)]:
        for op, dist in [('Get-Hit', 'Uniform'), ('Get-Hit', 'Zipf'), ('Get-Miss', 'Zipf'),
                         ('Scan', 'Uniform'), ('Scan', 'Zipf')]:
            for threads in [1, 16]:
                result.append((scenario, size, op, dist, threads))
        if size == 1024:
            for op, dist in [('Insert', ''), ('Update', 'Zipf'), ('Delete', ''), ('GetUpdate50', 'Zipf')]:
                for threads in [1, 16]:
                    result.append((scenario, size, op, dist, threads))
            result.append((scenario, size, 'YCSB-E', 'Zipf', 96))
    return result


def label(cell):
    return '-'.join(map(str, cell))


def one(cell, variant, rep, seconds=2, cpus=None):
    scenario, size, op, dist, threads = cell
    dest = OUT / (f'r{rep}-{label(cell)}-{variant}' + ('-pinned' if cpus else ''))
    if (dest / 'throughput.json').exists():
        return
    dest.mkdir(exist_ok=False)
    dataset = json.loads((OUT / 'datasets.json').read_text())[str(size)]
    source = Path(dataset['path'])
    path = source
    if op in ['Insert', 'Update', 'Delete', 'YCSB-E', 'GetUpdate50']:
        path = DATA / 'working'
        if scenario == 'resident' and op == 'Insert':
            for old in DATA.glob('working*'):
                if old.is_file():
                    old.unlink()
        else:
            clone(source, path, dataset['t2_bytes'])
    env = environment(path, size, dataset['count'], scenario, threads)
    env['VMEMKV_BENCH_RESULTS_DIR'] = str(dest)
    operation = 'Get/Mode=' + op.split('-')[1] if op.startswith('Get-') else op
    pattern = '^Store=VMemKV/Variant=Bloom-T1InlineValue/Op=' + operation + '/'
    if dist:
        pattern += 'Dist=' + dist + '/'
    pattern += 'Value=.*threads:' + str(threads) + '$'
    high, maximum = ((16 << 30, 32 << 30) if scenario == 'resident' else (1 << 30, 2 << 30))
    unit = 'vmemkv-throughput-' + uuid.uuid4().hex[:12]
    # 制限の適用値だけを記録してから同じPIDでベンチを実行する。
    check_limits = '''import json,os,pathlib,sys
group=pathlib.Path('/sys/fs/cgroup') / pathlib.Path('/proc/self/cgroup').read_text().split('0::/')[1].strip()
actual={k:(group/k).read_text().strip() for k in ['memory.high','memory.max']}
actual['cpu_affinity']=sorted(os.sched_getaffinity(0))
pathlib.Path(sys.argv[1]).write_text(json.dumps(actual)+'\\n')
assert int(actual['memory.high'])==int(sys.argv[2]) and int(actual['memory.max'])==int(sys.argv[3])
os.execv(sys.argv[4],sys.argv[4:])'''
    launcher = ['/usr/bin/taskset', '-c', cpus] if cpus else []
    command = ['systemd-run', '--user', '--scope', '--quiet', '--unit=' + unit, '-p', f'MemoryHigh={high}',
               '-p', f'MemoryMax={maximum}', '-p', 'MemorySwapMax=1099511627776', '--',
               *launcher, sys.executable, '-c', check_limits, str(dest / 'memory-limits.json'), str(high), str(maximum),
               str(OUT / variant), '--benchmark_filter=' + pattern,
               f'--benchmark_min_time={seconds}s', '--benchmark_min_warmup_time=0',
               '--benchmark_out=' + str(dest / 'benchmark.json'), '--benchmark_out_format=json']
    save(dest / 'command.json', {'command': command, 'environment': {
        k: v for k, v in env.items() if k.startswith(('REGRESSION_', 'VMEMKV_')) or k == 'TMPDIR'}})
    started = time.monotonic()
    with (dest / 'console.log').open('w') as log:
        try:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=240)
        except subprocess.TimeoutExpired:
            subprocess.run(['systemctl', '--user', 'stop', unit + '.scope'], check=False)
            save(dest / 'exit.json', {'code': 'timeout', 'wall_seconds': time.monotonic() - started})
            raise
    save(dest / 'exit.json', {'code': result.returncode, 'wall_seconds': time.monotonic() - started})
    result.check_returncode()
    rows = json.loads((dest / 'benchmark.json').read_text())['benchmarks']
    assert len(rows) == 1 and not rows[0].get('error_occurred'), rows
    rate = rows[0]['items_per_second']
    assert rate > 0
    save(dest / 'throughput.json', dict(scenario=scenario, value_bytes=size, operation=op, distribution=dist,
                                      threads=threads, variant=variant, repetition=rep, ops_per_second=rate, cpu_list=cpus))
    print(f'{dest.name}: {rate:,.0f} ops/s', flush=True)


def matrix():
    if not (OUT / 'matrix.json').exists():
        save(OUT / 'matrix.json', {'cells': cells(), 'repetitions': 2, 'variants': VARIANTS,
                                  'metric': 'items_per_second', 'benchmark_min_time_seconds': 2})
    for index, cell in enumerate(cells()):
        if args.only and not re.search(args.only, label(cell)):
            continue
        for rep in map(int, args.repetitions.split(',')):
            order = VARIANTS if (rep + index) % 2 else VARIANTS[::-1]
            for variant in order:
                one(cell, variant, rep, args.seconds, args.cpus)


def summarize():
    rows = [json.loads(p.read_text()) for p in OUT.glob('r*/throughput.json')]
    groups = {}
    for row in rows:
        key = tuple(row.get(k) for k in ['scenario', 'value_bytes', 'operation', 'distribution', 'threads', 'cpu_list'])
        groups.setdefault(key, {}).setdefault(row['variant'], []).append(row['ops_per_second'])
    summary = []
    for key, group in groups.items():
        if all(v in group for v in VARIANTS):
            a, b = [statistics.mean(group[v]) for v in VARIANTS]
            summary.append(dict(zip(['scenario', 'value_bytes', 'operation', 'distribution', 'threads', 'cpu_list'], key),
                                baseline=a, cross_pread=b, change_percent=(b / a - 1) * 100,
                                repetitions={v: len(group[v]) for v in VARIANTS}, raw=group))
    save(OUT / 'summary.json', summary)
    with (OUT / 'summary.csv').open('w') as output:
        columns = ['scenario', 'value_bytes', 'operation', 'distribution', 'threads', 'cpu_list',
                   'baseline', 'cross_pread', 'change_percent']
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(sorted(summary, key=lambda r: (r['scenario'], r['value_bytes'], r['operation'],
                                                        r['distribution'], r['threads'], r['cpu_list'] or '')))
    for row in summary:
        print(f"{row['scenario']:8s} {row['value_bytes']:5d} {row['operation']:12s} {row['distribution']:7s} "
              f"t{row['threads']:2d}: {row['change_percent']:+6.1f}%" + (' [pinned]' if row['cpu_list'] else ''))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['build', 'prepare', 'matrix', 'summarize'])
    parser.add_argument('--only', help='比較する条件名の正規表現')
    parser.add_argument('--repetitions', default='1,2')
    parser.add_argument('--seconds', type=float, default=2)
    parser.add_argument('--cpus', help='再確認用CPU affinity。通常計測と集計を分ける')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    globals()[args.action]()
