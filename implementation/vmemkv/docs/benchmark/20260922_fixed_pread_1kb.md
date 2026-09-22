# 小レコードの固定buffered pread：最小比較（2026-09-22）

## 変更と比較基準

`codex/get-read-policy`から既存の未コミット作業を保持して、`codex/fixed-pread-1kb`を作成した。4ブランチ（main、値分離、A/B、今回）の参照先はすべて`e75746c2d03e0d53edaf566c157aad95172b887c`のまま。コミット・pushは行っていない。

比較基準は**案1適用済みの製品作業ツリーのコピー**。mainのコミット内には案1が未反映であり、そのままのmainとの比較ではない。今回の変更は実験用コピーだけに適用し、製品src/includeには案1の条件式だけを残す。Aの独立fd・先読み設定、Bの適応制御、値とメタデータの分離は含めない。

変更は`src/vmemkv/read_path.hpp`の2箇所だけ。

- `read_large_get_cold()`に既定値trueの`check_residency`引数を追加し、falseなら既存の常駐確認を通らず、既存のbuffered `pread()`・バッファ・レコード解釈を使う。
- 不変baseのGetで`is_small`なら、ページ跨ぎの有無にかかわらず同関数へfalseを渡す。

試作の対象判定は**`size_hint <= 4096`**で、値長1024Bちょうどの判定ではない。今回のデータでは外部値1024Bが対象になる。8B値は既存のT1インライン経路を使う。mutable tail・Scan・大レコードの分岐、既存のキー／読込み長検証は維持した。読込み失敗時などは既存のmmap側フォールバックが残るため、全状況でpreadだけを使う保証ではない。

Getの対象経路では事前のmincoreを省く。初期化時のキャッシュ準備・確認やmmapの作成自体は既存どおりである。callbackはpread先のバッファを参照し、そのための追加コピーは加えていない。

## 条件

- Intel Xeon Gold 5418N、Linux 5.15.0-186-generic、GCC 13.4.0 / Release相当（`-O3 -DNDEBUG`）。既存ビルドのライブラリを共用。
- `Bloom-T1InlineValue`、8,259,552キー、キー16B。キー数の80%が1024B値、20%が8B値。既存の密詰めcheckpointを両版で共用。
- LTM：`MemoryHigh=1GiB / MemoryMax=2GiB`。T2論理使用量7,030,530,024B。各実行で制限の実適用を確認。
- 同期単件Get Hit、Uniform／Zipf（α=1）。返された値の全バイトを読む。指標はthroughputのみ。
- 1スレッドはCPU 0、16スレッドはCPU 0,2,…,30へ制限。各条件・各版2回、2回目は版の実行順を逆転。
- 既存Google Benchmark本体を再利用。初期化後にT2のキャッシュを落とし、非常駐を確認してから較正・計測。`--benchmark_min_time=2s`、明示的warmupは0。初期化は計測対象外。

まず1スレッドを8実行。両分布で2回とも改善したため、予定した16スレッドだけを追加した。従来の12秒warmup＋10秒計測の専用harnessや、過去の異なるCPU配置の結果とは合算しない。

## 結果

**案1に対し、測定したLTMの4条件で+1.37〜+9.06%。追加改善の方向として有望。** 全条件で2回とも固定pread版が基準を上回った。1スレッドUniformの上積みは小さいが、16スレッドでは両分布で改善が大きくなった。計16実行で比較を終了した。

各版2回の算術平均、単位ops/s。増減は丸め前の平均から計算。

| 分布 | スレッド | 案1 | 固定pread | 案1比 |
|---|---:|---:|---:|---:|
| Uniform | 1 | 18,597 | 18,853 | +1.37% |
| Zipf | 1 | 42,967 | 44,560 | +3.71% |
| Uniform | 16 | 246,938 | 261,076 | +5.73% |
| Zipf | 16 | 807,741 | 880,910 | +9.06% |

案1の過去の改善率に加算するものではない。今回の比較は案1を基準とした追加差分である。

## 確認と限界

両版のビルドと、1,024キーを2回ずつ全バイト照合する確認、Missの確認に成功した。初回はLTM Get Hitだけを測定した。その後の常駐・書込み混在の追加比較は以下の追記を参照。全テスト、他の値サイズ、原因別プロファイルには広げていない。

狙いはLTMにおけるmmapのfault・マッピング・回収処理の負担軽減だが、今回測るのはthroughputのみであり、原因の内訳やI/O量の削減は確認しない。buffered preadもページキャッシュを使い、常駐ヒットにもシステムコールとコピーが入る。保存形式・容量の変更はなく、読込みバッファは既存のものを再利用する。各版2回の方向性確認であり、統計的な有意差や全ワークロードでの改善を保証しない。

