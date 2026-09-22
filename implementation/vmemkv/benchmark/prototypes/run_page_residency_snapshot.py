#!/usr/bin/env python3
"""常駐Uniform、100万Get固定。3版各2回＋省略率の別計測1回だけ。"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import uuid

import run_fixed_pread_1kb as base
from page_residency_loader import Tracker, check_formats, save

HERE = Path(__file__).resolve().parent
BITMAP = base.OUT.parent / 'page-residency-bitmap-20260923'
OUT = base.OUT.parent / 'page-residency-snapshot-20260923'
PAGES = 1716438
VARIANTS = ['proposal1', 'empty', 'snapshot']


def build():
    OUT.mkdir(exist_ok=False)
    template = (BITMAP/'source/bitmap/fixed_pread_benchmark.cpp').read_text().split('int main(')[0]
    main = template + (HERE/'page_residency_snapshot_benchmark.inc').read_text()
    original_command = json.loads((BITMAP/'build-commands.json').read_text())[1]
    commands, files = [], [Path(__file__), HERE/'page_residency_snapshot.hpp',
        HERE/'page_residency_snapshot_benchmark.inc', HERE/'page_residency_loader.py',
        BITMAP/'page_residency_bitmap.bpf.o']
    for variant in ['proposal1', 'bitmap']:
        original = base.OUT/'source/proposal1' if variant == 'proposal1' else BITMAP/'source/bitmap'
        source = OUT/'source'/variant
        shutil.copytree(original, source)
        harness = source/'fixed_pread_benchmark.cpp'
        harness.write_text(main)
        header = source/'src/vmemkv/read_path.hpp'
        if variant == 'bitmap':
            old = header.read_text()
            updated = base.replace_once(old, '#include "page_residency_hints.hpp"',
                '#include "page_residency_hints.hpp"\n#ifdef SNAPSHOT_COUNTERS\n'
                'namespace page_residency { inline thread_local uint64_t count_target=0, count_skipped=0; }\n#endif')
            updated = base.replace_once(updated,
                '  const auto ticket = page_residency::observe(mapping, offset, read_len);',
                '  const auto ticket = page_residency::observe(mapping, offset, read_len);\n'
                '#ifdef SNAPSHOT_COUNTERS\n'
                '  if (ticket.active) { ++page_residency::count_target; page_residency::count_skipped += ticket.hit; }\n#endif')
            header.write_text(updated)
            (OUT/'diagnostic-only.patch').write_text(''.join(difflib.unified_diff(
                old.splitlines(True), updated.splitlines(True), fromfile='bitmap/read_path.hpp', tofile='snapshot/read_path.hpp')))
            shutil.copyfile(HERE/'page_residency_snapshot.hpp', source/'src/vmemkv/page_residency_snapshot.hpp')
            files.append(source/'src/vmemkv/page_residency_hints.hpp')
        command = [s.replace(str(BITMAP/'source/bitmap'), str(source)) for s in original_command]
        command[-1] = str(OUT/variant)
        if variant == 'bitmap': command.insert(1, '-DSNAPSHOT_HINTS')
        commands.append(command)
        if variant == 'bitmap':
            counter = command.copy(); counter.insert(1, '-DSNAPSHOT_COUNTERS'); counter[-1] = str(OUT/'count-snapshot')
            commands.append(counter)
        files += [header, harness]
    save(OUT/'build-commands.json', commands)
    for command in commands:
        name = Path(command[-1]).name
        with (OUT/f'build-{name}.log').open('w') as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        files.append(Path(command[-1]))
        print('built '+name, flush=True)
    result = subprocess.run([str(OUT/'bitmap'), '--selftest'], check=True, capture_output=True, text=True)
    (OUT/'selftest.log').write_text(result.stdout+result.stderr)
    save(OUT/'provenance.json', {
        'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
        'kernel':os.uname().release,
        'sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'note':'製品ソースは案1のみ。空/初期スナップショットは同じバイナリ。計数は別バイナリ。'})
    print(result.stdout.strip(), flush=True)


def case(args):
    dest = OUT/'runs'/args.name
    group = Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().split('0::/')[1].strip()
    limits = {k:(group/k).read_text().strip() for k in ['memory.high','memory.max']}
    assert int(limits['memory.high'])==16<<30 and int(limits['memory.max'])==32<<30
    assert sorted(os.sched_getaffinity(0)) == [0]
    save(dest/'memory-limits.json', dict(limits,cgroup=str(group),cpu_affinity=[0]))
    before = int((group/'memory.current').read_text())
    start = time.monotonic()
    tracker = Tracker(str(base.DB)+'.t2chk', PAGES, object_path=BITMAP/'page_residency_bitmap.bpf.o',
                      entries=(PAGES+63)//64, entry_bytes=24) if args.variant!='proposal1' else None
    load_seconds = time.monotonic()-start if tracker else 0
    try:
        save(dest/'map-memory.json', {'before_current':before,'after_current':int((group/'memory.current').read_text()),
            'body_bytes':((PAGES+63)//64)*24 if tracker else 0, 'bpf_load_seconds':load_seconds})
        env = base.runner.environment(base.DB,1024,base.COUNT,'resident',1)
        env.pop('PAGE_HINT_FD', None); env.pop('PAGE_HINT_PAGES', None)
        env.update(TMPDIR=str(base.ROOT/'build/ltm/tmp'),VMEMKV_BENCH_RESULTS_DIR=str(dest))
        if tracker: env.update(PAGE_HINT_FD=str(tracker.fd),PAGE_HINT_PAGES=str(PAGES))
        binary = 'count-snapshot' if args.diagnostic else 'proposal1' if args.variant=='proposal1' else 'bitmap'
        command = [str(OUT/binary),args.variant]
        save(dest/'command.json', {'command':command,'uid':args.uid,'gid':args.gid,
            'environment':{k:v for k,v in env.items() if k.startswith(('REGRESSION_','VMEMKV_','PAGE_HINT_')) or k=='TMPDIR'}})
        with (dest/'console.log').open('w') as log:
            result = subprocess.run(command,env=env,pass_fds=(tracker.fd,) if tracker else (),
                user=args.uid,group=args.gid,extra_groups=[],stdout=log,stderr=subprocess.STDOUT,timeout=90)
        save(dest/'exit.json',{'code':result.returncode}); result.check_returncode()
        rows = [json.loads(line) for line in (dest/'console.log').read_text().splitlines() if line.startswith('{')]
        assert len(rows)==1 and rows[0]['gets']==1000000 and rows[0]['value_check']
        row = rows[0]; row['bpf_load_seconds']=load_seconds
        if not args.diagnostic:
            row['incremental_setup_plus_get_seconds'] = row['snapshot_plus_get_seconds']+row['hint_attach_seconds']+load_seconds
        else:
            assert 0 <= row['mincore_skipped'] <= row['target_gets'] <= 1000000
            row['skip_percent']=100*row['mincore_skipped']/row['target_gets']
        save(dest/'result.json',row)
    finally:
        if tracker: tracker.close()


def run():
    assert os.geteuid()==0 and 'SUDO_UID' in os.environ, 'sudoで実行してください'
    uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
    runs=OUT/'runs'; runs.mkdir(exist_ok=False)
    try:
        save(runs/'event-formats.json',check_formats())
        save(runs/'plan.json', {'scenario':'resident','distribution':'Uniform','threads':1,'cpu':0,
            'variants':VARIANTS,'repetitions':2,'gets_per_run':1000000,'seed':42,
            'separate_diagnostic_runs':1,'snapshot_chunk_pages':1024,
            'note':'T2とPTEを事前常駐。固定回数・GB較正なし。初期化時間とGet時間を分離。性能実行はカウンターなし。'})
        jobs = [(f'r{r}-{v}',v,False) for r in [1,2] for v in (VARIANTS if r==1 else VARIANTS[::-1])]
        jobs.append(('diagnostic-snapshot','snapshot',True))
        for name,variant,diagnostic in jobs:
            dest=runs/name; dest.mkdir(); os.chown(dest,uid,gid)
            unit='vmemkv-snapshot-'+uuid.uuid4().hex[:12]
            command=['systemd-run','--scope','--quiet','--unit='+unit,
                '-p','MemoryHigh=17179869184','-p','MemoryMax=34359738368','-p','MemorySwapMax=1099511627776',
                '--','taskset','-c','0',sys.executable,str(Path(__file__).resolve()),'case',
                '--name',name,'--variant',variant,'--uid',str(uid),'--gid',str(gid)]
            if diagnostic: command.append('--diagnostic')
            save(dest/'scope-command.json',command)
            with (dest/'loader.log').open('w') as log:
                try: result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=120)
                except subprocess.TimeoutExpired:
                    subprocess.run(['systemctl','stop',unit+'.scope'],check=False); raise
            save(dest/'scope-exit.json',{'code':result.returncode}); result.check_returncode()
            row=json.loads((dest/'result.json').read_text())
            print(name+': '+(f"skip={row['skip_percent']:.2f}%" if diagnostic else
                f"{row['ops_per_second']:.0f} ops/s; snapshot={row['snapshot_seconds']*1000:.3f}ms"),flush=True)
        raw={v:[json.loads((runs/f'r{r}-{v}'/'result.json').read_text()) for r in [1,2]] for v in VARIANTS}
        means={v:{k:statistics.mean(row[k] for row in raw[v]) for k in
            ['ops_per_second','get_seconds','snapshot_seconds','snapshot_plus_get_seconds','incremental_setup_plus_get_seconds']}
            for v in VARIANTS}
        for v in VARIANTS:
            means[v]['vs_proposal1_percent']=(means[v]['ops_per_second']/means['proposal1']['ops_per_second']-1)*100
        save(runs/'summary.json',{'means':means,'raw':raw,
            'diagnostic':json.loads((runs/'diagnostic-snapshot/result.json').read_text())})
        print('完了: 6性能実行＋省略率1実行。BPF解除済み。',flush=True)
    finally:
        for p in runs.rglob('*'):
            if not p.is_symlink(): os.chown(p,uid,gid)
        os.chown(runs,uid,gid)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['build','run','case'])
    parser.add_argument('--variant',choices=VARIANTS); parser.add_argument('--name')
    parser.add_argument('--uid',type=int); parser.add_argument('--gid',type=int)
    parser.add_argument('--diagnostic',action='store_true')
    args=parser.parse_args()
    if args.action=='build': build()
    elif args.action=='run': run()
    else: case(args)
