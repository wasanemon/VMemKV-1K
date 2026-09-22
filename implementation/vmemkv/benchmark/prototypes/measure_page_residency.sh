#!/usr/bin/env bash
# 一般ユーザーのGetとrootのBPFローダーを同じ一時cgroupで比較する。
set -euo pipefail
root=/home/wasanemon/project/VMemKV-1K
out="$root/build/ltm/results/page-residency-hints-20260923"
[[ $EUID == 0 && -n ${SUDO_UID:-} ]] || { echo 'sudoで実行してください' >&2; exit 1; }
trap '[[ ! -f "$out/measure.log" ]] || chown "$SUDO_UID:$SUDO_GID" "$out/measure.log"' EXIT
python3 "$root/implementation/vmemkv/benchmark/prototypes/run_page_residency.py" run 2>&1 | tee "$out/measure.log"
