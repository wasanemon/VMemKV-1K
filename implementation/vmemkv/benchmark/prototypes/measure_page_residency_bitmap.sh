#!/usr/bin/env bash
# BPF確認→値照合→3版×4条件×2回。カーネル設定変更なし、各回終了時に解除。
set -euo pipefail
root=/home/wasanemon/project/VMemKV-1K
out="$root/build/ltm/results/page-residency-bitmap-20260923"
[[ $EUID == 0 && -n ${SUDO_UID:-} ]] || { echo 'sudoで実行してください' >&2; exit 1; }
[[ ! -e "$out/measurements" && ! -e "$out/measure.log" ]] || { echo '出力先が既に存在します。上書きせず停止します。' >&2; exit 1; }
trap '[[ ! -f "$out/measure.log" ]] || chown "$SUDO_UID:$SUDO_GID" "$out/measure.log"' EXIT
python3 "$root/implementation/vmemkv/benchmark/prototypes/run_page_residency_bitmap.py" run 2>&1 | tee "$out/measure.log"
