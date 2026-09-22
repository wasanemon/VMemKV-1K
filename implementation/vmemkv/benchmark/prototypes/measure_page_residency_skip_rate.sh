#!/usr/bin/env bash
# 省略率だけを常駐2分布で各1回確認。カーネル設定変更なし、終了時BPF解除。
set -euo pipefail
root=/home/wasanemon/project/VMemKV-1K
out="$root/build/ltm/results/page-residency-skip-rate-20260923"
[[ $EUID == 0 && -n ${SUDO_UID:-} ]] || { echo 'sudoで実行してください' >&2; exit 1; }
[[ ! -e "$out/runs" && ! -e "$out/run.log" ]] || { echo '出力先が既に存在します。上書きせず停止します。' >&2; exit 1; }
trap '[[ ! -f "$out/run.log" ]] || chown "$SUDO_UID:$SUDO_GID" "$out/run.log"' EXIT
python3 "$root/implementation/vmemkv/benchmark/prototypes/run_page_residency_skip_rate.py" run 2>&1 | tee "$out/run.log"
