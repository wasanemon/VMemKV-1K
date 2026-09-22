// 固定ファイルのpage cache追加・削除で、共有状態の世代と登録可否を更新する。
#include <linux/bpf.h>
#define SEC(name) __attribute__((section(name), used))
#define UINT(name, value) int (*name)[value]
#define TYPE(name, value) value *name
typedef unsigned long long u64;
typedef unsigned int u32;
struct target { u64 inode; u32 dev; u32 pages; };
struct {
  UINT(type, BPF_MAP_TYPE_ARRAY); UINT(max_entries, 1);
  TYPE(key, u32); TYPE(value, struct target);
} target SEC(".maps");
struct {
  UINT(type, BPF_MAP_TYPE_ARRAY); UINT(max_entries, 1);
  UINT(map_flags, BPF_F_MMAPABLE);
  TYPE(key, u32); TYPE(value, u64);
} states SEC(".maps");
// 実機formatとの照合をローダーで必須にする。
struct filemap_event { u64 common; u64 pfn; u64 ino; u64 index; u32 dev; };
static void *(*lookup)(void *, const void *) = (void *)BPF_FUNC_map_lookup_elem;
static __attribute__((always_inline)) int change(struct filemap_event *ctx, u64 status) {
  u32 zero = 0;
  struct target *cfg = lookup(&target, &zero);
  if (!cfg || ctx->ino != cfg->inode || ctx->dev != cfg->dev || ctx->index >= cfg->pages) return 0;
  u32 index = ctx->index;
  u64 *slot = lookup(&states, &index);
  if (slot) {
    // 同じページの追加・削除イベントはpage lockで順序付く。
    // ユーザー側の未知→確認済みCASに対しては、常に新しい世代で上書きする。
    u64 old = *(volatile u64 *)slot;
    __sync_lock_test_and_set(slot, ((old & ~3ULL) + 4) | status);
  }
  return 0;
}
SEC("tracepoint/filemap/mm_filemap_add_to_page_cache")
int page_added(struct filemap_event *ctx) { return change(ctx, 0); }
SEC("tracepoint/filemap/mm_filemap_delete_from_page_cache")
int page_deleted(struct filemap_event *ctx) { return change(ctx, 2); }
char LICENSE[] SEC("license") = "GPL";
