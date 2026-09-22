#!/usr/bin/env python3
"""常駐の劣化解消を元実装との直接比較で確認。既存バイナリ、4条件各2回。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess

import run_page_residency_snapshot_matrix as matrix
from page_residency_loader import check_formats, save

HERE=Path(__file__).resolve().parent
MATRIX=matrix.OUT
ORIGINAL=matrix.base.OUT.parent/'resident-mincore-diagnosis-20260923'
OUT=matrix.SNAPSHOT/'original-resident'
RUNS=OUT/'measurements'
VARIANTS=['original','snapshot']
matrix.OUT=OUT
matrix.common.OUT, matrix.common.RUNS, matrix.common.SCRIPT=OUT,RUNS,Path(__file__).resolve()


def prepare():
    OUT.mkdir(exist_ok=False)
    hashes={}
    for variant,source,provenance in [
        ('original',ORIGINAL/'original',ORIGINAL/'provenance.json'),
        ('snapshot',MATRIX/'snapshot',MATRIX/'provenance.json')]:
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        assert digest==json.loads(provenance.read_text())['sha256'][str(source)]
        (OUT/variant).symlink_to(source)
        hashes[str(source)]=digest
    hashes[str(Path(__file__))]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    save(OUT/'provenance.json',{'sha256':hashes,'original_provenance':str(ORIGINAL/'provenance.json'),
        'snapshot_provenance':str(MATRIX/'provenance.json'),
        'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
        'note':'再実装・再ビルドなし。元実装と案1の保存ソースはGet条件式以外同一、harnessも同一と確認。過去の絶対速度は合算しない。'})
    print('prepared existing original/snapshot binaries',flush=True)


def run():
    assert os.geteuid()==0 and 'SUDO_UID' in os.environ, 'sudoで実行してください'
    uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
    RUNS.mkdir(exist_ok=False); os.chown(RUNS,uid,gid)
    try:
        save(RUNS/'event-formats.json',check_formats())
        save(RUNS/'plan.json',{'scenario':'resident','distributions':['Uniform','Zipf'],'threads':[1,16],
            'variants':VARIANTS,'repetitions':2,'total_runs':16,'benchmark_min_time_seconds':2,
            'cpu_1':[0],'cpu_16':list(range(0,32,2)),'memory_high_max_GiB':[16,32],
            'purpose':'案1適用前の元実装と比べて常駐時の劣化が解消したか確認。LTMは再測定しない。'})
        rows=[]
        for dist in ['Uniform','Zipf']:
            for threads in [1,16]:
                for rep in [1,2]:
                    for variant in (VARIANTS if rep==1 else VARIANTS[::-1]):
                        matrix.common.one(variant,'resident',dist,threads,rep,uid,gid)
                raw={v:[json.loads((RUNS/f'r{rep}-resident-{dist}-t{threads}-{v}'/'throughput.json').read_text())['ops_per_second']
                        for rep in [1,2]] for v in VARIANTS}
                a,b=(statistics.mean(raw[v]) for v in VARIANTS)
                rows.append({'scenario':'resident','distribution':dist,'threads':threads,
                             'original':a,'snapshot':b,'change_percent':100*(b/a-1),'raw':raw})
                save(RUNS/'summary.json',rows)
        print('完了: 常駐4条件×2版×2回。BPF解除済み。',flush=True)
    finally:
        for p in RUNS.rglob('*'):
            if not p.is_symlink(): os.chown(p,uid,gid)
        os.chown(RUNS,uid,gid)


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['prepare','run','case'])
    p.add_argument('--variant',choices=VARIANTS); p.add_argument('--scenario',choices=['resident'])
    p.add_argument('--dist',choices=['Uniform','Zipf']); p.add_argument('--threads',type=int,choices=[1,16])
    p.add_argument('--uid',type=int); p.add_argument('--gid',type=int); p.add_argument('--dest')
    args=p.parse_args()
    if args.action=='prepare': prepare()
    elif args.action=='run': run()
    else: matrix.case(args)
