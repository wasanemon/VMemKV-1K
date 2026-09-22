#pragma once
// 実験コピーの既存ビットマップを初期化するだけ。Get/BPFの実装は変更しない。
#include "page_residency_hints.hpp"
#include <algorithm>
#include <array>

namespace page_residency {
struct SnapshotResult { uint64_t calls = 0, resident_pages = 0; };

inline SnapshotResult snapshot(uint64_t used) {
  if (!states || !target_mapping || pages != (used + 4095) / 4096)
    throw std::runtime_error("snapshot range mismatch");
  constexpr uint64_t chunk_pages = 1024; // 4MiBの状態のみ取得。データは読まない。
  std::array<unsigned char, chunk_pages> residency;
  std::array<uint64_t, chunk_pages / 64> before;
  SnapshotResult result;
  for (uint64_t first = 0; first < pages; first += chunk_pages) {
    const auto length = std::min(chunk_pages, pages - first);
    const auto groups = (length + 63) / 64;
    for (uint64_t g = 0; g < groups; ++g)
      before[g] = load(&states[first / 64 + g].generation);
    auto *address = static_cast<unsigned char *>(const_cast<void *>(target_mapping)) + first * 4096;
    if (::mincore(address, length * 4096, residency.data()))
      throw std::runtime_error("snapshot mincore failed");
    ++result.calls;
    for (uint64_t g = 0; g < groups; ++g) {
      uint64_t mask = 0;
      for (uint64_t b = 0; b < std::min<uint64_t>(64, length - g * 64); ++b)
        if (residency[g * 64 + b] & 1) mask |= uint64_t{1} << b;
      result.resident_pages += __builtin_popcountll(mask);
      // mincoreより前の世代を使う。競合時の取消しは既存と同じ。
      confirm_bits(states[first / 64 + g], mask, before[g]);
    }
  }
  return result;
}

// 常駐/非常駐の混在、64ページ境界、チャンク末尾、使用範囲の端だけを確認。
inline void verify_snapshot() {
  verify_registration();
  constexpr uint64_t count = 1025, used = 1024 * 4096 + 1;
  void *mapping = ::mmap(nullptr, count * 4096, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (mapping == MAP_FAILED) throw std::runtime_error("test mmap failed");
  if (::madvise(mapping, count * 4096, MADV_NOHUGEPAGE))
    throw std::runtime_error("test madvise failed");
  for (auto page : {0, 63, 64, 1024})
    static_cast<volatile unsigned char *>(mapping)[page * 4096] = 1;
  Block local[17] = {};
  states = local; pages = count; target_mapping = mapping;
  const auto result = snapshot(used);
  if (result.calls != 2 || result.resident_pages != 4 ||
      local[0].resident != ((uint64_t{1} << 63) | 1) ||
      local[1].resident != 1 || local[16].resident != 1)
    throw std::runtime_error("snapshot test failed");
  for (int i = 2; i < 16; ++i)
    if (local[i].resident) throw std::runtime_error("cold page registered");
  states = nullptr; pages = 0; target_mapping = nullptr;
  ::munmap(mapping, count * 4096);
}
} // namespace page_residency
