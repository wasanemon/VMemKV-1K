#pragma once
// 固定checkpoint・1 Storeの試作。64bit世代の周回は扱わない。
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <sys/mman.h>

namespace page_residency {
struct Block { uint64_t resident, blocked, generation; };
static_assert(sizeof(Block) == 24);
inline Block *states = nullptr;
inline uint64_t pages = 0;
inline const void *target_mapping = nullptr;
inline uint64_t load(const uint64_t *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }

inline void initialize(const void *mapping, uint64_t used) {
  const char *fd = std::getenv("PAGE_HINT_FD");
  if (!fd) return;
  if (states) throw std::runtime_error("one Store only");
  const char *count = std::getenv("PAGE_HINT_PAGES");
  if (!count) throw std::runtime_error("PAGE_HINT_PAGES missing");
  pages = std::strtoull(count, nullptr, 10);
  if (pages != (used + 4095) / 4096) throw std::runtime_error("hint range mismatch");
  const uint64_t bytes = ((pages + 63) / 64) * sizeof(Block);
  void *ptr = ::mmap(nullptr, (bytes + 4095) & ~uint64_t{4095},
                     PROT_READ | PROT_WRITE, MAP_SHARED, std::atoi(fd), 0);
  if (ptr == MAP_FAILED) throw std::runtime_error("hint mmap failed");
  states = static_cast<Block *>(ptr);
  target_mapping = mapping;
}

// 登録前後で世代と登録禁止を確認。後検査で失効を見つけたら登録を取り消す。
// 一時的に古いヒントが見える窓と、他の正当な登録も消す偽陰性は許容する。
// ヒントは経路選択だけに使い、不変mmapの通常faultを妨げない。
inline void confirm_bits(Block &b, uint64_t bits, uint64_t before) {
  bits &= ~load(&b.blocked) & ~load(&b.resident);
  if (!bits || load(&b.generation) != before) return;
  __atomic_fetch_or(&b.resident, bits, __ATOMIC_ACQ_REL);
  if (load(&b.generation) != before || (load(&b.blocked) & bits))
    __atomic_fetch_and(&b.resident, ~bits, __ATOMIC_ACQ_REL);
}

struct Ticket {
  uint64_t first = 0, before[2] = {};
  bool active = false, hit = false;
  void confirm(const unsigned char *resident) const {
    if (!active || hit) return;
    const uint64_t group = first >> 6, bit = uint64_t{1} << (first & 63);
    if ((first & 63) != 63) {
      const uint64_t mask = ((resident[0] & 1) ? bit : 0) |
                            ((resident[1] & 1) ? bit << 1 : 0);
      confirm_bits(states[group], mask, before[0]);
    } else {
      if (resident[0] & 1) confirm_bits(states[group], bit, before[0]);
      if (resident[1] & 1) confirm_bits(states[group + 1], 1, before[1]);
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
  const uint64_t group = t.first >> 6, bit = uint64_t{1} << (t.first & 63);
  if ((t.first & 63) != 63) {
    const uint64_t mask = bit | (bit << 1);
    t.hit = (load(&states[group].resident) & mask) == mask;
  } else {
    t.hit = (load(&states[group].resident) & bit) && (load(&states[group + 1].resident) & 1);
  }
  // 常駐ヒット時には世代・blockedを参照しない。
  if (!t.hit) {
    t.before[0] = load(&states[group].generation);
    if ((t.first & 63) == 63) t.before[1] = load(&states[group + 1].generation);
  }
  return t;
}

inline void verify_registration() {
  Block *saved = states;
  const uint64_t saved_pages = pages;
  const void *saved_mapping = target_mapping;
  Block local[2] = {{0, 0, 4}, {0, 0, 4}};
  states = local; pages = 128; target_mapping = local;
  unsigned char yes[2] = {1, 1};
  auto ticket = observe(local, 4000, 1064);
  ticket.confirm(yes);
  if (!observe(local, 4000, 1064).hit || local[0].resident != 3)
    throw std::runtime_error("same word registration failed");
  ticket = observe(local, 63 * 4096 + 4000, 1064);
  ticket.confirm(yes);
  if (!observe(local, 63 * 4096 + 4000, 1064).hit)
    throw std::runtime_error("word boundary registration failed");
  local[0] = {0, 0, 5};
  ticket = observe(local, 4000, 1064);
  local[0].generation++;
  ticket.confirm(yes);
  if (local[0].resident) throw std::runtime_error("stale generation accepted");
  ticket = observe(local, 4000, 1064);
  local[0].blocked = 1;  // 削除通知の途中、世代更新前でもこのページを登録しない。
  ticket.confirm(yes);
  if (local[0].resident != 2) throw std::runtime_error("blocked registration accepted");
  states = saved; pages = saved_pages; target_mapping = saved_mapping;
}
}  // namespace page_residency
