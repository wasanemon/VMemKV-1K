#!/usr/bin/env python3
"""既存の省略率確認に登録照合だけ追加。常駐Uniformの1回で終了する。"""
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

import run_page_residency_skip_rate as previous
from page_residency_loader import save, check_formats

HERE = Path(__file__).resolve().parent
OLD = previous.OUT
OUT = OLD.parent / 'page-residency-registration-audit-20260923'


def build():
    OUT.mkdir(exist_ok=False)
    source = OUT/'source/bitmap'
    shutil.copytree(OLD/'source/bitmap',source)
    header=source/'src/vmemkv/read_path.hpp'
    old=header.read_text()
    text=previous.base.replace_once(old, '#include "page_residency_hints.hpp"',
        '#include "page_residency_hints.hpp"\n#include "page_residency_registration_audit.hpp"')
    text=previous.base.replace_once(text, '  const auto ticket = page_residency::observe(mapping, offset, read_len);',
        '  const auto ticket = page_residency::observe(mapping, offset, read_len);\n  page_residency::audit_observe(offset, ticket);')
    text=previous.base.replace_once(text, '  ticket.confirm(tl_mincore_vec.data());',
        '  ticket.confirm(tl_mincore_vec.data());\n  page_residency::audit_after_confirm(offset, ticket, tl_mincore_vec.data());')
    marker='  if (::mincore(reinterpret_cast<void *>(aligned_start), aligned_len, tl_mincore_vec.data()) != 0) {'
    text=previous.base.replace_once(text, marker, marker+'\n    if (ticket.active) ++page_residency::registration_audit.mincore_errors;')
    header.write_text(text)
    shutil.copy2(HERE/'page_residency_registration_audit.hpp',header.parent)
    harness=source/'fixed_pread_benchmark.cpp'
    before=harness.read_text()
    after=previous.base.replace_once(before, '    prepare_cache(store, true);  // T2は常駐、ヒントは初期未知。',
        '    prepare_cache(store, true);  // T2は常駐、ヒントは初期未知。\n    page_residency::audit_start();')
    after=previous.base.replace_once(after, '    return 0;\n  }\n\n  if (argc == 2',
        '    page_residency::audit_finish();\n    return 0;\n  }\n\n  if (argc == 2')
    harness.write_text(after)
    patch=''.join(difflib.unified_diff(old.splitlines(True),text.splitlines(True),fromfile='skip/read_path.hpp',tofile='audit/read_path.hpp'))
    patch+=''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='skip/harness.cpp',tofile='audit/harness.cpp'))
    (OUT/'audit.patch').write_text(patch)
    command=[s.replace(str(OLD/'source/bitmap'),str(source)) for s in json.loads((OLD/'build-command.json').read_text())]
    command[-1]=str(OUT/'count-hints')
    save(OUT/'build-command.json',command)
    with (OUT/'build.log').open('w') as log:
        subprocess.run(command,check=True,stdout=log,stderr=subprocess.STDOUT)
    files=[Path(__file__), HERE/'page_residency_registration_audit.hpp', Path(previous.__file__),
           OUT/'count-hints', header, harness, previous.BITMAP/'page_residency_bitmap.bpf.o']
    save(OUT/'provenance.json',{'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
        'sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'note':'BPF・登録・失効は未変更。常駐Uniform1回の登録照合専用。'})
    print('built registration audit',flush=True)


def run():
    assert os.geteuid()==0 and 'SUDO_UID' in os.environ, 'sudoで実行してください'
    uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
    runs=OUT/'runs'; runs.mkdir(exist_ok=False)
    dest=runs/'Uniform'; dest.mkdir(); os.chown(dest,uid,gid)
    try:
        save(runs/'event-formats.json',check_formats())
        save(runs/'plan.json',{'scenario':'resident','distribution':'Uniform','threads':1,'gets':1000000,'repetitions':1,
            'note':'常駐確認直後の登録、独立記録との経路判定一致、終了時のヒント保持を確認。イベント数が0のときに失効なしとして解釈。速度は評価しない。'})
        unit='vmemkv-hint-audit-'+uuid.uuid4().hex[:12]
        command=['systemd-run','--scope','--quiet','--unit='+unit,
            '-p','MemoryHigh=17179869184','-p','MemoryMax=34359738368','-p','MemorySwapMax=1099511627776',
            '--','taskset','-c','0',sys.executable,str(Path(__file__).resolve()),'case',
            '--dist','Uniform','--uid',str(uid),'--gid',str(gid)]
        save(dest/'scope-command.json',command)
        with (dest/'loader.log').open('w') as log:
            try: result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=120)
            except subprocess.TimeoutExpired:
                subprocess.run(['systemctl','stop',unit+'.scope'],check=False)
                raise
        save(dest/'scope-exit.json',{'code':result.returncode})
        result.check_returncode()
        records=[json.loads(line) for line in (dest/'console.log').read_text().splitlines() if line.startswith('{')]
        assert len(records)==11
        audit=records[-1]; assert audit['kind']=='registration_audit'
        assert audit['target_gets']==sum(r['target_gets'] for r in records[:-1])
        assert audit['mincore_skipped']==sum(r['mincore_skipped'] for r in records[:-1])
        save(runs/'summary.json',audit)
        print(json.dumps(audit,ensure_ascii=False,indent=2),flush=True)
    finally:
        for p in runs.rglob('*'):
            if not p.is_symlink(): os.chown(p,uid,gid)
        os.chown(runs,uid,gid)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['build','run','case'])
    parser.add_argument('--dist',choices=['Uniform'])
    parser.add_argument('--uid',type=int);parser.add_argument('--gid',type=int)
    args=parser.parse_args()
    if args.action=='build':build()
    elif args.action=='run':run()
    else:
        previous.OUT=OUT
        previous.case(args)  # 既存のmapロード・制限確認・一般ユーザー実行・解除を再利用。
