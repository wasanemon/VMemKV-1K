// 既存harnessのprepare_cacheを再利用し、Get数を固定して3版を比較する。
#include <chrono>
#include <iomanip>
#ifdef SNAPSHOT_HINTS
#include "vmemkv/page_residency_snapshot.hpp"
#endif

int main(int argc, char **argv) try {
  std::cout << std::setprecision(12);
#ifdef SNAPSHOT_HINTS
  if (argc == 2 && std::string(argv[1]) == "--selftest") {
    page_residency::verify_snapshot();
    std::cout << "PASS: mixed residency, chunk/tail boundary, generation/blocked guards\n";
    return 0;
  }
#endif
  if (argc != 2) throw std::runtime_error("variant required");
  const std::string variant = argv[1];
  const auto path = required_env("REGRESSION_DB");
  const size_t size = std::stoull(required_env("REGRESSION_VALUE"));
  const size_t count = std::stoull(required_env("REGRESSION_COUNT"));
  RegressionStore store(path, RegressionStore::ConfigType::DefaultT2CapacityBytes);
  if (store.t2().get_memory()->base_boundary.load() != store.t2().bytes_used())
    throw std::runtime_error("immutable checkpoint only");
  using Clock = std::chrono::steady_clock;
  auto seconds = [](auto begin, auto end) { return std::chrono::duration<double>(end-begin).count(); };
  double attach_seconds = 0, snapshot_seconds = 0;
  uint64_t resident_pages = 0, snapshot_calls = 0, known_before = 0, events_before = 0;
#ifdef SNAPSHOT_HINTS
  if (variant != "empty" && variant != "snapshot") throw std::runtime_error("bad hint variant");
  const auto attach_begin = Clock::now();
  page_residency::initialize(store.t2().get_memory()->base_mmap_scan, store.t2().bytes_used());
  attach_seconds = seconds(attach_begin, Clock::now());
  if (!page_residency::states) throw std::runtime_error("BPF map required");
#else
  if (variant != "proposal1") throw std::runtime_error("bad baseline variant");
#endif
  prepare_cache(store, true); // 全版同じ常駐データ・PTE。ヒントはまだ未知。
#ifdef SNAPSHOT_HINTS
  if (variant == "snapshot") {
    const auto begin = Clock::now();
    const auto result = page_residency::snapshot(store.t2().bytes_used());
    snapshot_seconds = seconds(begin, Clock::now());
    resident_pages = result.resident_pages; snapshot_calls = result.calls;
  }
  for (uint64_t g = 0; g < (page_residency::pages + 63) / 64; ++g) {
    known_before += __builtin_popcountll(page_residency::load(&page_residency::states[g].resident));
    events_before += page_residency::load(&page_residency::states[g].generation);
  }
#endif
  std::mt19937_64 rng(kBenchmarkSeed);
  std::uniform_int_distribution<std::size_t> uniform_index(0, count - 1);
  constexpr uint64_t gets = 1000000;
  const auto begin = Clock::now();
  for (uint64_t i = 0; i < gets; ++i) {
    const auto index = uniform_index(rng);
    if (!store.get(make_key(index), [](std::span<const std::byte> value) {
      benchmark::DoNotOptimize(touch_bytes(value));
    })) throw std::runtime_error("Get missing key");
  }
  const double get_seconds = seconds(begin, Clock::now());
#ifdef SNAPSHOT_COUNTERS
  const auto target_gets = page_residency::count_target, skipped = page_residency::count_skipped;
#endif
  uint64_t events_after = 0, known_after = 0;
#ifdef SNAPSHOT_HINTS
  for (uint64_t g = 0; g < (page_residency::pages + 63) / 64; ++g) {
    known_after += __builtin_popcountll(page_residency::load(&page_residency::states[g].resident));
    events_after += page_residency::load(&page_residency::states[g].generation);
  }
#endif
  // 計時終了後に少数の値を照合。測定前のヒント学習には使わない。
  std::mt19937_64 verify_rng(kBenchmarkSeed);
  for (size_t i = 0; i < 1024; ++i) {
    const auto index = i < 512 ? i : verify_rng() % count;
    const auto expected = make_value_for_key(index, size);
    if (!store.get(make_key(index), [&](auto value) {
      if (value.size() != expected.size() || std::memcmp(value.data(), expected.data(), value.size()))
        throw std::runtime_error("Get value mismatch");
    })) throw std::runtime_error("verify missing key");
  }
  if (store.get(make_key(count + 1), [](auto) {})) throw std::runtime_error("false Get hit");
  std::cout << "{\"variant\":\"" << variant << "\",\"gets\":" << gets
            << ",\"snapshot_seconds\":" << snapshot_seconds
            << ",\"hint_attach_seconds\":" << attach_seconds
            << ",\"snapshot_calls\":" << snapshot_calls
            << ",\"snapshot_resident_pages\":" << resident_pages
            << ",\"known_before\":" << known_before << ",\"known_after\":" << known_after
            << ",\"events_during_gets\":" << events_after-events_before;
#ifdef SNAPSHOT_COUNTERS
  std::cout << ",\"diagnostic\":true,\"target_gets\":" << target_gets
            << ",\"mincore_skipped\":" << skipped;
#else
  std::cout << ",\"get_seconds\":" << get_seconds << ",\"ops_per_second\":" << gets/get_seconds
            << ",\"snapshot_plus_get_seconds\":" << snapshot_seconds+get_seconds;
#endif
  std::cout << ",\"value_check\":true}\n";
  return 0;
} catch (const std::exception &e) { std::cerr << e.what() << '\n'; return 1; }
