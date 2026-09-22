// 1KB Getの方向性確認用。単件Getのみ。製品のAPI/保存形式は変更しない。
// prepare: 同じcheckpointから密詰め/ページ内配置の読取り専用データを作る。
// run: 現行Get、または境界またぎだけ既存mincore/pread経路を使うGetを比較する。
#include <benchmark/benchmark.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#include <barrier>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
#include <sstream>
#include <vmemkv/vmemkv.hpp>

using Store = vmemkv::VMemKVStore;
using Clock = std::chrono::steady_clock;
constexpr uint64_t kPage = 4096;

static auto key_for(uint64_t index) -> std::string {
  char key[32];
  std::snprintf(key, sizeof(key), "k%015zx", static_cast<size_t>(index));
  return key;
}

static auto value_for(uint64_t index) -> std::string {
  std::string value(index % 5 == 0 ? 8 : 1024, '\0');
  uint64_t state = index * 0x9E3779B97F4A7C15ULL + 1;
  for (size_t offset = 0; offset < value.size(); offset += 8) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    std::memcpy(value.data() + offset, &state, std::min(size_t{8}, value.size() - offset));
  }
  return value;
}

// bench_kv.cppと同じZipf(alpha=1.0)生成式・seed・キー生成・全バイト走査。
class Zipf {
 public:
  explicit Zipf(size_t count)
      : count_(count), low_(std::log(1.5) - 1.0), high_(std::log(count + 0.5)),
        s_(1.0 - std::exp(std::log(1.5) - 1.0)) {}
  auto operator()(std::mt19937_64 &rng) const -> size_t {
    std::uniform_real_distribution<double> uniform(0, 1);
    while (true) {
      const double u = high_ + uniform(rng) * (low_ - high_);
      const double x = std::exp(u);
      const size_t rank = std::clamp(static_cast<size_t>(std::llround(x)), size_t{1}, count_);
      if (rank - x <= s_ || u >= std::log(rank + 0.5) - std::exp(-std::log(rank))) return rank - 1;
    }
  }
 private:
  size_t count_;
  double low_, high_, s_;
};

static auto touch(std::span<const std::byte> value) -> uint64_t {
  uint64_t checksum = 0;
  for (std::byte b : value) checksum = checksum * 131 + static_cast<uint8_t>(b);
  return checksum;
}

// get_impl()の不変base用の経路だけを試作。tail/不一致は現行Getへ戻す。
// 同じT1検索・インライン値処理・キー照合・コールバックを使用する。
template <typename Callback>
static auto cross_page_get(Store &store, const std::string &key, Callback callback) -> bool {
  const auto bytes = std::as_bytes(std::span(key.data(), key.size()));
  const auto res = store.impl().t1().get_with_hash(bytes);
  if (res.payload_bits == vmemkv::STORE_NOT_FOUND) return false;
  if (t1_detail::is_inline(res.raw_hash)) {
    std::array<std::byte, 8> buffer;
    const auto size = t1_detail::decode_size(res.raw_hash);
    vmemkv::copy_inline_value(res.payload_bits, size, buffer.data());
    callback(std::span<const std::byte>(buffer.data(), size));
    return true;
  }
  const auto *mem = store.t2().get_memory();
  const uint64_t offset = res.payload_bits & vmemkv::detail::kPayloadOffsetMask;
  const uint64_t boundary = mem->base_boundary.load(std::memory_order_acquire);
  if (offset < boundary) {
    const uint64_t hint = vmemkv::base_size_hint(res.payload_bits);
    thread_local std::vector<std::byte> cold;
    const auto record = hint <= kPage - offset % kPage
        ? vmemkv::read_base_record_via(mem->base, offset, boundary)
        : vmemkv::read_large_get_cold(mem, offset, boundary, hint, &cold);
    if (record && vmemkv::byte_span_equal(record->key, bytes)) {
      callback(record->value);
      return true;
    }
  }
  return store.get(key, callback);
}

