#pragma once
// 実験用コピーだけが使用するA/Bの最小部品。両案は併用しない。
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdexcept>
#include <string>

namespace read_policy_prototype {

inline void configure_fd(int &fd) {
#ifdef GET_READ_RANDOM_FD
  const int opened = ::open(("/proc/self/fd/" + std::to_string(fd)).c_str(), O_RDONLY | O_CLOEXEC);
  if (opened < 0) throw std::runtime_error("independent Get fd open failed");
  struct stat old_info{}, new_info{};
  if (::fstat(fd, &old_info) != 0 || ::fstat(opened, &new_info) != 0 ||
      old_info.st_dev != new_info.st_dev || old_info.st_ino != new_info.st_ino ||
      ::posix_fadvise(opened, 0, 0, POSIX_FADV_RANDOM) != 0) {
    ::close(opened);
    throw std::runtime_error("Get fd setup failed");
  }
  ::close(fd);
  fd = opened;  // 既存T2Memoryのデストラクタがcloseする。mmap側のfd状態は変更しない。
#else
  (void)fd;
#endif
}

struct ResidencyPolicy {
  enum Mode { Check, Mmap, Pread } mode = Check;
  const void *owner = nullptr;
  unsigned samples = 0, warm = 0, ticks = 0;

  void bind(const void *next) {
    if (owner != next) { *this = {}; owner = next; }
  }
  bool probe() { return mode == Check || (++ticks % 64 == 0); }
  void observe(bool resident) {
    if (mode != Check && resident != (mode == Mmap)) {
      mode = Check;
      samples = warm = ticks = 0;  // 傾向に反する観測で即座に毎回確認へ戻す。
    }
    warm += resident;
    if (++samples == 32) {
      mode = warm >= 31 ? Mmap : (warm <= 1 ? Pread : Check);
      samples = warm = ticks = 0;
    }
  }
};

inline ResidencyPolicy &policy_for(const void *owner) {
  thread_local ResidencyPolicy policy;
  policy.bind(owner);
  return policy;
}

inline void verify_policy() {
  auto require = [](bool ok) { if (!ok) throw std::runtime_error("policy transition failed"); };
  ResidencyPolicy policy;
  for (int i = 0; i < 32; ++i) policy.observe(true);
  require(policy.mode == ResidencyPolicy::Mmap);
  for (int i = 0; i < 63; ++i) require(!policy.probe());
  require(policy.probe());
  policy.observe(false);
  require(policy.mode == ResidencyPolicy::Check);
  for (int i = 0; i < 31; ++i) policy.observe(false);
  require(policy.mode == ResidencyPolicy::Pread);
  policy.observe(true);
  require(policy.mode == ResidencyPolicy::Check);
  policy = {};
  for (int i = 0; i < 32; ++i) policy.observe(i % 2);
  require(policy.mode == ResidencyPolicy::Check);
}
}  // namespace read_policy_prototype
