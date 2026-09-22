#!/usr/bin/env bash
# 案1/初期スナップショットの1KB Get Hitのみ。32実行、各回BPF解除。
set -euo pipefail
root=/home/wasanemon/project/VMemKV-1K
out="$root/build/ltm/results/page-residency-snapshot-20260923/matrix"
[[ $EUID == 0 && -n ${SUDO_UID:-} ]] || { echo 'sudoで実行してください' >&2; exit 1; }
[[ ! -e "$out/measurements" && ! -e "$out/run.log" ]] || { echo '出力先が既に存在します。上書きせず停止します。' >&2; exit 1; }
trap '[[ ! -f "$out/run.log" ]] || chown "$SUDO_UID:$SUDO_GID" "$out/run.log"' EXIT
python3 "$root/implementation/vmemkv/benchmark/prototypes/run_page_residency_snapshot_matrix.py" run 2>&1 | tee "$out/run.log"
