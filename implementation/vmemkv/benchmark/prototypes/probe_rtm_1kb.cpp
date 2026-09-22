// RTM利用可否だけを調べる最小プローブ。CPU/OSの設定は変更しない。
#include <cpuid.h>
#include <immintrin.h>
#include <cstdio>

__attribute__((target("rtm"), noinline))
static unsigned probe_two_pages(const volatile unsigned char *first,
                                const volatile unsigned char *second) {
  const unsigned status = _xbegin();
  if (status == _XBEGIN_STARTED) {
    (void)*first;
    (void)*second;
    _xend();
  }
  return status;
}

int main() {
  unsigned eax = 0, ebx = 0, ecx = 0, edx = 0;
  const bool leaf = __get_cpuid_count(7, 0, &eax, &ebx, &ecx, &edx);
  const bool rtm = leaf && (ebx & (1u << 11));
  const bool always_abort = leaf && (edx & (1u << 11));
  std::printf("{\"cpuid_leaf_7\":%s,\"ebx_hex\":\"0x%08x\","
              "\"ecx_hex\":\"0x%08x\",\"edx_hex\":\"0x%08x\","
              "\"rtm\":%s,\"rtm_always_abort\":%s,",
              leaf ? "true" : "false", ebx, ecx, edx,
              rtm ? "true" : "false", always_abort ? "true" : "false");
  if (!rtm || always_abort) {
    std::puts("\"attempts\":0,\"decision\":\"use_proposal1\"}");
    return 0;
  }
  alignas(4096) static volatile unsigned char pages[8192];
  pages[0] = 1;
  pages[4096] = 2;
  unsigned committed = 0, last_status = 0;
  // 利用可否の短い確認。Getに再試行ループを入れるものではない。
  for (unsigned i = 0; i < 100; ++i) {
    last_status = probe_two_pages(pages, pages + 4096);
    committed += last_status == _XBEGIN_STARTED;
  }
  std::printf("\"attempts\":100,\"committed\":%u,\"last_status\":%u,"
              "\"decision\":\"%s\"}\n", committed, last_status,
              committed ? "can_try_get_prototype" : "use_proposal1");
}
