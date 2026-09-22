#!/usr/bin/env python3
"""案1・8B/page・圧縮版を同じ一時cgroup条件で各2回比較する。"""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import statistics
import subprocess

import run_page_residency as common
from page_residency_loader import OUT as OLD, Tracker, check_formats, save

OUT = OLD.parent / 'page-residency-bitmap-20260923'
RUNS = OUT / 'measurements'
PAGES = common.PAGES
BLOCKS = (PAGES + 63) // 64
VARIANTS = ['proposal1', 'hints', 'bitmap']
# 既存scope/timeout/CPU配置/throughput抽出をそのまま再利用する。
common.OUT, common.RUNS, common.SCRIPT = OUT, RUNS, Path(__file__).resolve()


def bitmap_tracker(path, pages):
    return Tracker(path, pages, object_path=OUT / 'page_residency_bitmap.bpf.o',
                   entries=(pages + 63) // 64, entry_bytes=24)


def probe():
    path = OUT / 'probe-pages.tmp'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    tracker = None
    try:
        data = b'x' * (65 * 4096)
        assert os.write(fd, data) == len(data)
        os.fsync(fd)
        os.posix_fadvise(fd, 0, len(data), os.POSIX_FADV_DONTNEED)
        tracker = bitmap_tracker(path, 65)
        def blocks():
            return [[int.from_bytes(tracker.mapping[i*24+j*8:i*24+j*8+8], 'little')
                     for j in range(3)] for i in range(2)]
        rows = {'initial': blocks()}
        assert os.pread(fd, len(data), 0) == data
        rows['added'] = blocks()
        assert rows['added'][0][1] == rows['added'][1][1] == 0
        assert rows['added'][0][2] > 0 and rows['added'][1][2] > 0
        # 専用ファイル、回収圧力なし。確認済みのページ0,1,63,64をセットする。
        for index, mask in [(0, (1 << 63) | 3), (1, 1)]:
            tracker.mapping[index*24:index*24+8] = mask.to_bytes(8, 'little')
        rows['confirmed'] = blocks()
        for page in [0, 63, 64]:
            os.posix_fadvise(fd, page*4096, 4096, os.POSIX_FADV_DONTNEED)
        rows['deleted'] = blocks()
        assert rows['deleted'][0][0] == 2 and rows['deleted'][1][0] == 0
        assert rows['deleted'][0][1] & ((1 << 63) | 1) == ((1 << 63) | 1)
        assert rows['deleted'][1][1] & 1
        assert all(a[2] > b[2] for a, b in zip(rows['deleted'], rows['confirmed']))
        for page in [0, 63, 64]: assert os.pread(fd, 4096, page*4096) == b'x'*4096
        rows['readded'] = blocks()
        assert rows['readded'][0][1] == rows['readded'][1][1] == 0
        assert rows['readded'][0][0] == 2 and rows['readded'][1][0] == 0
        assert all(a[2] > b[2] for a, b in zip(rows['readded'], rows['deleted']))
        save(OUT / 'probe-result.json', {'pass': True, 'blocks_resident_blocked_generation': rows})
        print('PASS: compact BPF map; invalidation and re-add; neighboring bit preserved', flush=True)
    finally:
        if tracker: tracker.close()
        os.close(fd)
        path.unlink()


def case(args):
    group = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().split('0::/')[1].strip()
    dest = Path(args.dest)
    high, maximum = ((16 << 30, 32 << 30) if args.scenario == 'resident' else (1 << 30, 2 << 30))
    limits = {k: (group/k).read_text().strip() for k in ['memory.high', 'memory.max']}
    assert int(limits['memory.high']) == high and int(limits['memory.max']) == maximum
    limits.update(cgroup=str(group), cpu_affinity=sorted(os.sched_getaffinity(0)))
    save(dest / 'memory-limits.json', limits)
    before = int((group / 'memory.current').read_text())
    path = str(common.base.DB) + '.t2chk'
    tracker = bitmap_tracker(path, PAGES) if args.variant == 'bitmap' else Tracker(path, PAGES) if args.variant == 'hints' else None
    try:
        save(dest / 'map-memory.json', {'before_current': before,
            'after_current': int((group/'memory.current').read_text()),
            'map_body_bytes': BLOCKS*24 if args.variant == 'bitmap' else PAGES*8 if tracker else 0,
            'creator_cgroup': str(group), 'note': '差分にはローダー等も含む。両BPF版とも同じcgroup内で作成。'})
        env = common.base.runner.environment(common.base.DB, 1024, common.base.COUNT, args.scenario, args.threads)
        env.update(TMPDIR=str(common.base.ROOT/'build/ltm/tmp'), VMEMKV_BENCH_RESULTS_DIR=str(dest))
        if tracker: env.update(PAGE_HINT_FD=str(tracker.fd), PAGE_HINT_PAGES=str(PAGES))
        command = [str(OUT / args.variant)]
        if args.verify: command.append('--verify')
        else:
            pattern = '^Store=VMemKV/Variant=Bloom-T1InlineValue/Op=Get/Mode=Hit/Dist=' + args.dist + '/Value=.*threads:1$'
            command += ['--benchmark_filter='+pattern, '--benchmark_min_time=2s', '--benchmark_min_warmup_time=0',
                        '--benchmark_out='+str(dest/'benchmark.json'), '--benchmark_out_format=json']
        save(dest/'benchmark-command.json', {'command': command,
            'environment': {k:v for k,v in env.items() if k.startswith(('VMEMKV_', 'REGRESSION_', 'PAGE_HINT_')) or k=='TMPDIR'},
            'uid': args.uid, 'gid': args.gid})
        with (dest/'console.log').open('w') as log:
            result = subprocess.run(command, env=env, pass_fds=(tracker.fd,) if tracker else (),
                user=args.uid, group=args.gid, extra_groups=[], stdout=log, stderr=subprocess.STDOUT, timeout=180)
        save(dest/'exit.json', {'code': result.returncode})
        result.check_returncode()
        if tracker:
            view = memoryview(tracker.mapping).cast('Q')
            confirmed = sum(int(view[i*3]).bit_count() for i in range(BLOCKS)) if args.variant == 'bitmap' else sum((v & 3)==1 for v in view[:PAGES])
            view.release()
            save(dest/'hint-states-after.json', {'confirmed_pages': confirmed, 'note': '終了後の状態数。mincore省略率ではない。'})
            assert confirmed > 0
    finally:
        if tracker: tracker.close()


def summarize():
    rows = []
    for scenario in ['resident', 'ltm']:
        for dist in ['Uniform', 'Zipf']:
            raw = {v: [json.loads((RUNS/f'r{r}-{scenario}-{dist}-t1-{v}'/'throughput.json').read_text())['ops_per_second']
                       for r in [1, 2]] for v in VARIANTS}
            means = {v: statistics.mean(raw[v]) for v in VARIANTS}
            rows.append({'scenario': scenario, 'distribution': dist, 'threads': 1, **means,
                         'bitmap_vs_proposal1_percent': (means['bitmap']/means['proposal1']-1)*100,
                         'bitmap_vs_hints_percent': (means['bitmap']/means['hints']-1)*100, 'raw': raw})
    save(RUNS/'summary.json', rows)
    return rows


def run():
    assert os.geteuid()==0 and 'SUDO_UID' in os.environ, 'sudoで実行してください'
    uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
    RUNS.mkdir(exist_ok=False); os.chown(RUNS, uid, gid)
    try:
        save(RUNS/'event-formats.json', check_formats())
        save(RUNS/'plan.json', {'variants': VARIANTS, 'threads': 1, 'repetitions': 2, 'measurements': 24,
            'min_time_seconds': 2, 'map_body_bytes': {'hints': PAGES*8, 'bitmap': BLOCKS*24},
            'note': '既存と同じ初期未知ヒント。pread後登録なし。16スレッド・他操作・原因分析へ広げない。'})
        probe()
        common.one('bitmap', 'resident', 'Uniform', 1, 0, uid, gid, verify=True)
        for scenario in ['resident', 'ltm']:
            for dist in ['Uniform', 'Zipf']:
                for rep in [1, 2]:
                    for variant in (VARIANTS if rep==1 else VARIANTS[::-1]):
                        common.one(variant, scenario, dist, 1, rep, uid, gid)
        print(json.dumps(summarize(), ensure_ascii=False, indent=2), flush=True)
    finally:
        for path in RUNS.rglob('*'):
            if not path.is_symlink(): os.chown(path, uid, gid)
        if (OUT/'probe-result.json').exists(): os.chown(OUT/'probe-result.json', uid, gid)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'case'])
    parser.add_argument('--variant', choices=VARIANTS)
    parser.add_argument('--scenario', choices=['resident', 'ltm'])
    parser.add_argument('--dist', choices=['Uniform', 'Zipf'])
    parser.add_argument('--threads', type=int, choices=[1], default=1)
    parser.add_argument('--uid', type=int); parser.add_argument('--gid', type=int)
    parser.add_argument('--dest'); parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.action == 'run': run()
    else: case(args)
