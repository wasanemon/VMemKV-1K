// regression_throughput.cppの実験用コピーのmainに挿入。通常測定には入らない。
  if (argc == 2 && std::string(argv[1]) == "--verify-policy") {
    RegressionStore store(path, RegressionStore::ConfigType::DefaultT2CapacityBytes);
    read_policy_prototype::configure_fd(store.t2().get_memory()->read_fd);
    read_policy_prototype::verify_policy();
    prepare_cache(store, false);
    for (int pass = 0; pass < 2; ++pass) {
      std::mt19937_64 rng(42);
      for (size_t i = 0; i < 1024; ++i) {
        const auto index = i < 512 ? i : rng() % count;
        const auto expected = make_value_for_key(index, size);
        if (!store.get(make_key(index), [&](auto bytes) {
          if (bytes.size() != expected.size() || std::memcmp(bytes.data(), expected.data(), bytes.size()) != 0)
            throw std::runtime_error("Get value mismatch");
        })) throw std::runtime_error("Get missing key");
      }
    }
    if (store.get(make_key(count + 1), [](auto) {})) throw std::runtime_error("false Get hit");
    std::cout << "verify: PASS (1024 keys, two passes; miss; adaptive transitions)\n";
    return 0;
  }
