// 単件GetのI/O観測専用。既存の試作Getをそのまま再利用する。
// デバイス統計はホスト全体なので、対象プロセスのread_bytesとの一致も記録する。
#define main prototype_main
#include "get_hit_1kb.cpp"
#undef main

static auto disk_stat(const char *path) -> std::array<uint64_t, 11> {
  std::istringstream input(read_file(path));
  std::array<uint64_t, 11> result{};
  for (auto &item : result) if (!(input >> item)) throw std::runtime_error("disk stat unavailable");
  return result;
}

static auto residency(const vmemkv::T2Memory *mem, uint64_t offset, uint64_t length) -> std::string {
  std::vector<unsigned char> pages(length / kPage);
  if (::mincore(mem->base + offset, length, pages.data()) != 0) throw std::runtime_error("mincore failed");
  std::string result;
  for (auto page : pages) result += (page & 1) ? '1' : '0';
  return result;
}

int main(int argc, char **argv) try {
  if (argc != 5) throw std::runtime_error("inspect_get_hit_io PATH OUT SAMPLES now|wait");
  const std::string path = argv[1];
  const std::filesystem::path out = argv[2];
  const size_t samples = std::stoull(argv[3]);
  std::filesystem::create_directories(out);
  struct stat info{};
  if (::stat(vmemkv::derive_t2_chk_path(path).c_str(), &info)) throw std::runtime_error("T2 stat failed");
  Store store(path, info.st_size);
  const auto *mem = store.t2().get_memory();
  const uint64_t used = store.t2().bytes_used();
  struct Key { uint64_t index, offset, hint; bool crossing; std::string key, expected; };
  std::vector<Key> keys;
  std::mt19937_64 rng(20260922);
  size_t crossings = 0, contained = 0;
  while (crossings < samples || contained < samples / 4) {
    const uint64_t index = rng() % 8259552;
    const auto key = key_for(index);
    const auto found = store.impl().t1().get_with_hash(std::as_bytes(std::span(key.data(), key.size())));
    if (found.payload_bits == vmemkv::STORE_NOT_FOUND || t1_detail::is_inline(found.raw_hash)) continue;
    const uint64_t offset = found.payload_bits & vmemkv::detail::kPayloadOffsetMask;
    const uint64_t hint = vmemkv::base_size_hint(found.payload_bits);
    const bool crossing = offset % kPage + 1064 > kPage;
    if (offset < (1ULL << 20) || offset + (1ULL << 20) >= used ||
        (crossing ? crossings >= samples : contained >= samples / 4)) continue;
    // hintだけが境界を越える例を、今回の二分類には混ぜない。
    if (crossing != (offset % kPage + hint > kPage)) continue;
    if (std::any_of(keys.begin(), keys.end(), [&](const Key &other) {
          return std::max(offset, other.offset) - std::min(offset, other.offset) < (2ULL << 20);
        })) continue;
    keys.push_back({index, offset, hint, crossing, key, value_for(index)});
    crossing ? ++crossings : ++contained;
  }
  // 初回のコード・スレッドローカルバッファ確保は観測前に済ませる。
  for (const auto &key : keys) {
    if (!cross_page_get(store, key.key, [](auto value) { benchmark::DoNotOptimize(touch(value)); }) ||
        !store.get(key.key, [](auto value) { benchmark::DoNotOptimize(touch(value)); }))
      throw std::runtime_error("warmup miss");
  }
  snapshot(out, "before");
  std::ofstream(out / "pid") << ::getpid() << '\n';
  if (std::string(argv[4]) == "wait") {
    std::ofstream(out / "ready") << "waiting for block trace\n";
    const auto deadline = Clock::now() + std::chrono::minutes(15);
    while (!std::filesystem::exists(out / "start")) {
      if (Clock::now() >= deadline) throw std::runtime_error("trace start timed out");
      ::usleep(100000);
    }
  }
  std::ofstream results(out / "operations.jsonl");
  for (int rep = 0; rep < 2; ++rep) {
    for (int pass = 0; pass < 2; ++pass) {
      const bool cross = (pass ^ rep) != 0;
      for (size_t n = 0; n < keys.size(); ++n) {
        const auto &key = keys[n];
        const uint64_t page = key.offset & ~(kPage - 1);
        const uint64_t reset_start = page - (128 * kPage);
        const uint64_t reset_length = 256 * kPage;
        for (auto *mapping : {mem->base, mem->base_mmap_scan, mem->base_mmap_scan_seq})
          if (mapping && ::madvise(mapping + reset_start, reset_length, MADV_DONTNEED))
            throw std::runtime_error("madvise failed");
        if (::posix_fadvise(mem->read_fd, reset_start, reset_length, POSIX_FADV_DONTNEED))
          throw std::runtime_error("fadvise failed");
        const auto before_pages = residency(mem, page, 16 * kPage);
        if (before_pages != std::string(16, '0')) throw std::runtime_error("pages not cold");
        // 直前の空区間も記録し、背景I/Oの有無を確認する。
        const auto idle0 = disk_stat("/sys/block/sda/stat");
        ::usleep(2000);
        const auto idle1 = disk_stat("/sys/block/sda/stat");
        const uint64_t io0 = read_bytes();
        rusage u0{}, u1{};
        ::getrusage(RUSAGE_THREAD, &u0);
        const auto device0 = disk_stat("/sys/block/sda/stat");
        timespec begin{}, end{};
        ::clock_gettime(CLOCK_MONOTONIC, &begin);
        uint64_t checksum = 0;
        auto callback = [&](auto value) { checksum = touch(value); };
        const bool found = cross ? cross_page_get(store, key.key, callback) : store.get(key.key, callback);
        ::clock_gettime(CLOCK_MONOTONIC, &end);
        ::getrusage(RUSAGE_THREAD, &u1);
        const uint64_t io1 = read_bytes();
        // 先読みの完了を含める。Get latency自体には含めない。
        ::usleep(2000);
        const auto device1 = disk_stat("/sys/block/sda/stat");
        if (!found || checksum != touch(std::as_bytes(std::span(key.expected.data(), key.expected.size()))))
          throw std::runtime_error("value mismatch");
        const uint64_t t0 = uint64_t(begin.tv_sec) * 1000000000 + begin.tv_nsec;
        const uint64_t t1 = uint64_t(end.tv_sec) * 1000000000 + end.tv_nsec;
        results << "{\"rep\":" << rep << ",\"mode\":\"" << (cross ? "cross-pread" : "baseline")
          << "\",\"sample\":" << n << ",\"index\":" << key.index << ",\"offset\":" << key.offset
          << ",\"hint\":" << key.hint << ",\"crossing\":" << (key.crossing ? "true" : "false")
          << ",\"start_ns\":" << t0 << ",\"end_ns\":" << t1 << ",\"latency_ns\":" << t1 - t0
          << ",\"read_bytes\":" << io1 - io0 << ",\"major_faults\":" << u1.ru_majflt - u0.ru_majflt
          << ",\"voluntary_switches\":" << u1.ru_nvcsw - u0.ru_nvcsw
          << ",\"involuntary_switches\":" << u1.ru_nivcsw - u0.ru_nivcsw
          << ",\"device_reads\":" << device1[0] - device0[0]
          << ",\"device_read_sectors\":" << device1[2] - device0[2]
          << ",\"device_merges\":" << device1[1] - device0[1]
          << ",\"idle_reads\":" << idle1[0] - idle0[0]
          << ",\"resident_before\":\"" << before_pages
          << "\",\"resident_after\":\"" << residency(mem, page, 16 * kPage) << "\"}\n";
      }
    }
  }
  snapshot(out, "after");
  std::ofstream(out / "done") << "PASS\n";
  std::cout << "PASS: " << keys.size() * 4 << " single Gets\n";
  return 0;
} catch (const std::exception &error) {
  std::cerr << error.what() << '\n';
  return 1;
}
