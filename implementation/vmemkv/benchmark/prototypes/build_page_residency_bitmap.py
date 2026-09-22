#!/usr/bin/env python3
"""保存済み常駐ヒント版のGet分岐を保ち、管理表現だけを差し替える。"""
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from page_residency_loader import OUT as OLD, save

HERE = Path(__file__).resolve().parent
OUT = OLD.parent / 'page-residency-bitmap-20260923'
OUT.mkdir(exist_ok=True)
source = OUT / 'source/bitmap'
shutil.copytree(OLD / 'source/hints', source, dirs_exist_ok=True)
header = source / 'src/vmemkv/page_residency_hints.hpp'
old = header.read_text()
shutil.copy2(HERE / 'page_residency_bitmap.hpp', header)
(OUT / 'candidate.patch').write_text(''.join(difflib.unified_diff(
    old.splitlines(True), header.read_text().splitlines(True),
    fromfile='hints/page_residency_hints.hpp', tofile='bitmap/page_residency_hints.hpp')))
commands = [
    ['clang', '-target', 'bpf', '-mcpu=v3', '-O2', '-g', '-I/usr/include/x86_64-linux-gnu',
     '-c', str(HERE / 'page_residency_bitmap.bpf.c'), '-o', str(OUT / 'page_residency_bitmap.bpf.o')],
    [s.replace(str(OLD / 'source/hints'), str(source))
     for s in json.loads((OLD / 'build-hints-command.json').read_text())],
]
commands[1][-1] = str(OUT / 'bitmap')
save(OUT / 'build-commands.json', commands)
with (OUT / 'build.log').open('w') as log:
    for command in commands:
        subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
for variant in ['proposal1', 'hints']:
    link = OUT / variant
    if not link.exists(): link.symlink_to(OLD / variant)
files = [Path(__file__), HERE / 'page_residency_bitmap.hpp', HERE / 'page_residency_bitmap.bpf.c',
         HERE / 'page_residency_loader.py', HERE / 'run_page_residency.py',
         HERE / 'run_page_residency_bitmap.py', HERE / 'measure_page_residency_bitmap.sh',
         OUT / 'bitmap', OUT / 'hints', OUT / 'proposal1', OUT / 'page_residency_bitmap.bpf.o']
save(OUT / 'provenance.json', {
    'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
    'branch': subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip(),
    'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    'previous_provenance': str(OLD / 'provenance.json'),
    'note': '通常ソースは案1のみ。実験コピーの常駐表現だけ変更。pread後登録は含めない。'})
print('built bitmap; proposal1 and hints binaries reused', flush=True)
