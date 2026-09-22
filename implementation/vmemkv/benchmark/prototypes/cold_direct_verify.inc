    // 同じページ跨ぎレコードで00/10/01/11を作り、返却先と値を確認する。
    cold_direct_prototype::configure(store.t2().get_memory()->read_fd);
    {
      const auto *mem = store.t2().get_memory();
      constexpr uint64_t offset = 3192, hint = 1072;
      const auto boundary = mem->base_boundary.load();
      std::array<std::byte, 1064> expected;
      if (::pread(mem->read_fd, expected.data(), expected.size(), offset) != 1064)
        throw std::runtime_error("verify reference read failed");
      std::vector<std::byte> cold;
      for (int state : {0, 1, 2, 3}) {
        prepare_cache(store, false);
        for (int page = 0; page < 2; ++page) if (state & (1 << page)) {
          auto byte = *reinterpret_cast<const volatile std::byte *>(mem->base + page * 4096);
          benchmark::DoNotOptimize(byte);
        }
        unsigned char resident[2];
        if (::mincore(mem->base_mmap_scan, 8192, resident) ||
            (resident[0] & 1) != (state & 1) || (resident[1] & 1) != ((state >> 1) & 1))
          throw std::runtime_error("verify residency setup failed");
        auto record = vmemkv::read_large_get_cold(mem, offset, boundary, hint, &cold);
        const auto *wanted = state == 0 ? cold_direct_prototype::buffer() + offset % 512
                           : state == 3 ? mem->base_mmap_scan + offset : cold.data();
        if (!record || reinterpret_cast<const std::byte *>(record->header) != wanted ||
            std::memcmp(record->header, expected.data(), expected.size()))
          throw std::runtime_error("verify read route or bytes failed");
        if (state == 0 && (::mincore(mem->base_mmap_scan, 8192, resident) ||
                           (resident[0] & 1) || (resident[1] & 1)))
          throw std::runtime_error("verify direct read populated cache");
      }
      // DIOが使用不可なら同じ呼出し内のbuffered preadへ戻る。
      prepare_cache(store, false);
      const int saved = cold_direct_prototype::file.fd;
      cold_direct_prototype::file.fd = -1;
      const auto fallback = vmemkv::read_large_get_cold(mem, offset, boundary, hint, &cold);
      cold_direct_prototype::file.fd = saved;
      if (!fallback || reinterpret_cast<const std::byte *>(fallback->header) != cold.data() ||
          std::memcmp(fallback->header, expected.data(), expected.size()))
        throw std::runtime_error("verify direct fallback failed");
      std::cout << "routes: PASS (00 direct, 10/01 buffered, 11 mmap; fallback)\n";
    }
