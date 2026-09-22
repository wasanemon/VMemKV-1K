// 不変・16Bキー・1KB外部値だけの配置試作。既存の生成器・T1・計測部品を再利用。
// 通常checkpointへの書戻しや更新は行わない。製品の保存形式ではない。
#define main original_get_hit_prototype_main
#include "get_hit_1kb.cpp"
#undef main

constexpr uint64_t kValueBytes = 1024;
constexpr uint64_t kMetaBytes = sizeof(ValueRecordHeader) + 16;
constexpr uint64_t kDenseBytes = kMetaBytes + kValueBytes;
static_assert(kMetaBytes == 40 && kDenseBytes == 1064);

struct PackedFile {
  int fd;
  uint64_t size;
  std::byte *data;
  explicit PackedFile(const std::string &path) {
    fd = ::open(path.c_str(), O_RDONLY);
    struct stat st{};
    if (fd < 0 || ::fstat(fd, &st) != 0 || st.st_size <= 0 || st.st_size % kValueBytes)
      throw std::runtime_error("invalid packed value file");
    size = st.st_size;
    data = static_cast<std::byte *>(::mmap(nullptr, size, PROT_READ, MAP_SHARED, fd, 0));
    if (data == MAP_FAILED || ::madvise(data, size, MADV_RANDOM) != 0)
      throw std::runtime_error("packed mmap failed");
  }
  ~PackedFile() { ::munmap(data, size); ::close(fd); }
  void drop() const {
    if (::madvise(data, size, MADV_DONTNEED) != 0 ||
        ::posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED) != 0)
      throw std::runtime_error("packed cache reset failed");
    std::vector<unsigned char> pages((size + kPage - 1) / kPage);
    if (::mincore(data, size, pages.data()) != 0 ||
        std::any_of(pages.begin(), pages.end(), [](auto x) { return x & 1; }))
      throw std::runtime_error("packed values not cold after reset");
  }
};

