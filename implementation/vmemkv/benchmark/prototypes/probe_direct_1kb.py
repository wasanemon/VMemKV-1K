#!/usr/bin/env python3
"""既存不変T2の少数読取りで512B DIOとページキャッシュ非投入を確認。"""
import ctypes as c
import errno
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'build/ltm/results/cold-direct-1kb-20260923'
PATH = ROOT / 'build/ltm/data/get-hit-prototypes-20260922/dense.t2chk'
lib = c.CDLL(None, use_errno=True)
lib.mmap.restype = c.c_void_p
lib.mmap.argtypes = [c.c_void_p, c.c_size_t, c.c_int, c.c_int, c.c_int, c.c_long]
lib.pread.restype = c.c_ssize_t
lib.pread.argtypes = [c.c_int, c.c_void_p, c.c_size_t, c.c_long]
lib.mincore.argtypes = [c.c_void_p, c.c_size_t, c.c_void_p]
lib.munmap.argtypes = [c.c_void_p, c.c_size_t]
fd = os.open(PATH, os.O_RDONLY)
dio = os.open(PATH, os.O_RDONLY | os.O_DIRECT | os.O_CLOEXEC)
buf = c.c_void_p()
assert lib.posix_memalign(c.byref(buf), 4096, 8192) == 0
rows = []
try:
    for length in [1536, 2048]:
        record = next(i*1064 for i in range(1, 1024)
                      if i*1064 % 4096 + 1072 > 4096
                      and ((i*1064 % 512 + 1072 + 511)//512)*512 == length)
        offset = record // 512 * 512
        page = record // 4096 * 4096
        expected = os.pread(fd, length, offset)
        assert len(expected) == length and any(expected)
        mapping = lib.mmap(None, 8192, 1, 1, fd, page)
        assert mapping != c.c_void_p(-1).value
        def residency():
            v = (c.c_ubyte * 2)()
            assert lib.mincore(mapping, 8192, v) == 0
            return [int(x & 1) for x in v]
        os.posix_fadvise(fd, page, 8192, os.POSIX_FADV_DONTNEED)
        before = residency()
        assert before == [0, 0], before
        got = lib.pread(dio, buf, length, offset)
        after = residency()
        assert got == length, (got, c.get_errno())
        assert c.string_at(buf, length) == expected
        assert after == [0, 0], ('DIO populated page cache', after)
        c.set_errno(0)
        unaligned = lib.pread(dio, buf, length, offset + 1)
        error = c.get_errno()
        assert unaligned == -1 and error == errno.EINVAL
        assert os.pread(fd, length, offset) == expected
        control = residency()
        assert control == [1, 1], control
        rows.append(dict(record_offset=record, dio_offset=offset, length=length,
                         buffer_alignment=4096, before=before, after_direct=after,
                         after_buffered=control, unaligned_errno=error, value_equal=True))
        assert lib.munmap(mapping, 8192) == 0
finally:
    lib.free(buf)
    os.close(dio)
    os.close(fd)
OUT.mkdir(parents=True, exist_ok=True)
result = dict(path=str(PATH), kernel=os.uname().release, cases=rows,
              conclusion='512B-aligned DIO reads succeeded; no page-cache population; buffered controls populated both pages')
(OUT / 'direct-probe.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
