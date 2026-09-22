#pragma once
// 固定checkpoint・1 Storeの試作専用。BPFローダーの生存中だけ使用する。
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <sys/mman.h>

namespace page_residency {
inline uint64_t *states = nullptr;
inline uint64_t pages = 0;
inline const void *target_mapping = nullptr;

inline void initialize(const void *mapping, uint64_t used) {
  const char *fd = std::getenv("PAGE_HINT_FD");
  if (!fd) return;  // ロード不可時は案1。
  if (states) throw std::runtime_error("one Store only");
  const char *count = std::getenv("PAGE_HINT_PAGES");
  if (!count) throw std::runtime_error("PAGE_HINT_PAGES missing");
  pages = std::strtoull(count, nullptr, 10);
  if (pages != (used + 4095) / 4096) throw std::runtime_error("hint range mismatch");
  void *ptr = ::mmap(nullptr, (pages * 8 + 4095) & ~uint64_t{4095},
                     PROT_READ | PROT_WRITE, MAP_SHARED, std::atoi(fd), 0);
  if (ptr == MAP_FAILED) throw std::runtime_error("hint mmap failed");
  states = static_cast<uint64_t *>(ptr);
  target_mapping = mapping;
}

struct Ticket {
  uint64_t first = 0, before[2] = {};
  bool active = false, hit = false;
  void confirm(const unsigned char *resident) const {
    if (!active) return;
    for (unsigned i = 0; i < 2; ++i) {
      if ((resident[i] & 1) && (before[i] & 3) == 0) {
        uint64_t expected = before[i];
        __atomic_compare_exchange_n(states + first + i, &expected, expected | 1,
                                    false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
      }
    }
  }
};

inline Ticket observe(const void *mapping, uint64_t offset, uint64_t read_len) {
  Ticket t;
  if (!states || mapping != target_mapping || read_len > 4096 ||
      read_len <= 4096 - offset % 4096) return t;
  t.first = offset / 4096;
  if (t.first + 1 >= pages) return t;
  t.active = true;
  for (unsigned i = 0; i < 2; ++i)
    t.before[i] = __atomic_load_n(states + t.first + i, __ATOMIC_ACQUIRE);
  t.hit = (t.before[0] & 3) == 1 && (t.before[1] & 3) == 1;
  return t;
}

// 実際の共有状態は変更せず、世代変化・登録禁止のCASを小さく確認する。
inline void verify_registration() {
  uint64_t *saved = states;
  uint64_t local[2] = {4, 4};
  states = local;
  unsigned char yes[2] = {1, 1};
  Ticket t{0, {4, 4}, true, false};
  t.confirm(yes);
  if (local[0] != 5 || local[1] != 5) throw std::runtime_error("confirm failed");
  local[0] = 10;  // 削除イベント後。
  local[1] = 12;  // 削除・再追加で世代が変化。
  t.confirm(yes);
  if (local[0] != 10 || local[1] != 12) throw std::runtime_error("stale confirm accepted");
  t.before[0] = 10; t.before[1] = 12;
  t.confirm(yes);
  if (local[0] != 10 || local[1] != 13) throw std::runtime_error("blocked registration accepted");
  states = saved;
}
}  // namespace page_residency
