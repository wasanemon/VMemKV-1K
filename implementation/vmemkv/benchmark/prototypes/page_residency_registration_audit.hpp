#pragma once
// 1スレッド・常駐条件の確認専用。経路選択には使わない独立の記録。
#include <bit>
#include <iostream>
#include <vector>

namespace page_residency {
struct RegistrationAudit {
  std::vector<uint64_t> confirmed;
  uint64_t initial_generation = 0, initial_known = 0;
  uint64_t target = 0, skipped = 0, expected_skipped = 0, decision_mismatches = 0;
  uint64_t mincore_errors = 0, mincore_nonresident = 0;
  uint64_t resident_page_checks = 0, unregistered_after_confirm = 0;
};
inline thread_local RegistrationAudit registration_audit;

inline void audit_start() {
  auto &a = registration_audit;
  a = {};
  a.confirmed.resize((pages + 63) / 64);
  for (size_t i = 0; i < a.confirmed.size(); ++i) {
    a.initial_generation += load(&states[i].generation);
    a.initial_known += std::popcount(load(&states[i].resident));
  }
}

inline void audit_observe(uint64_t offset, const Ticket &t) {
  if (!t.active) return;
  auto &a = registration_audit;
  ++a.target;
  a.skipped += t.hit;
  bool expected = true;
  // Ticket側のマスク生成とは別に、ファイル内の2ページを1ページずつ照合。
  for (uint64_t page = offset / 4096; page < offset / 4096 + 2; ++page)
    expected &= (a.confirmed[page / 64] & (uint64_t{1} << (page % 64))) != 0;
  a.expected_skipped += expected;
  a.decision_mismatches += expected != t.hit;
}

inline void audit_after_confirm(uint64_t offset, const Ticket &t, const unsigned char *resident) {
  if (!t.active) return;
  auto &a = registration_audit;
  a.mincore_nonresident += !(resident[0] & 1) || !(resident[1] & 1);
  for (unsigned i = 0; i < 2; ++i) {
    if (!(resident[i] & 1)) continue;
    const uint64_t page = offset / 4096 + i, bit = uint64_t{1} << (page % 64);
    a.confirmed[page / 64] |= bit;
    ++a.resident_page_checks;
    a.unregistered_after_confirm += (load(&states[page / 64].resident) & bit) == 0;
  }
}

inline void audit_finish() {
  const auto &a = registration_audit;
  uint64_t generation = 0, missing = 0, known = 0, recorded = 0;
  for (size_t i = 0; i < a.confirmed.size(); ++i) {
    const uint64_t actual = load(&states[i].resident);
    generation += load(&states[i].generation);
    missing += std::popcount(a.confirmed[i] & ~actual);
    known += std::popcount(actual);
    recorded += std::popcount(a.confirmed[i]);
  }
  std::cout << "{\"kind\":\"registration_audit\",\"target_gets\":" << a.target
            << ",\"mincore_skipped\":" << a.skipped
            << ",\"expected_skipped_without_events\":" << a.expected_skipped
            << ",\"decision_mismatches\":" << a.decision_mismatches
            << ",\"mincore_errors\":" << a.mincore_errors
            << ",\"mincore_nonresident\":" << a.mincore_nonresident
            << ",\"resident_page_checks\":" << a.resident_page_checks
            << ",\"unregistered_after_confirm\":" << a.unregistered_after_confirm
            << ",\"initial_known_pages\":" << a.initial_known
            << ",\"recorded_unique_pages\":" << recorded
            << ",\"actual_known_pages_at_end\":" << known
            << ",\"recorded_pages_missing_at_end\":" << missing
            << ",\"generation_events_during_gets\":" << generation - a.initial_generation << "}\n";
}
} // namespace page_residency
