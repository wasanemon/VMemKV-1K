#!/usr/bin/env python3
"""sudoでロードし、測定用cgroup内の一般ユーザーGetとmapを同じ予算に置く。"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid

import run_fixed_pread_1kb as base
from page_residency_loader import OUT, Tracker, check_formats, save

RUNS = OUT / 'measurements'
PAGES = 1716438
VARIANTS = ['proposal1', 'hints']
SCRIPT = Path(__file__).resolve()


def case(args):
    group = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().split('0::/')[1].strip()
    dest = Path(args.dest)
    high, maximum = ((16 << 30, 32 << 30) if args.scenario == 'resident' else (1 << 30, 2 << 30))
    limits = {k:(group/k).read_text().strip() for k in ['memory.high', 'memory.max']}
    assert int(limits['memory.high']) == high and int(limits['memory.max']) == maximum
    limits.update(cgroup=str(group), cpu_affinity=sorted(os.sched_getaffinity(0)))
    save(dest / 'memory-limits.json', limits)
    before = int((group / 'memory.current').read_text())
    tracker = Tracker(str(base.DB) + '.t2chk', PAGES) if args.variant == 'hints' else None
    try:
        save(dest / 'map-memory.json', {'before_current':before,
            'after_current':int((group/'memory.current').read_text()),
            'map_body_bytes': PAGES * 8 if tracker else 0,
            'creator_cgroup':str(group), 'note':'BPF map作成はこのcgroupに入った後。差分はローダー等も含む。'})
        env = base.runner.environment(base.DB, 1024, base.COUNT, args.scenario, args.threads)
        env.update(TMPDIR=str(base.ROOT/'build/ltm/tmp'), VMEMKV_BENCH_RESULTS_DIR=str(dest))
        if tracker: env.update(PAGE_HINT_FD=str(tracker.fd), PAGE_HINT_PAGES=str(PAGES))
        command = [str(OUT / args.variant)]
        if args.verify:
            command.append('--verify')
        else:
            pattern = '^Store=VMemKV/Variant=Bloom-T1InlineValue/Op=Get/Mode=Hit/Dist=' + args.dist + '/Value=.*threads:' + str(args.threads) + '$'
            command += ['--benchmark_filter=' + pattern, '--benchmark_min_time=2s', '--benchmark_min_warmup_time=0',
                        '--benchmark_out=' + str(dest/'benchmark.json'), '--benchmark_out_format=json']
        save(dest / 'benchmark-command.json', {'command': command, 'environment':{k:v for k,v in env.items() if k.startswith(('VMEMKV_', 'REGRESSION_', 'PAGE_HINT_')) or k=='TMPDIR'},
                                               'uid':args.uid, 'gid':args.gid})
        # 子は一般ユーザー。root親はBPFリンクを保持して待つだけで、同じcgroupに残る。
        with (dest / 'console.log').open('w') as log:
            result = subprocess.run(command, env=env, pass_fds=(tracker.fd,) if tracker else (),
                                    user=args.uid, group=args.gid, extra_groups=[],
                                    stdout=log, stderr=subprocess.STDOUT, timeout=180)
        save(dest / 'exit.json', {'code':result.returncode})
        result.check_returncode()
        if tracker:
            view = memoryview(tracker.mapping).cast('Q')
            counts = Counter(v & 3 for v in view[:PAGES])
            view.release()
            save(dest/'hint-states-after.json', dict(counts))
            assert counts[1] > 0, 'confirmed hints were never registered'
    finally:
        if tracker: tracker.close()


def one(variant, scenario, dist, threads, rep, uid, gid, verify=False):
    name = f'r{rep}-{scenario}-{dist}-t{threads}-{variant}' if not verify else 'verify-' + variant
    dest = RUNS / name
    if (dest / 'throughput.json').exists(): return
    dest.mkdir(exist_ok=False)
    os.chown(dest, uid, gid)
    high, maximum = ((16 << 30, 32 << 30) if scenario == 'resident' else (1 << 30, 2 << 30))
    cpus = '0' if threads == 1 else ','.join(str(i) for i in range(0,32,2))
    unit = 'vmemkv-hints-' + uuid.uuid4().hex[:12]
    command = ['systemd-run', '--scope', '--quiet', '--unit='+unit,
               '-p', f'MemoryHigh={high}', '-p', f'MemoryMax={maximum}', '-p','MemorySwapMax=1099511627776',
               '--', 'taskset','-c',cpus,sys.executable,str(SCRIPT),'case',
               '--variant',variant,'--scenario',scenario,'--dist',dist,'--threads',str(threads),
               '--uid',str(uid),'--gid',str(gid),'--dest',str(dest)]
    if verify: command.append('--verify')
    save(dest / 'scope-command.json', command)
    started = time.monotonic()
    with (dest/'loader.log').open('w') as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
        except subprocess.TimeoutExpired:
            subprocess.run(['systemctl','stop',unit+'.scope'],check=False)
            raise
    save(dest/'scope-exit.json', {'code':result.returncode,'wall_seconds':time.monotonic()-started})
    result.check_returncode()
    if verify:
        print(variant + ': ' + (dest/'console.log').read_text().strip(),flush=True)
        return
    data = json.loads((dest/'benchmark.json').read_text())['benchmarks']
    assert len(data)==1 and not data[0].get('error_occurred'), data
    rate = data[0]['items_per_second']
    save(dest/'throughput.json', {'variant':variant,'scenario':scenario,'distribution':dist,'threads':threads,'repetition':rep,'ops_per_second':rate})
    print(name + ': ' + f'{rate:,.0f} ops/s',flush=True)


def summarize():
    grouped = {}
    for path in RUNS.glob('r*/throughput.json'):
        d=json.loads(path.read_text()); key=(d['scenario'],d['distribution'],d['threads'])
        grouped.setdefault(key,{}).setdefault(d['variant'],[]).append(d['ops_per_second'])
    rows=[]
    for key, raw in sorted(grouped.items()):
        assert all(len(raw[v])==2 for v in VARIANTS)
        a,b=(statistics.mean(raw[v]) for v in VARIANTS)
        rows.append(dict(zip(['scenario','distribution','threads'],key), proposal1=a,hints=b,change_percent=(b/a-1)*100,raw=raw))
    save(RUNS/'summary.json',rows)
    return rows


def matrix(uid,gid,threads):
    for scenario in ['resident','ltm']:
        for dist in ['Uniform','Zipf']:
            for rep in [1,2]:
                for variant in (VARIANTS if rep==1 else VARIANTS[::-1]):
                    one(variant,scenario,dist,threads,rep,uid,gid)
    return summarize()


def run():
    assert os.geteuid()==0 and 'SUDO_UID' in os.environ, 'sudoで実行してください'
    uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
    RUNS.mkdir(exist_ok=True); os.chown(RUNS,uid,gid)
    save(RUNS/'event-formats.json',check_formats())
    save(RUNS/'plan.json',{'variants':VARIANTS,'scenarios':['resident','ltm'],'distributions':['Uniform','Zipf'],
        'first_threads':1,'repetitions':2,'min_time_seconds':2,'initial_runs':16,
        'extension_rule':'常駐2条件とも+1%超かつLTM2条件とも-1%以上なら16スレッド16実行を追加。統計的有意差の基準ではない。',
        'map_body_bytes':PAGES*8,'memory_accounting':'mapとrootローダーも測定cgroup内、Get子は一般ユーザー',
        'note':'再ビルド・設定変更なし。既存固定checkpointでGet Hitのみ。'})
    try:
        for variant in VARIANTS: one(variant,'resident','Uniform',1,0,uid,gid,verify=True)
        rows=matrix(uid,gid,1)
        extend=all(d['change_percent']>1 for d in rows if d['scenario']=='resident') and all(d['change_percent']>=-1 for d in rows if d['scenario']=='ltm')
        save(RUNS/'extension-decision.json',{'extend':extend})
        if extend: rows=matrix(uid,gid,16)
        print(json.dumps(rows,ensure_ascii=False,indent=2),flush=True)
    finally:
        for p in RUNS.rglob('*'):
            if not p.is_symlink(): os.chown(p,uid,gid)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['run','case'])
    parser.add_argument('--variant',choices=VARIANTS)
    parser.add_argument('--scenario',choices=['resident','ltm'])
    parser.add_argument('--dist',choices=['Uniform','Zipf'])
    parser.add_argument('--threads',type=int,choices=[1,16],default=1)
    parser.add_argument('--uid',type=int); parser.add_argument('--gid',type=int)
    parser.add_argument('--dest'); parser.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if args.action=='run': run()
    else: case(args)
