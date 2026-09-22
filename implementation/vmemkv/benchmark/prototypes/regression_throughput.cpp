// 公開bench_kv.cppのベンチマーク本体を再利用。実験用コピーでのみビルドする。
#define main original_benchmark_main
#include "bench_kv.cpp"
#undef main

using RegressionStore = vmemkv::VMemKVStore;

static auto required_env(const char *name) -> std::string {
  const char *value = std::getenv(name);
  if (!value) throw std::runtime_error(std::string("missing ") + name);
  return value;
}

static void prepare_cache(RegressionStore &store, bool resident) {
  const auto *mem = store.t2().get_memory();
  const uint64_t used = store.t2().bytes_used();
  if (used == 0) return;
  if (resident) {
    std::vector<std::byte> buffer(1ULL << 20);
    for (uint64_t offset = 0; offset < used; offset += buffer.size()) {
      const auto size = std::min<uint64_t>(buffer.size(), used - offset);
      if (::pread(mem->read_fd, buffer.data(), size, offset) != static_cast<ssize_t>(size))
        throw std::runtime_error("cache warm failed");
    }
    for (auto *mapping : {mem->base, mem->base_mmap_scan, mem->base_mmap_scan_seq})
      if (mapping) for (uint64_t offset = 0; offset < used; offset += 4096) {
        auto byte = *reinterpret_cast<const volatile std::byte *>(mapping + offset);
        benchmark::DoNotOptimize(byte);
      }
  } else {
    const uint64_t length = (used + 4095) & ~uint64_t{4095};
    for (auto *mapping : {mem->base, mem->base_mmap_scan, mem->base_mmap_scan_seq})
      if (mapping && ::madvise(mapping, length, MADV_DONTNEED)) throw std::runtime_error("madvise failed");
    if (::posix_fadvise(mem->read_fd, 0, length, POSIX_FADV_DONTNEED)) throw std::runtime_error("fadvise failed");
    std::vector<unsigned char> pages(length / 4096);
    if (::mincore(mem->base, length, pages.data()) ||
        std::any_of(pages.begin(), pages.end(), [](auto x) { return x & 1; }))
      throw std::runtime_error("T2 cache not cold");
  }
}

int main(int argc, char **argv) try {
  std::cout.setf(std::ios::unitbuf);
  const auto path = required_env("REGRESSION_DB");
  const size_t size = std::stoull(required_env("REGRESSION_VALUE"));
  const size_t count = std::stoull(required_env("REGRESSION_COUNT"));
  if (argc == 2 && std::string(argv[1]) == "--prepare") {
    if (!vmemkv::read_manifest(vmemkv::derive_manifest_path(path))) {
      RegressionStore store(path, RegressionStore::ConfigType::DefaultT2CapacityBytes);
      populate(store, {count, size});
      store.checkpoint();
    }
    const auto manifest = vmemkv::read_manifest(vmemkv::derive_manifest_path(path));
    std::cout << "{\"t2_bytes\":" << manifest->t2_bytes_used << ",\"count\":" << count << "}\n";
    return 0;
  }
  const int threads = std::stoi(required_env("REGRESSION_THREADS"));
  const bool resident = required_env("REGRESSION_SCENARIO") == "resident";
  auto make = [path, resident] {
    auto store = std::make_unique<RegressionStore>(path, RegressionStore::ConfigType::DefaultT2CapacityBytes);
    prepare_cache(*store, resident);
    return store;
  };
  auto corpus = [make](size_t, size_t) { return make(); };
  using Holder = StoreHolder<decltype(make())>;
  auto crud = std::make_shared<Holder>();
  const auto name = RegressionStore::name();
  const auto value = value_label(size);
  const std::vector<int> thread_counts{threads};
  benchmark::Initialize(&argc, argv);
  register_insert_benchmark(std::make_shared<Holder>(), name, value, make, corpus, size, count, thread_counts);
  register_get_benchmarks(crud, name, value, make, size, count, thread_counts);
  register_update_benchmark(crud, name, value, make, size, count, thread_counts);
  register_delete_benchmark(name, value, corpus, size, count, thread_counts);
  register_scan_benchmarks(name, value, make, make, size, count, thread_counts);
  register_ycsb_e_benchmark(std::make_shared<Holder>(), threads, name, value, corpus, size, count);
  // 単件Getを含む混在の追加確認。既存YCSB-Eは95%Scan・5%InsertでGetを含まない。
  register_bench(std::make_shared<Holder>(), benchmark_name(name, "GetUpdate50", std::nullopt, "Zipf", value),
      make_metadata(count, size), make, noop_already_populated_init,
      [count, size](benchmark::State &state, auto &store) {
        std::mt19937_64 rng(kBenchmarkSeed + state.thread_index());
        ZipfDistribution zipf({count, 1.0});
        std::string large(size, 'a'), small(8, 'a');
        uint64_t operation = 0;
        for (auto _ : state) {
          const auto index = zipf(rng);
          const auto key = make_key(index);
          if (operation++ % 2 == 0)
            store.get(key, [](auto bytes) { benchmark::DoNotOptimize(touch_bytes(bytes)); });
          else
            benchmark::DoNotOptimize(store.update(key, size != 8 && index % 5 == 0 ? small : large));
        }
        state.SetItemsProcessed(state.iterations());
      }, thread_counts, false);
  benchmark::RunSpecifiedBenchmarks();
  if (g_active_store_holder) g_active_store_holder->reset_store();
  benchmark::Shutdown();
  return 0;
} catch (const std::exception &error) {
  std::cerr << error.what() << '\n';
  return 1;
}
