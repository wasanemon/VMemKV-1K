#!/usr/bin/env bash
# 利用者がsudoで実行する、今回の単件Get観測専用の10秒トレース。
# sysctl、tracefsの権限、既存のftrace設定は変更しない。
set -euo pipefail
root=/home/wasanemon/project/VMemKV-1K
out="$root/build/ltm/results/get-hit-io-20260922/trace"
binary="$root/build/ltm/results/get-hit-io-20260922/inspect_get_hit_io"
if [[ $EUID != 0 ]]; then
  echo 'このスクリプトだけsudoで実行してください。' >&2
  exit 1
fi
pid=$(cat "$out/pid")
[[ "$pid" =~ ^[0-9]+$ && -f "$out/ready" && ! -e "$out/start" ]]
[[ $(readlink -f "/proc/$pid/exe") == "$binary" ]]
owner=$(stat -c '%u:%g' "$out")
events=(
  -e block:block_bio_queue --filter '(dev == 8388608 || dev == 8388610 || dev == 265289730) && rwbs ~ "R*"'
  -e block:block_bio_remap --filter '(dev == 8388608 || dev == 8388610 || dev == 265289730) && rwbs ~ "R*"'
  -e block:block_bio_backmerge --filter 'dev == 8388608 && rwbs ~ "R*"'
  -e block:block_bio_frontmerge --filter 'dev == 8388608 && rwbs ~ "R*"'
  -e block:block_rq_issue --filter 'dev == 8388608 && rwbs ~ "R*"'
  -e block:block_rq_complete --filter 'dev == 8388608 && rwbs ~ "R*"'
  -e syscalls:sys_enter_pread64 --filter "common_pid == $pid"
  -e syscalls:sys_exit_pread64 --filter "common_pid == $pid"
  -e sched:sched_switch --filter "prev_pid == $pid || next_pid == $pid"
)
# 記録するのはsdaのI/Oメタデータと対象Getスレッドのpread/スケジューリング。
# ファイル内容や他プロセスのシステムコールは採取しない。
cleanup() {
  for name in perf.data perf.data.old perf.log start; do
    [[ ! -e "$out/$name" ]] || chown "$owner" "$out/$name"
  done
}
trap cleanup EXIT
/usr/bin/perf record -a -k mono -m 256 -o "$out/perf.data" "${events[@]}" -- \
  /usr/bin/python3 -c 'from pathlib import Path; import time; Path("/home/wasanemon/project/VMemKV-1K/build/ltm/results/get-hit-io-20260922/trace/start").touch(); time.sleep(10)' \
  2>"$out/perf.log"
echo 'トレース採取完了。Codexで解析できます。'
