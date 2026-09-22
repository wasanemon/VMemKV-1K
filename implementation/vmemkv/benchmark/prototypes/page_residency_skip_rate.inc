// 計数専用。既存harnessのpath/size/count取得直後に挿入する。
  if (argc == 3 && std::string(argv[1]) == "--count-hints") {
    const std::string dist = argv[2];
    if (dist != "Uniform" && dist != "Zipf") throw std::runtime_error("unknown distribution");
    RegressionStore store(path, RegressionStore::ConfigType::DefaultT2CapacityBytes);
    page_residency::initialize(store.t2().get_memory()->base_mmap_scan, store.t2().bytes_used());
    if (!page_residency::states) throw std::runtime_error("BPF map required");
    prepare_cache(store, true);  // T2は常駐、ヒントは初期未知。
    std::mt19937_64 rng(kBenchmarkSeed);
    ZipfDistribution zipf({count, 1.0});
    std::uniform_int_distribution<std::size_t> uniform_index(0, count - 1);
    constexpr uint64_t window = 100000;
    for (uint64_t block = 0; block < 10; ++block) {
      page_residency::count_target = page_residency::count_skipped = 0;
      for (uint64_t i = 0; i < window; ++i) {
        const auto index = dist == "Zipf" ? zipf(rng) : uniform_index(rng);
        if (!store.get(make_key(index), [](std::span<const std::byte> value) {
          benchmark::DoNotOptimize(touch_bytes(value));
        })) throw std::runtime_error("Get missing key");
      }
      std::cout << "{\"distribution\":\"" << dist << "\",\"get_begin\":" << block*window
                << ",\"get_end\":" << (block+1)*window
                << ",\"target_gets\":" << page_residency::count_target
                << ",\"mincore_skipped\":" << page_residency::count_skipped << "}\n";
    }
    return 0;
  }