static void prepare_packed(const std::string &source, const std::string &values,
                           const std::string &metadata) {
  const auto manifest = vmemkv::read_manifest(vmemkv::derive_manifest_path(source));
  if (!manifest) throw std::runtime_error("source manifest missing");
  vmemkv::ShardedT1CheckpointFile index(vmemkv::derive_t1_chk_path(source));
  const int input = ::open(vmemkv::derive_t2_chk_path(source).c_str(), O_RDONLY);
  if (input < 0) throw std::runtime_error("source open failed");
  auto *data = static_cast<std::byte *>(::mmap(nullptr, manifest->t2_bytes_used, PROT_READ, MAP_SHARED, input, 0));
  if (data == MAP_FAILED) throw std::runtime_error("source mmap failed");
  ::madvise(data, manifest->t2_bytes_used, MADV_SEQUENTIAL);
  const int output = ::open(values.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
  const int meta = ::open(metadata.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
  if (output < 0 || meta < 0) throw std::runtime_error("destination must be new files");
  std::vector<std::byte> vb, mb;
  vb.reserve(8ULL << 20);
  mb.reserve(1ULL << 20);
  auto flush = [&] {
    write_bytes(output, vb.data(), vb.size()); vb.clear();
    write_bytes(meta, mb.data(), mb.size()); mb.clear();
  };
  uint64_t slots = 0, keys = 0, inlined = 0;
  for (size_t shard = 0; shard < index.shard_count(); ++shard) {
    for (const auto &entry : index.shard_entries(shard)) {
      ++keys;
      if (t1_detail::is_inline(entry.hash)) {
        if (t1_detail::decode_size(entry.hash) != 8) throw std::runtime_error("expected 8B inline");
        ++inlined;
        continue;
      }
      const auto offset = entry.payload_bits & vmemkv::detail::kPayloadOffsetMask;
      if (offset != slots * kDenseBytes || offset + kDenseBytes > manifest->t2_bytes_used)
        throw std::runtime_error("requires existing compact, fixed-record corpus");
      const auto *header = reinterpret_cast<const ValueRecordHeader *>(data + offset);
      const auto record = make_record_view(header);
      if (header->key_len != 16 || header->value_len != kValueBytes || header->alloc_len != kValueBytes ||
          std::memcmp(record.key.data(), entry.key_prefix.data(), 16) != 0)
        throw std::runtime_error("requires exact 16B keys and immutable 1KB values");
      vb.insert(vb.end(), record.value.begin(), record.value.end());
      mb.insert(mb.end(), data + offset, data + offset + kMetaBytes);
      ++slots;
      if (vb.size() >= (8ULL << 20)) flush();
    }
  }
  if (slots * kDenseBytes != manifest->t2_bytes_used) throw std::runtime_error("source has trailing records");
  flush();
  if (::fsync(output) != 0 || ::fsync(meta) != 0) throw std::runtime_error("fsync failed");
  ::posix_fadvise(output, 0, 0, POSIX_FADV_DONTNEED);
  ::posix_fadvise(meta, 0, 0, POSIX_FADV_DONTNEED);
  ::close(output); ::close(meta);
  ::munmap(data, manifest->t2_bytes_used); ::close(input);
  std::cout << "{\"keys\":" << keys << ",\"inline_keys\":" << inlined << ",\"slots\":" << slots
            << ",\"values_bytes\":" << slots * kValueBytes << ",\"metadata_bytes\":" << slots * kMetaBytes
            << ",\"dense_bytes\":" << manifest->t2_bytes_used << "}\n";
}

template <typename Callback>
static bool packed_get(Store &store, const PackedFile &values, const std::string &key, Callback callback) {
  // 全キーが16Bの専用形式。T1の16B prefix比較がキー全体の一致を確定する。
  if (key.size() != 16) return false;
  const auto res = store.impl().t1().get_with_hash(std::as_bytes(std::span(key.data(), key.size())));
  if (res.payload_bits == vmemkv::STORE_NOT_FOUND) return false;
  if (t1_detail::is_inline(res.raw_hash)) {
    std::array<std::byte, 8> buffer;
    const auto size = t1_detail::decode_size(res.raw_hash);
    vmemkv::copy_inline_value(res.payload_bits, size, buffer.data());
    callback(std::span<const std::byte>(buffer.data(), size));
    return true;
  }
  // T1を再構築せず、検証済み密詰めoffsetをslot番号として解釈する試作。
  // メタデータは別ファイルに保存するが、Getでは開かず、T2元レコードも読まない。
  const auto offset = res.payload_bits & vmemkv::detail::kPayloadOffsetMask;
  const auto value_offset = (offset / kDenseBytes) * kValueBytes;
  if (value_offset + kValueBytes > values.size) throw std::runtime_error("invalid slot");
  callback(std::span<const std::byte>(values.data + value_offset, kValueBytes));
  return true;
}

static void verify_packed(Store &store, const PackedFile &values, uint64_t count) {
  std::mt19937_64 rng(42);
  for (uint64_t i = 0; i < 4096; ++i) {
    const auto index = i < 1024 ? i : (i < 2048 ? count - 1 - (i - 1024) : rng() % count);
    const auto key = key_for(index), expected = value_for(index);
    auto check = [&](auto value) {
      if (value.size() != expected.size() || std::memcmp(value.data(), expected.data(), value.size()) != 0)
        throw std::runtime_error("value mismatch: " + key);
    };
    if (!packed_get(store, values, key, check) || !store.get(key, check)) throw std::runtime_error("missing key");
  }
  for (uint64_t i = 0; i < 16; ++i)
    if (packed_get(store, values, key_for(count + i), [](auto) {})) throw std::runtime_error("unexpected hit");
  std::cout << "verify: PASS (4096 values, 16 misses)\n";
}

template <bool Packed>
static void measure_packed(Store &store, const PackedFile &values, size_t count, int threads,
                           const std::filesystem::path &out) {
  std::barrier sync(threads + 1);
  std::vector<uint64_t> operations(threads);
  std::vector<std::jthread> workers;
  for (int thread = 0; thread < threads; ++thread) workers.emplace_back([&, thread] {
    std::uniform_int_distribution<size_t> uniform(0, count - 1);
    auto run = [&](double seconds, uint64_t seed) {
      std::mt19937_64 rng(seed);
      uint64_t completed = 0;
      const auto until = Clock::now() + std::chrono::duration<double>(seconds);
      do {
        for (int i = 0; i < 64; ++i) {
          const auto key = key_for(uniform(rng));
          auto callback = [](auto value) { benchmark::DoNotOptimize(touch(value)); };
          const bool found = Packed ? packed_get(store, values, key, callback) : store.get(key, callback);
          if (!found) std::abort();
          ++completed;
        }
      } while (Clock::now() < until);
      return completed;
    };
    sync.arrive_and_wait();
    run(12, (42 + thread) ^ 0xBAD5EEDULL);
    sync.arrive_and_wait(); sync.arrive_and_wait();
    operations[thread] = run(10, 42 + thread);
    sync.arrive_and_wait();
  });
  sync.arrive_and_wait(); sync.arrive_and_wait();
  const auto start = Clock::now();
  sync.arrive_and_wait(); sync.arrive_and_wait();
  const double elapsed = std::chrono::duration<double>(Clock::now() - start).count();
  const auto total = std::accumulate(operations.begin(), operations.end(), uint64_t{0});
  std::ofstream(out / "result.json") << std::setprecision(12)
    << "{\"operations\":" << total << ",\"elapsed_seconds\":" << elapsed
    << ",\"ops_per_second\":" << total / elapsed << "}\n";
  std::cout << "ops/s=" << total / elapsed << '\n';
}

int main(int argc, char **argv) try {
  if (argc == 5 && std::string(argv[1]) == "prepare") {
    prepare_packed(argv[2], argv[3], argv[4]); return 0;
  }
  if (argc < 5) throw std::runtime_error("verify SOURCE VALUES COUNT; run SOURCE VALUES COUNT MODE THREADS OUT");
  const std::string command = argv[1], source = argv[2];
  const auto count = std::stoull(argv[4]);
  struct stat st{};
  if (::stat(vmemkv::derive_t2_chk_path(source).c_str(), &st) != 0) throw std::runtime_error("source T2 missing");
  // 両版で同一Storeを開き、同じT1・設定を使う。packed側では元のT2をGetしない。
  Store store(source, st.st_size);
  PackedFile values(argv[3]);
  if (command == "verify" && argc == 5) { verify_packed(store, values, count); return 0; }
  if (command != "run" || argc != 8) throw std::runtime_error("invalid arguments");
  const std::string mode = argv[5];
  const auto threads = std::stoi(argv[6]);
  if ((mode != "baseline" && mode != "packed") || (threads != 1 && threads != 16))
    throw std::runtime_error("unsupported mode or thread count");
  const std::filesystem::path out = argv[7];
  const auto cg = cgroup_path();
  const auto high = read_file(cg / "memory.high"), maximum = read_file(cg / "memory.max");
  std::ofstream(out / "memory-limits.json") << "{\"high\":" << high << ",\"max\":" << maximum << "}\n";
  if (std::stoull(high) != (1ULL << 30) || std::stoull(maximum) != (2ULL << 30))
    throw std::runtime_error("LTM limits not applied");
  if (drop_t2(store) != 0) throw std::runtime_error("source T2 not cold after reset");
  values.drop();
  std::ofstream(out / "cache-reset.json") << "{\"source_resident_pages\":0,\"values_resident_pages\":0}\n";
  std::cout << "ready: " << mode << ", threads=" << threads << std::endl;
  if (mode == "packed") measure_packed<true>(store, values, count, threads, out);
  else measure_packed<false>(store, values, count, threads, out);
  return 0;
} catch (const std::exception &error) {
  std::cerr << error.what() << '\n'; return 1;
}