static void write_bytes(int fd, const void *data, size_t length) {
  const auto *ptr = static_cast<const char *>(data);
  while (length) {
    const ssize_t n = ::write(fd, ptr, length);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) throw std::system_error(errno, std::generic_category(), "write");
    ptr += n;
    length -= static_cast<size_t>(n);
  }
}

// T1のshard構成・キー・hash・inline値を保持し、非inline値のoffsetだけ更新。
// ベースラインも同じ変換を通すので、不要レコード除去の効果は両配置で共通。
static void prepare(const std::string &source, const std::string &dest, bool padded) {
  const auto manifest = vmemkv::read_manifest(vmemkv::derive_manifest_path(source));
  if (!manifest) throw std::runtime_error("source manifest missing");
  vmemkv::ShardedT1CheckpointFile index(vmemkv::derive_t1_chk_path(source));
  const int input = ::open(vmemkv::derive_t2_chk_path(source).c_str(), O_RDONLY);
  if (input < 0) throw std::runtime_error("source T2 open failed");
  struct stat info{};
  if (::fstat(input, &info) != 0) throw std::runtime_error("source T2 stat failed");
  auto *mapping = static_cast<std::byte *>(::mmap(nullptr, manifest->t2_bytes_used, PROT_READ, MAP_SHARED, input, 0));
  if (mapping == MAP_FAILED) throw std::runtime_error("source T2 mmap failed");
  ::madvise(mapping, manifest->t2_bytes_used, MADV_SEQUENTIAL);
  const auto output_path = vmemkv::derive_t2_chk_path(dest);
  const int output = ::open(output_path.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
  if (output < 0) throw std::system_error(errno, std::generic_category(), "destination T2 create");
  vmemkv::ShardedT1CheckpointWriter writer(vmemkv::derive_t1_chk_path(dest));
  struct Entry { vmemkv::T1ChkKeyPrefix key; uint64_t hash; uint64_t payload_bits; };
  std::vector<std::byte> buffer;
  buffer.reserve(8ULL << 20);
  auto flush = [&] { write_bytes(output, buffer.data(), buffer.size()); buffer.clear(); };
  uint64_t used = 0, total = 0, external = 0, crossings = 0, padding = 0;
  for (size_t shard = 0; shard < index.shard_count(); ++shard) {
    std::vector<Entry> entries;
    entries.reserve(index.shard_entries(shard).size());
    for (const auto &old : index.shard_entries(shard)) {
      Entry next{old.key_prefix, old.hash, old.payload_bits};
      ++total;
      if (!t1_detail::is_inline(old.hash)) {
        const auto offset = old.payload_bits & vmemkv::detail::kPayloadOffsetMask;
        if (offset + sizeof(ValueRecordHeader) > manifest->t2_bytes_used) throw std::runtime_error("bad offset");
        const auto *header = reinterpret_cast<const ValueRecordHeader *>(mapping + offset);
        const auto record = make_record_view(header);
        const uint64_t size = vmemkv::record_aligned_len(sizeof(ValueRecordHeader), header->key_len, header->alloc_len);
        if (header->key_len != 16 || header->value_len != 1024 || header->alloc_len != 1024 ||
            offset + size > manifest->t2_bytes_used || std::memcmp(record.key.data(), old.key_prefix.data(), 16) != 0)
          throw std::runtime_error("prepare supports this 1KB read-only corpus only");
        if (padded && used % kPage + size > kPage) {
          const uint64_t gap = kPage - used % kPage;
          buffer.insert(buffer.end(), gap, std::byte{0});
          used += gap;
          padding += gap;
        }
        crossings += used % kPage + size > kPage;
        next.payload_bits = (old.payload_bits & ~vmemkv::detail::kPayloadOffsetMask) | used;
        buffer.insert(buffer.end(), mapping + offset, mapping + offset + size);
        used += size;
        ++external;
        if (buffer.size() >= (8ULL << 20)) flush();
      }
      entries.push_back(next);
    }
    writer.add_shard(std::span<const Entry>(entries));
  }
  flush();
  if (::ftruncate(output, info.st_size) != 0 || ::fsync(output) != 0) throw std::runtime_error("T2 finish failed");
  ::posix_fadvise(output, 0, used, POSIX_FADV_DONTNEED);
  ::close(output);
  writer.finish(index.boundaries());
  vmemkv::write_manifest(vmemkv::derive_manifest_path(dest), manifest->generation, used);
  ::munmap(mapping, manifest->t2_bytes_used);
  ::close(input);
  std::cout << "{\"keys\":" << total << ",\"external\":" << external << ",\"crossings\":" << crossings
            << ",\"padding_bytes\":" << padding << ",\"t2_bytes\":" << used << "}\n";
}

static auto read_file(const std::filesystem::path &path) -> std::string {
  std::ifstream input(path);
  return {std::istreambuf_iterator<char>(input), {}};
}

static auto read_bytes() -> uint64_t {
  std::istringstream input(read_file("/proc/self/io"));
  std::string name;
  uint64_t value = 0;
  while (input >> name >> value) if (name == "read_bytes:") return value;
  return 0;
}

static auto cpu_seconds(const rusage &usage) -> double {
  return usage.ru_utime.tv_sec + usage.ru_stime.tv_sec +
         (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1e6;
}

static auto cgroup_path() -> std::filesystem::path {
  const auto text = read_file("/proc/self/cgroup");
  const auto begin = text.find("0::/");
  if (begin == std::string::npos) throw std::runtime_error("cgroup v2 required");
  return std::filesystem::path("/sys/fs/cgroup") / text.substr(begin + 4, text.find('\n', begin) - begin - 4);
}

static void snapshot(const std::filesystem::path &out, const std::string &phase) {
  const auto cg = cgroup_path();
  for (const auto *name : {"memory.current", "memory.high", "memory.max", "memory.swap.current", "memory.stat",
                           "memory.events", "memory.pressure", "io.stat", "cpu.stat"})
    std::ofstream(out / (phase + "." + name)) << read_file(cg / name);
}

static auto drop_t2(Store &store) -> uint64_t {
  const auto *mem = store.t2().get_memory();
  const uint64_t length = mem->bytes_used.load();
  for (auto *mapping : {mem->base, mem->base_mmap_scan, mem->base_mmap_scan_seq})
    if (mapping && ::madvise(mapping, length, MADV_DONTNEED) != 0) throw std::runtime_error("madvise failed");
  const int error = ::posix_fadvise(mem->read_fd, 0, (length + kPage - 1) & ~(kPage - 1), POSIX_FADV_DONTNEED);
  if (error != 0) throw std::system_error(error, std::generic_category(), "drop private experiment T2 cache");
  std::vector<unsigned char> residency((length + kPage - 1) / kPage);
  if (::mincore(mem->base, length, residency.data()) != 0) throw std::runtime_error("mincore failed");
  return std::count_if(residency.begin(), residency.end(), [](unsigned char x) { return x & 1; });
}

static void verify(Store &store, uint64_t count) {
  // 境界前後を含む先頭2048件とコーパス全体の固定seedサンプル2048件。
  std::mt19937_64 rng(42);
  for (uint64_t i = 0; i < std::min(count, uint64_t{4096}); ++i) {
    const uint64_t index = i < 2048 ? i : rng() % count;
    const auto key = key_for(index), expected = value_for(index);
    auto check = [&](std::span<const std::byte> value) {
      if (value.size() != expected.size() || std::memcmp(value.data(), expected.data(), value.size()) != 0)
        throw std::runtime_error("value mismatch: " + key);
    };
    if (!cross_page_get(store, key, check) || !store.get(key, check)) throw std::runtime_error("missing key");
  }
  if (cross_page_get(store, key_for(count + 1), [](auto) {})) throw std::runtime_error("miss reported as hit");
}

static void smoke(const std::string &path) {
  // 試作Getのtail経路、checkpoint後、baseの更新、削除を小さい専用DBで確認。
  if (std::filesystem::exists(vmemkv::derive_manifest_path(path))) throw std::runtime_error("smoke path exists");
  Store store(path, 64ULL << 20);
  store.bulk_load(4096, key_for, value_for);
  verify(store, 4096);
  store.checkpoint();
  drop_t2(store);
  verify(store, 4096);
  const auto key = key_for(3);
  const std::string replacement(1024, 'x');
  if (!store.update(key, replacement)) throw std::runtime_error("update failed");
  if (!cross_page_get(store, key, [&](auto v) {
        if (v.size() != replacement.size() || std::memcmp(v.data(), replacement.data(), v.size()) != 0)
          throw std::runtime_error("stale update");
      })) throw std::runtime_error("updated key missing");
  if (!store.remove(key) || cross_page_get(store, key, [](auto) {})) throw std::runtime_error("delete failed");
  std::cout << "smoke: PASS\n";
}

template <bool Cross>
static void measure(Store &store, const std::string &distribution, size_t count, int threads,
                    double warmup, double seconds, const std::filesystem::path &out) {
  std::barrier sync(threads + 1);
  std::vector<uint64_t> operations(threads);
  std::vector<std::jthread> workers;
  for (int thread = 0; thread < threads; ++thread) workers.emplace_back([&, thread] {
    Zipf zipf(count);
    std::uniform_int_distribution<size_t> uniform(0, count - 1);
    const bool skewed = distribution == "Zipf";
    auto run = [&](double duration, uint64_t seed) {
      std::mt19937_64 rng(seed);
      uint64_t completed = 0;
      const auto until = Clock::now() + std::chrono::duration<double>(duration);
      do {
        for (int i = 0; i < 64; ++i) {
          const auto index = skewed ? zipf(rng) : uniform(rng);
          const auto key = key_for(index);
          auto callback = [](auto value) { benchmark::DoNotOptimize(touch(value)); };
          const bool found = Cross ? cross_page_get(store, key, callback) : store.get(key, callback);
          if (!found) std::abort();
          ++completed;
        }
      } while (Clock::now() < until);
      return completed;
    };
    sync.arrive_and_wait();
    // 同じ短い要求列の再生でUniformがキャッシュヒットする偏りを避ける。
    run(warmup, (42 + thread) ^ 0xBAD5EEDULL);
    sync.arrive_and_wait();
    sync.arrive_and_wait();
    operations[thread] = run(seconds, 42 + thread);
    sync.arrive_and_wait();
  });
  sync.arrive_and_wait();
  sync.arrive_and_wait();
  snapshot(out, "before");
  const auto stats_before = store.get_statistics();
  const uint64_t io_before = read_bytes();
  rusage cpu_before{}, cpu_after{};
  ::getrusage(RUSAGE_SELF, &cpu_before);
  const auto start = Clock::now();
  sync.arrive_and_wait();
  sync.arrive_and_wait();
  const double elapsed = std::chrono::duration<double>(Clock::now() - start).count();
  ::getrusage(RUSAGE_SELF, &cpu_after);
  const uint64_t io_after = read_bytes();
  const auto stats_after = store.get_statistics();
  snapshot(out, "after");
  const uint64_t total = std::accumulate(operations.begin(), operations.end(), uint64_t{0});
  std::ofstream result(out / "result.json");
  result << std::setprecision(12)
         << "{\"operations\":" << total << ",\"elapsed_seconds\":" << elapsed
         << ",\"ops_per_second\":" << total / elapsed
         << ",\"cpu_seconds\":" << cpu_seconds(cpu_after) - cpu_seconds(cpu_before)
         << ",\"major_faults\":" << cpu_after.ru_majflt - cpu_before.ru_majflt
         << ",\"read_bytes\":" << io_after - io_before
         << ",\"checkpoints\":" << stats_after.checkpoint_count - stats_before.checkpoint_count
         << ",\"splits\":" << stats_after.t1_split_count - stats_before.t1_split_count
         << ",\"defrag_cycles\":" << stats_after.defrag_cycle_count - stats_before.defrag_cycle_count
         << ",\"t2_bytes\":" << store.t2().bytes_used() << "}\n";
  std::cout << "ops/s=" << total / elapsed << ", read B/op=" << double(io_after - io_before) / total << '\n';
}

int main(int argc, char **argv) try {
  if (argc < 3) throw std::runtime_error("prepare SRC DEST padded|dense; smoke PATH; verify PATH COUNT; run PATH MODE DIST THREADS COUNT WARMUP SECONDS OUT resident|ltm");
  const std::string command = argv[1];
  if (command == "prepare" && argc == 5) { prepare(argv[2], argv[3], std::string(argv[4]) == "padded"); return 0; }
  if (command == "smoke" && argc == 3) { smoke(argv[2]); return 0; }
  const std::string path = argv[2];
  const auto manifest = vmemkv::read_manifest(vmemkv::derive_manifest_path(path));
  if (!manifest) throw std::runtime_error("missing experiment checkpoint");
  struct stat info{};
  if (::stat(vmemkv::derive_t2_chk_path(path).c_str(), &info) != 0) throw std::runtime_error("missing T2");
  Store store(path, info.st_size);
  if (command == "verify" && argc == 4) {
    drop_t2(store);
    verify(store, std::stoull(argv[3]));
    std::cout << "verify: PASS\n";
    return 0;
  }
  if (command != "run" || argc != 11) throw std::runtime_error("invalid run arguments");
  const std::string mode = argv[3], distribution = argv[4];
  if ((mode != "baseline" && mode != "cross-pread") || (distribution != "Uniform" && distribution != "Zipf"))
    throw std::runtime_error("invalid mode/distribution");
  const int threads = std::stoi(argv[5]);
  const size_t count = std::stoull(argv[6]);
  const double warmup = std::stod(argv[7]), seconds = std::stod(argv[8]);
  const std::filesystem::path out = argv[9];
  std::filesystem::create_directories(out);
  const auto resident = drop_t2(store);
  std::ofstream(out / "cache-reset.json") << "{\"resident_t2_pages_after_drop\":" << resident << "}\n";
  if (resident != 0) throw std::runtime_error("T2 cache was not fully dropped");
  if (std::string(argv[10]) == "resident") {
    const auto *mem = store.t2().get_memory();
    // 常駐準備のI/Oは大きなreadで済ませ、測定区間から除く。
    std::vector<std::byte> warm_buffer(1ULL << 20);
    for (uint64_t offset = 0; offset < store.t2().bytes_used(); offset += warm_buffer.size()) {
      const size_t size = std::min<uint64_t>(warm_buffer.size(), store.t2().bytes_used() - offset);
      if (::pread(mem->read_fd, warm_buffer.data(), size, offset) != static_cast<ssize_t>(size))
        throw std::runtime_error("resident preparation read failed");
    }
    // 両Getが使うVMAのPTEも揃えて温め、片方だけminor faultする初期化差を避ける。
    for (auto *mapping : {mem->base, mem->base_mmap_scan}) {
      if (!mapping) continue;
      for (uint64_t offset = 0; offset < store.t2().bytes_used(); offset += kPage) {
        auto byte = *reinterpret_cast<const volatile std::byte *>(mapping + offset);
        benchmark::DoNotOptimize(byte);
      }
    }
  }
  std::cout << "ready: mode=" << mode << " dist=" << distribution << " threads=" << threads << std::endl;
  if (mode == "cross-pread") measure<true>(store, distribution, count, threads, warmup, seconds, out);
  else measure<false>(store, distribution, count, threads, warmup, seconds, out);
  return 0;
} catch (const std::exception &error) {
  std::cerr << error.what() << '\n';
  return 1;
}
