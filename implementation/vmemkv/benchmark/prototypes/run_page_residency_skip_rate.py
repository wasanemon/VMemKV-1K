#!/usr/bin/env python3
"""圧縮版の常駐Uniform/Zipfで省略率を各1回だけ確認。速度比較は行わない。"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import run_fixed_pread_1kb as base
from page_residency_loader import Tracker, check_formats, save

HERE = Path(__file__).resolve().parent
BITMAP = base.OUT.parent / 'page-residency-bitmap-20260923'
OUT = base.OUT.parent / 'page-residency-skip-rate-20260923'
PAGES = 1716438


def build():
    OUT.mkdir(exist_ok=False)
    source = OUT / 'source/bitmap'
    shutil.copytree(BITMAP / 'source/bitmap', source)
    header = source / 'src/vmemkv/read_path.hpp'
    old = header.read_text()
    text = base.replace_once(old, '#include "page_residency_hints.hpp"',
        '#include "page_residency_hints.hpp"\nnamespace page_residency {\n'
        'inline thread_local uint64_t count_target = 0, count_skipped = 0;\n}\n')
    text = base.replace_once(text, '  const auto ticket = page_residency::observe(mapping, offset, read_len);',
        '  const auto ticket = page_residency::observe(mapping, offset, read_len);\n'
        '  if (ticket.active) { ++page_residency::count_target; page_residency::count_skipped += ticket.hit; }')
    header.write_text(text)
    harness = source / 'fixed_pread_benchmark.cpp'
    before = harness.read_text()
    marker = '  const size_t count = std::stoull(required_env("REGRESSION_COUNT"));'
    after = base.replace_once(before, marker, marker + '\n' + (HERE/'page_residency_skip_rate.inc').read_text())
    harness.write_text(after)
    patch = ''.join(difflib.unified_diff(old.splitlines(True), text.splitlines(True), fromfile='bitmap/read_path.hpp', tofile='count/read_path.hpp'))
    patch += ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='bitmap/harness.cpp', tofile='count/harness.cpp'))
    (OUT/'counter.patch').write_text(patch)
    command = [s.replace(str(BITMAP/'source/bitmap'), str(source)) for s in json.loads((BITMAP/'build-commands.json').read_text())[1]]
    command[-1] = str(OUT/'count-hints')
    save(OUT/'build-command.json', command)
    with (OUT/'build.log').open('w') as log:
        subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
    files = [Path(__file__), HERE/'page_residency_skip_rate.inc', HERE/'page_residency_loader.py',
             OUT/'count-hints', header, harness, source/'src/vmemkv/page_residency_hints.hpp',
             BITMAP/'page_residency_bitmap.bpf.o']
    save(OUT/'provenance.json', {'commit': subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'branch': subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
        'sha256': {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'bitmap_provenance': str(BITMAP/'provenance.json'),
        'note': '圧縮版の別コピーに2カウンター追加。BPFと経路選択・登録処理は変更なし。'})
    print('built count-hints', flush=True)


def case(args):
    dest = OUT/'runs'/args.dist
    group = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().split('0::/')[1].strip()
    limits = {k:(group/k).read_text().strip() for k in ['memory.high','memory.max']}
    assert int(limits['memory.high'])==16<<30 and int(limits['memory.max'])==32<<30
    assert sorted(os.sched_getaffinity(0)) == [0]
    save(dest/'memory-limits.json', dict(limits, cgroup=str(group), cpu_affinity=[0]))
    before = int((group/'memory.current').read_text())
    tracker = Tracker(str(base.DB)+'.t2chk', PAGES, object_path=BITMAP/'page_residency_bitmap.bpf.o',
                      entries=(PAGES+63)//64, entry_bytes=24)
    try:
        save(dest/'map-memory.json', {'before_current': before, 'after_current': int((group/'memory.current').read_text()),
                                     'body_bytes': ((PAGES+63)//64)*24})
        env = base.runner.environment(base.DB, 1024, base.COUNT, 'resident', 1)
        env.update(PAGE_HINT_FD=str(tracker.fd), PAGE_HINT_PAGES=str(PAGES),
                   TMPDIR=str(base.ROOT/'build/ltm/tmp'), VMEMKV_BENCH_RESULTS_DIR=str(dest))
        command = [str(OUT/'count-hints'), '--count-hints', args.dist]
        save(dest/'command.json', {'command':command, 'uid':args.uid, 'gid':args.gid,
            'environment': {k:v for k,v in env.items() if k.startswith(('REGRESSION_', 'VMEMKV_', 'PAGE_HINT_')) or k=='TMPDIR'}})
        with (dest/'console.log').open('w') as log:
            result = subprocess.run(command, env=env, pass_fds=(tracker.fd,), user=args.uid, group=args.gid,
                extra_groups=[], stdout=log, stderr=subprocess.STDOUT, timeout=90)
        save(dest/'exit.json', {'code':result.returncode})
        result.check_returncode()
    finally:
        tracker.close()


def run():
    assert os.geteuid()==0 and 'SUDO_UID' in os.environ, 'sudoで実行してください'
    uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
    runs = OUT/'runs'
    runs.mkdir(exist_ok=False)
    try:
        save(runs/'event-formats.json', check_formats())
        save(runs/'plan.json', {'distributions':['Uniform','Zipf'], 'scenario':'resident', 'threads':1,
            'gets_per_distribution':1000000, 'window_gets':100000, 'repetitions':1,
            'denominator':'ticket.active（ページ跨ぎ小レコードの対象Get）',
            'note':'T2は事前常駐、ヒントは初期未知。連続乱数列。GB較正なし。throughputとして評価しない。'})
        rows=[]
        for dist in ['Uniform','Zipf']:
            dest=runs/dist; dest.mkdir(); os.chown(dest,uid,gid)
            unit='vmemkv-skip-'+uuid.uuid4().hex[:12]
            command=['systemd-run','--scope','--quiet','--unit='+unit,
                '-p','MemoryHigh=17179869184','-p','MemoryMax=34359738368','-p','MemorySwapMax=1099511627776',
                '--','taskset','-c','0',sys.executable,str(Path(__file__).resolve()),'case',
                '--dist',dist,'--uid',str(uid),'--gid',str(gid)]
            save(dest/'scope-command.json',command)
            with (dest/'loader.log').open('w') as log:
                try: result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=120)
                except subprocess.TimeoutExpired:
                    subprocess.run(['systemctl','stop',unit+'.scope'],check=False)
                    raise
            save(dest/'scope-exit.json',{'code':result.returncode})
            result.check_returncode()
            windows=[json.loads(line) for line in (dest/'console.log').read_text().splitlines() if line.startswith('{')]
            assert len(windows)==10
            for i,w in enumerate(windows):
                assert w['get_begin']==i*100000 and w['get_end']==(i+1)*100000
                assert 0 <= w['mincore_skipped'] <= w['target_gets'] <= 100000 and w['target_gets']>0
                w['skip_percent']=100*w['mincore_skipped']/w['target_gets']
            save(dest/'windows.json',windows)
            rows.append({'distribution':dist,'initial':windows[0],'late':windows[-1]})
            print(dist+': initial '+f"{windows[0]['skip_percent']:.2f}%"+'; late '+f"{windows[-1]['skip_percent']:.2f}%",flush=True)
        save(runs/'summary.json',rows)
    finally:
        for p in runs.rglob('*'):
            if not p.is_symlink(): os.chown(p,uid,gid)
        os.chown(runs,uid,gid)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action', choices=['build','run','case'])
    parser.add_argument('--dist',choices=['Uniform','Zipf'])
    parser.add_argument('--uid',type=int); parser.add_argument('--gid',type=int)
    args=parser.parse_args()
    if args.action=='build': build()
    elif args.action=='run': run()
    else: case(args)
