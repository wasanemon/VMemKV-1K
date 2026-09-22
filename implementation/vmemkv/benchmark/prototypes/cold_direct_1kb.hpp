#pragma once
// 単一checkpointを読む実験用。通常src/includeからは使用しない。
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <array>
#include <cstddef>
#include <optional>
#include <stdexcept>
#include <string>
#include "t2_flat_file/t2_flat_file.hpp"

namespace cold_direct_prototype {
struct DirectFile {
  int fd = -1;
  ~DirectFile() { if (fd >= 0) ::close(fd); }
};
inline DirectFile file;
inline void configure(int source) {
  const int opened = ::open(("/proc/self/fd/" + std::to_string(source)).c_str(),
                            O_RDONLY | O_DIRECT | O_CLOEXEC);
  struct stat a{}, b{};
  if (opened < 0 || ::fstat(source, &a) || ::fstat(opened, &b) ||
      a.st_dev != b.st_dev || a.st_ino != b.st_ino) {
    if (opened >= 0) ::close(opened);
    throw std::runtime_error("Direct I/O fd setup failed");
  }
  if (file.fd >= 0) ::close(file.fd);
  file.fd = opened;  // ベンチ開始前、単一Storeだけを登録する。
}
inline auto buffer() -> std::byte * {
  alignas(4096) thread_local std::array<std::byte, 8192> data;
  return data.data();
}
inline auto read(uint64_t offset, uint64_t length, uint64_t boundary)
    -> std::optional<T2RecordView> {
  constexpr uint64_t alignment = 512;  // このホストのprobeで確認済み。
  const uint64_t start = offset & ~(alignment - 1);
  const uint64_t end = (offset + length + alignment - 1) & ~(alignment - 1);
  if (file.fd < 0 || end > boundary || end - start > 8192) return std::nullopt;
  std::byte *data = buffer();
  if (::pread(file.fd, data, end - start, start) != static_cast<ssize_t>(end - start))
    return std::nullopt;
  const auto *header = reinterpret_cast<const ValueRecordHeader *>(data + offset - start);
  const uint64_t needed = sizeof(*header) + uint64_t{header->key_len} + header->value_len;
  if (needed > length) return std::nullopt;
  return make_record_view(header);
}
}  // namespace cold_direct_prototype
