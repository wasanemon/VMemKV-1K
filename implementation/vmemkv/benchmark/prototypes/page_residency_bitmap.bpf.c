// 64ページを24Bで管理。固定checkpoint・4KiBページの試作専用。
#include <linux/bpf.h>
#define SEC(name) __attribute__((section(name), used))
#define UINT(name, value) int (*name)[value]
#define TYPE(name, value) value *name
typedef unsigned long long u64;
typedef unsigned int u32;
struct target { u64 inode; u32 dev; u32 pages; };
struct block { u64 resident; u64 blocked; u64 generation; };
struct {
  UINT(type, BPF_MAP_TYPE_ARRAY); UINT(max_entries, 1);
  TYPE(key, u32); TYPE(value, struct target);
} target SEC(".maps");
struct {
  UINT(type, BPF_MAP_TYPE_ARRAY); UINT(max_entries, 1);
  UINT(map_flags, BPF_F_MMAPABLE);
  TYPE(key, u32); TYPE(value, struct block);
} states SEC(".maps");
struct filemap_event { u64 common; u64 pfn; u64 ino; u64 index; u32 dev; };
static void *(*lookup)(void *, const void *) = (void *)BPF_FUNC_map_lookup_elem;
static __attribute__((always_inline)) int change(struct filemap_event *ctx, int deleting) {
  u32 zero = 0;
  struct target *cfg = lookup(&target, &zero);
  if (!cfg || ctx->ino != cfg->inode || ctx->dev != cfg->dev || ctx->index >= cfg->pages) return 0;
  u32 index = ctx->index >> 6;
  struct block *slot = lookup(&states, &index);
  if (!slot) return 0;
  u64 bit = 1ULL << (ctx->index & 63);
  // 同じブロックの別ページのイベントとも競合するため、全てatomic RMW。
  // 削除通知から実削除までの再登録をblockedで拒否する。
  if (deleting) __sync_fetch_and_or(&slot->blocked, bit);
  else __sync_fetch_and_and(&slot->resident, ~bit);
  __sync_fetch_and_add(&slot->generation, 1);
  if (deleting) __sync_fetch_and_and(&slot->resident, ~bit);
  else __sync_fetch_and_and(&slot->blocked, ~bit);
  return 0;
}
SEC("tracepoint/filemap/mm_filemap_add_to_page_cache")
int page_added(struct filemap_event *ctx) { return change(ctx, 0); }
SEC("tracepoint/filemap/mm_filemap_delete_from_page_cache")
int page_deleted(struct filemap_event *ctx) { return change(ctx, 1); }
char LICENSE[] SEC("license") = "GPL";
