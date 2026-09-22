"""既存libbpfをctypesで利用する固定ファイル用ローダー。常駐デーモン・pinなし。"""
import ctypes as C
import json
import mmap
import os
from pathlib import Path
import re
import resource

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'build/ltm/results/page-residency-hints-20260923'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def check_formats():
    result = {}
    for event in ['mm_filemap_add_to_page_cache', 'mm_filemap_delete_from_page_cache']:
        path = Path('/sys/kernel/tracing/events/filemap') / event / 'format'
        if not path.exists(): path = Path('/sys/kernel/debug/tracing/events/filemap') / event / 'format'
        text = path.read_text()
        for name, offset, size in [('pfn', 8, 8), ('i_ino', 16, 8), ('index', 24, 8), ('s_dev', 32, 4)]:
            assert re.search(r'\b' + name + r';\s*offset:' + str(offset) + r';\s*size:' + str(size) + ';', text), text
        result[event] = text
    return result


class Tracker:
    def __init__(self, path, pages, *, object_path=None, entries=None, entry_bytes=8):
        self.obj, self.links, self.mapping = None, [], None
        self.lib = lib = C.CDLL('libbpf.so.0', use_errno=True)
        definitions = {
            'bpf_object__open_file': (C.c_void_p, [C.c_char_p, C.c_void_p]),
            'libbpf_get_error': (C.c_long, [C.c_void_p]),
            'bpf_object__find_map_by_name': (C.c_void_p, [C.c_void_p, C.c_char_p]),
            'bpf_map__set_max_entries': (C.c_int, [C.c_void_p, C.c_uint]),
            'bpf_object__load': (C.c_int, [C.c_void_p]),
            'bpf_map__fd': (C.c_int, [C.c_void_p]),
            'bpf_map_update_elem': (C.c_int, [C.c_int, C.c_void_p, C.c_void_p, C.c_ulonglong]),
            'bpf_object__find_program_by_name': (C.c_void_p, [C.c_void_p, C.c_char_p]),
            'bpf_program__attach_tracepoint': (C.c_void_p, [C.c_void_p, C.c_char_p, C.c_char_p]),
            'bpf_link__destroy': (C.c_int, [C.c_void_p]),
            'bpf_object__close': (None, [C.c_void_p]),
        }
        for name, (ret, args) in definitions.items():
            fn = getattr(lib, name); fn.restype = ret; fn.argtypes = args
        resource.setrlimit(resource.RLIMIT_MEMLOCK, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
        self.obj = self.ptr(lib.bpf_object__open_file(os.fsencode(object_path or OUT / 'page_residency.bpf.o'), None))
        try:
            array = self.ptr(lib.bpf_object__find_map_by_name(self.obj, b'states'))
            entries = pages if entries is None else entries
            assert lib.bpf_map__set_max_entries(array, entries) == 0
            assert lib.bpf_object__load(self.obj) == 0, 'BPF load failed'
            cfg = self.ptr(lib.bpf_object__find_map_by_name(self.obj, b'target'))
            class Target(C.Structure):
                _fields_ = [('inode', C.c_uint64), ('dev', C.c_uint32), ('pages', C.c_uint32)]
            st = os.stat(path)
            value = Target(st.st_ino, (os.major(st.st_dev) << 20) | os.minor(st.st_dev), pages)
            key = C.c_uint32(0)
            assert lib.bpf_map_update_elem(lib.bpf_map__fd(cfg), C.byref(key), C.byref(value), 0) == 0
            self.fd = lib.bpf_map__fd(array)
            self.length = ((entries * entry_bytes + 4095) // 4096) * 4096
            self.mapping = mmap.mmap(self.fd, self.length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
            for name, event in [('page_added', 'mm_filemap_add_to_page_cache'), ('page_deleted', 'mm_filemap_delete_from_page_cache')]:
                prog = self.ptr(lib.bpf_object__find_program_by_name(self.obj, name.encode()))
                self.links.append(self.ptr(lib.bpf_program__attach_tracepoint(prog, b'filemap', event.encode())))
        except Exception:
            self.close(); raise

    def ptr(self, value):
        error = self.lib.libbpf_get_error(value)
        if not value or error: raise RuntimeError('libbpf error: ' + str(error))
        return value

    def close(self):
        for link in self.links: self.lib.bpf_link__destroy(link)
        self.links.clear()
        if self.mapping is not None: self.mapping.close(); self.mapping = None
        if self.obj: self.lib.bpf_object__close(self.obj); self.obj = None


def probe():
    assert os.geteuid() == 0, 'sudoで実行してください'
    save(OUT / 'event-formats.json', check_formats())
    path = OUT / 'probe-pages.tmp'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    tracker = None
    try:
        os.write(fd, b'x' * 8192); os.fsync(fd)
        os.posix_fadvise(fd, 0, 8192, os.POSIX_FADV_DONTNEED)
        tracker = Tracker(path, 2)
        def state(): return [int.from_bytes(tracker.mapping[i*8:i*8+8], 'little') for i in range(2)]
        rows = {'initial': state()}
        assert os.pread(fd, 8192, 0) == b'x' * 8192
        rows['added'] = state()
        assert all(v >= 4 and v % 4 == 0 for v in rows['added'])
        # この専用ファイルを触るのはこのプロセスのみ。確認済み→失効の反映を見る。
        for i, value in enumerate(rows['added']): tracker.mapping[i*8:i*8+8] = (value | 1).to_bytes(8, 'little')
        rows['confirmed'] = state()
        os.posix_fadvise(fd, 0, 8192, os.POSIX_FADV_DONTNEED)
        rows['deleted'] = state()
        assert all(v % 4 == 2 and v > old for v, old in zip(rows['deleted'], rows['confirmed']))
        assert os.pread(fd, 8192, 0) == b'x' * 8192
        rows['readded'] = state()
        assert all(v % 4 == 0 and v > old for v, old in zip(rows['readded'], rows['deleted']))
        save(OUT / 'probe-result.json', {'pass': True, 'states': rows})
        print('PASS: BPF map mmap、追加→確認済み→削除→再追加の反映を確認', flush=True)
    finally:
        if tracker: tracker.close()
        os.close(fd); path.unlink()


if __name__ == '__main__':
    probe()