## 再現・保存先

既存の準備済みデータとビルドが必要。リポジトリルートから実行する。

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_fixed_pread_1kb.py build
python3 implementation/vmemkv/benchmark/prototypes/run_fixed_pread_1kb.py verify
python3 implementation/vmemkv/benchmark/prototypes/run_fixed_pread_1kb.py matrix
python3 implementation/vmemkv/benchmark/prototypes/run_fixed_pread_1kb.py matrix --threads 16
python3 implementation/vmemkv/benchmark/prototypes/run_fixed_pread_1kb.py summarize
```

ランナーは[run_fixed_pread_1kb.py](../../benchmark/prototypes/run_fixed_pread_1kb.py)。既存の`regression_throughput.cpp`・`run_regression_throughput.py`と、A/B用検証片の値照合部分だけを再利用する。

`build/ltm/results/fixed-pread-1kb-20260922/`（Git管理外）に次を保存した。

- `source/`：両版のソース、`fixed-pread.patch`：案1からの実験差分、`proposal1-working-tree.patch`：mainから案1への製品差分。
- `provenance.json`・`build-*.json`：対象コミット・ブランチ・コンパイラ・ハッシュ・ビルドコマンド。
- `verify-*.log`・`verify-*-command.json`：値照合結果とコマンド。
- `plan-t*.json`・各実行ディレクトリ：完全なコマンド／環境、適用メモリ制限・CPU配置、生のベンチマークJSON、終了コード、throughput。
- `summary.json`：各版2回の平均と生のthroughput。

## 追加比較（2026-09-23）

ユーザー依頼で、常駐1KB Get HitのUniform／Zipf × 1／16スレッドと、LTMのZipf Get/Update 50%ずつ・1スレッドに限定して比較した。各版2回、計20実行。初回と同じバイナリであることをSHA256で確認し、再ビルド・アルゴリズム変更は行わない。比較基準は引き続き案1。

CPU配置・データ・Google Benchmark min_time=2秒は初回と同じ。常駐はHigh=16GiB/Max=32GiBでT2全ページと既存マッピングを事前に温め、LTMはHigh=1GiB/Max=2GiB。Get/Update混在ではGetとUpdateを交互に呼び、両操作を合わせたops/sを測る。各実行の前に専用checkpointコピーへ戻し、元DBは保持する。初期化・コピー・キャッシュ準備は計測区間に含めない。

**常駐Get Hitは案1比で11.25〜27.05%低下した。LTM Getでの改善は維持して評価できるが、一律適用には大きな常駐性能のトレードオフがある。** 常駐の全4条件で2回とも低下。Get/Update混在の平均差は+0.59%で、1回目は改善、2回目は低下しており、明確な改善とは扱わない。

各版2回の平均、単位ops/s。

| 条件 | スレッド | 案1 | 固定pread | 案1比 |
|---|---:|---:|---:|---:|
| 常駐 Get Hit Uniform | 1 | 330,150 | 282,034 | −14.57% |
| 常駐 Get Hit Zipf | 1 | 416,090 | 369,272 | −11.25% |
| 常駐 Get Hit Uniform | 16 | 5,338,202 | 4,078,169 | −23.60% |
| 常駐 Get Hit Zipf | 16 | 6,922,034 | 5,049,816 | −27.05% |
| LTM Get/Update 50%ずつ Zipf | 1 | 14,611 | 14,696 | +0.59% |

常駐Getにも毎回システムコールとコピーを入れる変更と整合する結果だが、原因別の時間は測っていない。計20実行で終了し、閾値調整、Scan・書込み単独・他サイズ等への追加拡大は行わない。これら未測定の条件で劣化なしとは扱わない。混在用の一時DBは終了後に削除し、元DB・全ログは保持した。

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_fixed_pread_1kb_extra.py
```

追加ランナーは[run_fixed_pread_1kb_extra.py](../../benchmark/prototypes/run_fixed_pread_1kb_extra.py)。`build/ltm/results/fixed-pread-1kb-20260922/extra-20260923/`に計画・元バイナリのハッシュ・コマンド／環境・適用制限・生JSON・集計を保存する。実行開始時のブランチは`codex/fixed-pread-1kb`で、今回ブランチ切替は行っていない。通常ソースは案1のみ、試作と記録は未コミット・未push。
