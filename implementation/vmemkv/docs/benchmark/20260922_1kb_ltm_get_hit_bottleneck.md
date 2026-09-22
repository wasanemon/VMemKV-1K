# 2026-09-22: 1KB LTM Get (Hit) のボトルネック調査

主因は **T2 の非常駐ファイルページを、Get の実行スレッドがページフォルト経由で同期的に読み込む待ち**。
代表設定 `Bloom-T1InlineValue` の Zipf / Uniform、1 / 16 スレッドで確認した。
実装・設定の改良は行っていない。

## 対象と条件

- 公開対象: [2026091416 フル測定](https://vmemkv.pages.dev/2026091416_charts.html)。
  公開チャートの対象8セルは、リポジトリ内
  `benchmark_results/2026091416/results_ltm_1KB.json` の数値と完全一致。
  公開側はコミット `047300bf27de`、AWS i4i.8xlarge / 32 vCPU / local NVMe、
  カーネル `7.0.0-1012-aws`。公開1スレッドの +Inline は Zipf 26,885、Uniform 9,964 ops/s。
- ローカル対象: `e75746c2d03e0d53edaf566c157aad95172b887c` と調査開始前からの保存先修正。
  既存 `build/vmemkv-gcc13/benchmark/bench_kv` を再ビルドせず使用。
  GCC 13.4 / Release `-O3 -DNDEBUG` / Google Benchmark 1.8.4。
  バイナリ SHA256: `a7f02a8c2bcc20175782a2c43febdb9313900625a2cd0c26eb197ec8c9df26ba`。
- ホスト: Xeon Gold 5418N、48物理コア / 96論理CPU、RAM 247GiB、Linux 5.15.0-186-generic。
  DB は ext4 / `/dev/mapper/vg0-lvhome`、下位デバイスは非回転 `sda`。
  公開環境と異なるため、両ホスト間の速度差を改善率とは扱わない。
- 8,259,552キー、キー16B。キーの80%が1024B値、20%が8B値（5キーごと）。
  Zipf のパラメーターは1.0、乱数seedは `42 + thread_index`。
  Getのコールバックは値全体をチェックサム計算し、全バイトを読む。
- 算定予算1GiB × 倍率8。実効制限を毎回cgroupファイルから記録:
  `memory.high=1073741824`、`memory.max=2147483648`、
  `memory.swap.max=1099511627776`。ホストの実Swap総量は4GiBのまま。
- マスターを制限外で一度準備し、各計測は制限内で新しいT2コピーから開始。
  マスターmanifestのT2実使用範囲は **7,030,530,024B（6.55GiB）**。
  ベンチマークの `CorpusBytes=8,589,934,080` は目標値サイズによる算定値で、混在後の実サイズではない。

## 実測

初期化・クローン作成を除き、Google Benchmark の最後の測定区間を使用。
各条件1回。perf採取は別実行に分離した。

| 条件 | threads | ops/s | CPU稼働率/測定スレッド | major fault/Get | デバイス読込B/Get |
| --- | ---: | ---: | ---: | ---: | ---: |
| LTM / Uniform | 1 | 13,053 | 15.5% | 0.864 | 3,541 |
| LTM / Zipf | 1 | 49,823 | 23.7% | 0.203 | 834 |
| LTM / Uniform | 16 | 147,513 | 20.7% | 0.907 | 3,714 |
| LTM / Zipf | 16 | 652,306 | 27.0% | 0.191 | 786 |
| メモリ制限緩和 / Uniform | 1 | 332,362 | 100.0% | 0 | 0 |

CPU稼働率は `cpu_time / real_time / threads`。
major fault とデバイス読込は、区間前後の `/proc/PID/stat` と `/proc/PID/io` の差分。
区間境界の観測には数ms〜数十msのずれがあり、数値は丸めた。

対照はデータ件数・分布・実行ファイルを維持し、実際の制限だけを
`MemoryHigh=16GiB / MemoryMax=32GiB` に変更したもの。
通常のIn-Memoryベンチマーク（8,000,000キー）への切り替えではない。
Uniformで **25.5倍** となり、major fault / デバイス読込が0になった。
これは律速要因を切り分ける実験であり、実装改良の効果ではない。

## 待ち時間と発生箇所

Uniform / 1スレッドの通常計測では、363,121 Get に27.819秒、測定スレッドのCPU時間は4.313秒。
**84.5%が非CPU時間**。major fault は313,796回で、主スレッドの自発的コンテキストスイッチも313,796回だった。
0.5秒間隔の主スレッド観測は55点中47点が `wait_on_page_bit_common`、残り8点が実行中。
CPU実行待ちのPSIは約1%、メモリ回収のPSI fullは4.4%だった。
この組み合わせから、中心はページ読込完了待ちと判断する。

別実行のperfは `cpu-clock/freq=99/` と `major-faults/period=97/` を採取。
最後の測定区間の **major fault 3,251サンプル中3,250件（99.97%）がT2クローンファイル、1件が匿名領域**。
採取付きでも12,585 ops/s、CPU稼働率15.5%と、通常計測に近い傾向だった。

フォルト時の命令を既存バイナリの逆アセンブルと照合した:

| 命令位置（ELF相対） | サンプル | 対応する処理 |
| --- | ---: | --- |
| `0x617ae: mov (%rax),%esi` | 2,552（78.5%） | T2レコードヘッダーの読み取り |
| `0x61b42: movzbl -0x1(%rdx),%eax` | 675（20.8%） | 値全体を読む `touch_bytes()` |
| libc の `memcmp` | 24（0.7%） | キー照合等。匿名領域1サンプルを含む |

値読込の675サンプルは **すべてフォルト先アドレスが4KBページ先頭**。
1KBレコードでもページ境界をまたぐと、ヘッダー側の読込に加えて値側でもフォルトすることを確認した。
Uniformの平均値サイズ820.8Bに対し、実デバイス読込は3,541B/Getで約4.3倍。
この倍率にはページ単位の読み込みと少量のSwap等が含まれ、単純な1KBコピーのCPUコストでは説明できない。

## 実装との対応

1. `src/vmemkv_impl.hpp:317` の `get_impl()` がT1を検索。
   8Bのインライン値はここでコールバックを呼び、T2アクセスを省く。
2. 1KB値は `src/vmemkv/read_path.hpp:248` のsmall判定を通り、
   `for_get_small()` → `read_base_record_via()` で `mem->base` を直接読む。
   大きい値向けの `mincore()` / `pread()` 経路は使わない。
3. `src/vmemkv/read_path.hpp:134` の `header->key_len / value_len` が最初の主要フォルト位置。
   キー照合後、`benchmark/bench_kv.cpp:648` の `touch_bytes()` が値を全走査し、
   必要なら次ページで追加フォルトする。
4. `src/t2_flat_file/t2_flat_file.cpp:146` のT2は通常ファイルの **MAP_SHARED + MADV_RANDOM**。
   非常駐ページの読み込みはその場で待つ。8月の旧調査のMAP_PRIVATE・大量Swapという説明を
   現在の実装へそのまま当てはめてはいけない。

LTMの測定終了時は、匿名メモリが約324〜328MiB、ファイルキャッシュが約656〜667MiB。
T1等が約1/3の予算を使い、6.55GiBのT2に対してファイルキャッシュは約1/10しか残らない。
ページ回収・再読込が発生し、局所性の弱いUniformほどmajor fault/Getが多くなる。
これは実測値と実装からの因果関係の解釈であり、キャッシュの最適配分を検証した結果ではない。

## 切り分けと限界

- Swap不足が主因という証拠はない。LTM通常4条件のcgroup Swapは終了時3.3〜49.9MiB。
  Uniform / 1スレッドのホスト全体のswap-in増分は366ページで、major fault 313,796回より桁違いに少ない。
  ホスト全体カウンターは他プロセスも含むため、補助指標として扱う。
- 全計測で `Checkpoints=0`、`T1_Splits=0`、`Reorganize_Wait_Duration_us=0`。
  本文のI/O量は初期化時の約7GBコピーを含めていない。
- T1検索のCPU処理は残るが、主因をT1検索やロック競合とする証拠は得られなかった。
  カーネルシンボルの参照権限がないため、カーネル内部のロック別時間までは断定しない。
  デバイスの帯域上限への到達も別途検証していない。
- 最大96スレッド、他バリアント、書込み混在は未測定。公開側の32スレッド値を今回再現したとは扱わない。
  Google Benchmarkは各試行で同じ乱数列を使うため、較正時のキャッシュ状態が結果に影響する。
  信頼区間や改善案の優劣を求める実験ではない。

## 保存先と再現

生データ・観測スクリプトは、このホストの
`build/ltm/results/get-hit-20260922/`（Git管理外）に保存。

- `environment.txt` / `starting-worktree.patch`: 環境、対象バイナリ、開始時の差分。
- `summary.json`: 初期化を除いた最終区間の集計。
- 各条件ディレクトリの `command.json` / `benchmark.json` / `phases.jsonl` / `samples.jsonl`:
  実行コマンド、Google Benchmark結果、区間前後・0.5秒間隔の生観測。
- `profile-uniform-t1/`: `perf.data`、`perf-script.txt`、`maps.txt`、
  `fault-classification.json`、ヘッダー・値の逆アセンブル。
- `public-2026091416.html` / `public-get-hit.json` / `public-context.json`: 公開結果との照合記録。

準備済みのマスターとビルドを使って再実行するコマンド（ディレクトリ名は未使用名にする）:

```bash
cd /home/wasanemon/project/VMemKV-1K
source build/ltm/env.sh
python3 build/ltm/results/get-hit-20260922/observe.py rerun-uniform-t1 \
  --dist Uniform --threads 1 --seconds 12s
python3 build/ltm/results/get-hit-20260922/observe.py rerun-zipf-t1 \
  --dist Zipf --threads 1 --seconds 12s
python3 build/ltm/results/get-hit-20260922/observe.py rerun-uniform-t16 \
  --dist Uniform --threads 16 --seconds 12s
python3 build/ltm/results/get-hit-20260922/observe.py rerun-zipf-t16 \
  --dist Zipf --threads 16 --seconds 12s
python3 build/ltm/results/get-hit-20260922/observe.py rerun-profile \
  --dist Uniform --threads 1 --seconds 10s --profile
python3 build/ltm/results/get-hit-20260922/observe.py rerun-resident \
  --dist Uniform --threads 1 --seconds 8s \
  --memory-high 17179869184 --memory-max 34359738368
python3 build/ltm/results/get-hit-20260922/summarize.py
```

`observe.py` は観測プロセスをcgroupの外に置き、既存の `--v=3` の
`Running ... for N` / `Ran in ...` を区間境界に使う。
perfは最初の初期化込み試行では無効、以後の試行だけFIFOで有効化し、最後の区間を時刻で抽出した。
通常計測ではperfを起動しない。`systemd-run --user --scope` のため、このホストではサンドボックス外で実行した。
実装・依存環境・カーネル設定・Swap設定は変更していない。
