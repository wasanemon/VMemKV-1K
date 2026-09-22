#!/usr/bin/env python3
"""案1/初期スナップショット、常駐/LTM・Uniform/Zipf・1/16 threadの追加比較。"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

import run_page_residency as common
from page_residency_loader import Tracker, check_formats, save

base = common.base
HERE = Path(__file__).resolve().parent
BITMAP = base.OUT.parent/'page-residency-bitmap-20260923'
SNAPSHOT = base.OUT.parent/'page-residency-snapshot-20260923'
OUT = SNAPSHOT/'matrix'
RUNS = OUT/'measurements'
VARIANTS = ['proposal1','snapshot']
common.OUT, common.RUNS, common.SCRIPT = OUT, RUNS, Path(__file__).resolve()


def build():
    OUT.mkdir(exist_ok=False)
    commands, hashes = [], {}
    for variant in VARIANTS:
        original = base.OUT/'source/proposal1' if variant=='proposal1' else BITMAP/'source/bitmap'
        source = OUT/'source'/variant
        shutil.copytree(original,source)
        harness = source/'fixed_pread_benchmark.cpp'
        old = harness.read_text()
        if variant=='snapshot':
            # 測定済みの初期化実装・Get/BPFをそのまま使用する。
            shutil.copyfile(SNAPSHOT/'source/bitmap/src/vmemkv/page_residency_snapshot.hpp',
                            source/'src/vmemkv/page_residency_snapshot.hpp')
            text = '#include <chrono>\n#include <iomanip>\n#include "vmemkv/page_residency_snapshot.hpp"\n'+old
            text = base.replace_once(text,'    prepare_cache(*store, resident);', '''    prepare_cache(*store, resident);
    if (store->t2().get_memory()->base_boundary.load() != store->t2().bytes_used())
      throw std::runtime_error("immutable checkpoint only");
    if (!page_residency::states) throw std::runtime_error("BPF map required");
    const auto begin = std::chrono::steady_clock::now();
    const auto snap = page_residency::snapshot(store->t2().bytes_used());
    const double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now()-begin).count();
    uint64_t confirmed = 0;
    for (uint64_t g = 0; g < (page_residency::pages+63)/64; ++g)
      confirmed += __builtin_popcountll(page_residency::load(&page_residency::states[g].resident));
    std::cout << std::setprecision(12) << "{\\"snapshot_seconds\\":" << elapsed
              << ",\\"resident_pages\\":" << snap.resident_pages
              << ",\\"confirmed_pages\\":" << confirmed
              << ",\\"mincore_calls\\":" << snap.calls << "}\\n";''')
            harness.write_text(text)
            (OUT/'snapshot-harness.patch').write_text(''.join(difflib.unified_diff(
                old.splitlines(True),text.splitlines(True),fromfile='bitmap/harness.cpp',tofile='snapshot/harness.cpp')))
        cmd=[s.replace(str(BITMAP/'source/bitmap'),str(source))
             for s in json.loads((BITMAP/'build-commands.json').read_text())[1]]
        cmd[-1]=str(OUT/variant); commands.append(cmd)
        with (OUT/f'build-{variant}.log').open('w') as log:
            subprocess.run(cmd,check=True,stdout=log,stderr=subprocess.STDOUT)
        for p in [harness,source/'src/vmemkv/read_path.hpp',OUT/variant]:
            hashes[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
        print('built '+variant,flush=True)
    for p in [Path(__file__),HERE/'page_residency_loader.py',HERE/'run_page_residency.py',
              OUT/'source/snapshot/src/vmemkv/page_residency_hints.hpp',
              OUT/'source/snapshot/src/vmemkv/page_residency_snapshot.hpp',BITMAP/'page_residency_bitmap.bpf.o']:
        hashes[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
    save(OUT/'build-commands.json',commands)
    save(OUT/'provenance.json',{'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
        'kernel':os.uname().release,'sha256':hashes,
        'note':'既存Getベンチを再利用。初期化/Get/BPFは前回の実装。計数なし。前回100万Get固定とは絶対速度を合算しない。'})


def case(args):
    dest=Path(args.dest)
    group=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().split('0::/')[1].strip()
    high,maximum=((16<<30,32<<30) if args.scenario=='resident' else (1<<30,2<<30))
    limits={k:(group/k).read_text().strip() for k in ['memory.high','memory.max']}
    assert int(limits['memory.high'])==high and int(limits['memory.max'])==maximum
    cpus=[0] if args.threads==1 else list(range(0,32,2))
    assert sorted(os.sched_getaffinity(0))==cpus
    save(dest/'memory-limits.json',dict(limits,cgroup=str(group),cpu_affinity=cpus))
    before=int((group/'memory.current').read_text()); started=time.monotonic()
    tracker=Tracker(str(base.DB)+'.t2chk',common.PAGES,object_path=BITMAP/'page_residency_bitmap.bpf.o',
                    entries=(common.PAGES+63)//64,entry_bytes=24) if args.variant=='snapshot' else None
    load_seconds=time.monotonic()-started if tracker else 0
    try:
        save(dest/'map-memory.json',{'before_current':before,'after_current':int((group/'memory.current').read_text()),
            'body_bytes':((common.PAGES+63)//64)*24 if tracker else 0,'bpf_load_seconds':load_seconds})
        env=base.runner.environment(base.DB,1024,base.COUNT,args.scenario,args.threads)
        env.pop('PAGE_HINT_FD',None); env.pop('PAGE_HINT_PAGES',None)
        env.update(TMPDIR=str(base.ROOT/'build/ltm/tmp'),VMEMKV_BENCH_RESULTS_DIR=str(dest))
        if tracker: env.update(PAGE_HINT_FD=str(tracker.fd),PAGE_HINT_PAGES=str(common.PAGES))
        pattern='^Store=VMemKV/Variant=Bloom-T1InlineValue/Op=Get/Mode=Hit/Dist='+args.dist+'/Value=.*threads:'+str(args.threads)+'$'
        cmd=[str(OUT/args.variant),'--benchmark_filter='+pattern,'--benchmark_min_time=2s',
             '--benchmark_min_warmup_time=0','--benchmark_out='+str(dest/'benchmark.json'),'--benchmark_out_format=json']
        save(dest/'benchmark-command.json',{'command':cmd,'uid':args.uid,'gid':args.gid,
            'environment':{k:v for k,v in env.items() if k.startswith(('VMEMKV_','REGRESSION_','PAGE_HINT_')) or k=='TMPDIR'}})
        with (dest/'console.log').open('w') as log:
            result=subprocess.run(cmd,env=env,pass_fds=(tracker.fd,) if tracker else (),
                user=args.uid,group=args.gid,extra_groups=[],stdout=log,stderr=subprocess.STDOUT,timeout=180)
        save(dest/'exit.json',{'code':result.returncode}); result.check_returncode()
        if tracker:
            initial=[json.loads(line) for line in (dest/'console.log').read_text().splitlines()
                     if line.startswith('{"snapshot_seconds":')]
            assert len(initial)==1, 'snapshot must run once before benchmark calibration'
            save(dest/'snapshot-init.json',initial[0])
    finally:
        if tracker: tracker.close()


def run():
    assert os.geteuid()==0 and 'SUDO_UID' in os.environ, 'sudoで実行してください'
    uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
    RUNS.mkdir(exist_ok=False); os.chown(RUNS,uid,gid)
    cells=[(scenario,dist,threads) for scenario in ['resident','ltm'] for dist in ['Uniform','Zipf'] for threads in [1,16]]
    try:
        save(RUNS/'event-formats.json',check_formats())
        save(RUNS/'plan.json',{'cells':cells,'variants':VARIANTS,'repetitions':2,'total_runs':32,
            'benchmark_min_time_seconds':2,'cpu_1':[0],'cpu_16':list(range(0,32,2)),
            'resident_high_max_GiB':[16,32],'ltm_high_max_GiB':[1,2],
            'note':'常駐はT2/PTE事前常駐、LTMは既存cold準備。初期化は各プロセス1回、GB較正前。追加省略率・原因別計測なし。'})
        rows=[]
        for scenario,dist,threads in cells:
            for rep in [1,2]:
                for variant in (VARIANTS if rep==1 else VARIANTS[::-1]):
                    common.one(variant,scenario,dist,threads,rep,uid,gid)
            def folder(rep,variant): return RUNS/f'r{rep}-{scenario}-{dist}-t{threads}-{variant}'
            raw={v:[json.loads((folder(rep,v)/'throughput.json').read_text())['ops_per_second'] for rep in [1,2]] for v in VARIANTS}
            a,b=(statistics.mean(raw[v]) for v in VARIANTS)
            init=[json.loads((folder(rep,'snapshot')/'snapshot-init.json').read_text()) for rep in [1,2]]
            rows.append({'scenario':scenario,'distribution':dist,'threads':threads,'proposal1':a,'snapshot':b,
                'change_percent':100*(b/a-1),'raw':raw,'snapshot_init':init,
                'snapshot_ms_mean':statistics.mean(x['snapshot_seconds'] for x in init)*1000})
            save(RUNS/'summary.json',rows)
        print('完了: 8条件×2版×2回、32実行。各回BPF解除済み。',flush=True)
    finally:
        for p in RUNS.rglob('*'):
            if not p.is_symlink(): os.chown(p,uid,gid)
        os.chown(RUNS,uid,gid)


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['build','run','case'])
    p.add_argument('--variant',choices=VARIANTS); p.add_argument('--scenario',choices=['resident','ltm'])
    p.add_argument('--dist',choices=['Uniform','Zipf']); p.add_argument('--threads',type=int,choices=[1,16])
    p.add_argument('--uid',type=int); p.add_argument('--gid',type=int); p.add_argument('--dest')
    args=p.parse_args()
    if args.action=='build': build()
    elif args.action=='run': run()
    else: case(args)
