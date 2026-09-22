#!/usr/bin/env python3
"""案1の保存済みソースから、常駐ヒントだけを追加する実験版を作る。"""
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import run_fixed_pread_1kb as base
from page_residency_loader import OUT, save

HERE = Path(__file__).resolve().parent
OUT.mkdir(exist_ok=True)
source = OUT / 'source/hints'
shutil.copytree(base.OUT / 'source/proposal1', source, dirs_exist_ok=True)
header = source / 'src/vmemkv/read_path.hpp'
old = header.read_text()
text = '#include "page_residency_hints.hpp"\n' + old
begin = text.index('  thread_local static std::vector<unsigned char> tl_mincore_vec;')
end = text.index('\n  const auto *header =', begin)
block = text[begin:end]
block = base.replace_once(block, '  for (unsigned char page_status : tl_mincore_vec)',
                          '  ticket.confirm(tl_mincore_vec.data());\n  for (unsigned char page_status : tl_mincore_vec)')
text = text[:begin] + '  const auto ticket = page_residency::observe(mapping, offset, read_len);\n  if (!ticket.hit) {\n' + block + '\n  }\n' + text[end:]
header.write_text(text)
shutil.copy2(HERE / 'page_residency_hints.hpp', header.parent)
(OUT / 'candidate.patch').write_text(''.join(difflib.unified_diff(old.splitlines(True), text.splitlines(True), fromfile='proposal1/read_path.hpp', tofile='hints/read_path.hpp')))
harness = source / 'fixed_pread_benchmark.cpp'
text = harness.read_text()
text = base.replace_once(text, '    prepare_cache(*store, resident);',
    '    page_residency::initialize(store->t2().get_memory()->base_mmap_scan, store->t2().bytes_used());\n    prepare_cache(*store, resident);')
text = base.replace_once(text, '    prepare_cache(store, false);',
    '    page_residency::verify_registration();\n    page_residency::initialize(store.t2().get_memory()->base_mmap_scan, store.t2().bytes_used());\n    prepare_cache(store, false);')
text = text.replace('pass < 2', 'pass < 3').replace('two passes; miss', 'three passes; miss; generation CAS')
harness.write_text(text)
command = json.loads((base.OUT / 'build-proposal1.json').read_text())
command = [s.replace(str(base.OUT / 'source/proposal1'), str(source)) for s in command]
command[-1] = str(OUT / 'hints')
save(OUT / 'build-hints-command.json', command)
with (OUT / 'build-hints.log').open('w') as log:
    subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
link = OUT / 'proposal1'
if not link.exists(): link.symlink_to(base.OUT / 'proposal1')
files = [Path(__file__), HERE / 'page_residency_hints.hpp', HERE / 'page_residency.bpf.c',
         HERE / 'page_residency_loader.py', OUT / 'hints', OUT / 'proposal1', OUT / 'page_residency.bpf.o']
save(OUT / 'provenance.json', {'commit': subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
    'branch': subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
    'sha256': {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    'baseline_provenance': str(base.OUT / 'provenance.json'), 'note': '製品作業ツリーは案1のみ。実験コピーに常駐ヒントを追加。'})
print('built hints; proposal1 reused', flush=True)
