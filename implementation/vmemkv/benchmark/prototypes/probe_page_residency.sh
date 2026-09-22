#!/usr/bin/env bash
# sudoが必要な最初の確認だけ。対象はこの実験の8KiB一時ファイル。
set -euo pipefail
root=/home/wasanemon/project/VMemKV-1K
out="$root/build/ltm/results/page-residency-hints-20260923"
[[ $EUID == 0 ]] || { echo 'sudoで実行してください' >&2; exit 1; }
owner=$(stat -c '%u:%g' "$out")
cleanup() {
  for name in event-formats.json probe-result.json probe.log; do
    [[ ! -f "$out/$name" ]] || chown "$owner" "$out/$name"
  done
}
trap cleanup EXIT
timeout 30s python3 "$root/implementation/vmemkv/benchmark/prototypes/page_residency_loader.py" 2>&1 | tee "$out/probe.log"
