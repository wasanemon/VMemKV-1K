// 原因切り分け専用。全ページを事前に温めるresident測定以外には使用しない。
#define _GNU_SOURCE
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>

__attribute__((constructor)) static void require_resident(void) {
  const char *scenario = getenv("REGRESSION_SCENARIO");
  if (!scenario || strcmp(scenario, "resident") != 0) abort();
  fputs("diagnostic: mincore replaced with resident result\n", stderr);
}

int mincore(void *address, size_t length, unsigned char *vec) {
  (void)address;
  memset(vec, 1, (length + 4095) / 4096);
  return 0;
}
