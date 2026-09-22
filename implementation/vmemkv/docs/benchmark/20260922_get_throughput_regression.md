# 2026-09-22: 案1のthroughput退行確認

**2026-09-23追記：** 常駐Get低下の主因は、案1で追加されたmincore呼出しのコストと切り分けた。全4条件で、同一案1バイナリのmincoreだけを常駐結果に置換すると元実装とほぼ同じ性能へ回復した。[原因調査・条件・結果](20260923_proposal1_resident_cause.md)を参照。以下の「原因未確認」は初回比較時点の記録。

## 結果

**案1はLTM 1KB Get Hitを改善する一方、常駐1KB Get Hitではthroughputが低下した。全条件で劣化なしとは言えない。**

CPU配置を揃えた追加比較でも、常駐1KB Get Hitは1スレッドで1.5〜3.6%、16スレッドで4.5〜5.0%低下した。境界をまたぐ常駐Getにも追加の判定・常駐確認が入ることは説明候補だが、内訳は測定していない。

追加比較の完了分（各版2回の平均、ops/s）：

| 条件 | 現行 | 案1 | 増減 |
|---|---:|---:|---:|
| LTM / 1KB / Scan Uniform / 16スレッド | 18,601 | 18,611 | +0.05% |
| 常駐 / 1KB / Get-Hit Uniform / 1スレッド | 331,851 | 326,920 | -1.49% |
| 常駐 / 1KB / Get-Hit Uniform / 16スレッド | 5,583,091 | 5,329,474 | -4.54% |
| 常駐 / 1KB / Get-Hit Zipf / 1スレッド | 439,786 | 424,140 | -3.56% |
| 常駐 / 1KB / Get-Hit Zipf / 16スレッド | 7,266,756 | 6,901,208 | -5.03% |

初回比較では、LTM 1KB Get Hitの4条件は+8.9〜+22.8%。書込み単独のInsert/Update/DeleteとYCSB-Eは、測定した1KB条件で−2.6〜+2.4%だった。

初回比較で3%以上低下した条件は次のとおり。ばらつきや追加比較の有無を区別し、すべてを変更の直接的な影響とは断定しない。

| 条件 | 初回の増減 | 追加比較 |
|---|---:|---|
| LTM / 1KB / GetUpdate50 Zipf / 16スレッド | -8.04% | 未完了 |
| LTM / 1KB / Scan Uniform / 16スレッド | -4.17% | +0.05% |
| LTM / 64KB / Scan Uniform / 16スレッド | -5.71% | 未完了 |
| 常駐 / 8B / Get-Miss Zipf / 16スレッド | -3.11% | 未完了 |
| 常駐 / 8B / Scan Uniform / 1スレッド | -3.37% | 未完了 |
| 常駐 / 8B / Scan Zipf / 1スレッド | -7.39% | 未完了 |
| 常駐 / 1KB / Get-Hit Uniform / 1スレッド | -5.20% | -1.49% |
| 常駐 / 1KB / Get-Hit Uniform / 16スレッド | -15.43% | -4.54% |
| 常駐 / 1KB / Get-Hit Zipf / 1スレッド | -3.71% | -3.56% |
| 常駐 / 1KB / Get-Hit Zipf / 16スレッド | -6.99% | -5.03% |

8B Scanの1スレッドは初回2回とも低下したが、変更したGet分岐を直接通る操作ではなく、原因は未確認。LTM GetUpdate50 / 16スレッドは初回の2組で増減の向きが逆だった。LTM 1KB Scan / Uniform / 16スレッドの低下は追加比較では再現しなかった。

ユーザーの「過剰な確認はしなくていい」という指示で追加測定を途中終了した。追加比較は5条件・20実行が完了。次の条件の現行1実行だけが完了していたが、比較には含めない。停止した実行も除外した。

提案の方向性は、LTMの改善を維持しつつ、常駐Getへ毎回加わる処理を減らすこと。今回はthroughput比較までとし、追加実装・原因の再プロファイルは行っていない。

[全58条件の初回結果と完了した追加5条件（CSV）](20260922_get_throughput_regression.csv)。初回と追加の平均は合算していない。

## 比較対象

案1の「1ページに収まらないレコードを既存のmincore/pread経路へ回す」変更を、
実験ディレクトリへコピーした `read_path.hpp` のGet分岐1箇所に適用した。
製品のsrc/includeは変更していない。現行・案1は同じGCC 13.4、`-O3 -DNDEBUG`、
同じ既存 `libvmemkv.a` でビルドした。対象コミットは
`e75746c2d03e0d53edaf566c157aad95172b887c` と既存の保存先等の修正。

```diff
 case BaseReader::kGet:
-  if (is_small) {
+  if (is_small && size_hint <= kPageSize - offset % kPageSize) {
```

既存 `bench_kv.cpp` のGet Hit/Miss、Insert、Update、Delete、Scan、YCSB-Eの本体を再利用した。
型は両版とも `Bloom-T1InlineValue` のみに絞った。
YCSB-EはGetを含まないため、単件GetとUpdateを交互に呼ぶ50%/50%のZipfケースを別途追加した。
MultiGetは使っていない。

## 測定範囲

| 条件 | 値サイズ | キー数 | 操作 | スレッド |
|---|---|---:|---|---|
| 常駐 | 1KB（20%は8B） | 8,259,552 | Get Hit U/Z、Get Miss、Insert、Update、Delete、Scan U/Z、GetUpdate50 | 1 / 16 |
| LTM | 1KB（20%は8B） | 8,259,552 | 同上 | 1 / 16 |
| 常駐 / LTM | 1KB（20%は8B） | 8,259,552 | YCSB-E（95%Scan / 5%Insert） | 96 |
| 常駐 | 8B | 20,000,000 | Get Hit U/Z、Get Miss、Scan U/Z | 1 / 16 |
| LTM | 64KB（20%は8B） | 131,040 | Get Hit U/Z、Get Miss、Scan U/Z | 1 / 16 |

U=Uniform、Z=Zipf(alpha=1)。Get Miss/Update/GetUpdate50はZipf。
20%の8B比率はキー数ベースで、Zipf時の要求回数の20%という意味ではない。
Scanの1操作は最大100件の範囲検索。GetUpdate50とYCSB-Eは混在する操作の合計を数える。

全58条件を各版2回、条件ごとに順序を逆転して測った（232実行）。
初回の平均が3%以上低下した10条件を対象に追加確認を開始し、CPU配置と測定時間を揃え直した。
5条件を各版2回測った時点で、追加測定を終了した。
これは方向性確認の短時間比較であり、3%は追加測定対象を選ぶ目安。統計的有意性の境界ではない。

## 比較条件

- 同じ既存ホスト、Xeon Gold 5418N / Linux 5.15.0-186-generic / ext4。
  依存導入、ボトルネックの再プロファイルは行わず、評価指標はthroughputのみとした。
- 1KBは前の試作で準備した密詰めcheckpointを再利用。
  常駐側も同じ8,259,552キーなので、公開In-Memoryの固定8,000,000キーとは少し異なる。
  8Bと64KBは既存ベンチマークと同じ生成関数で追加準備した。
- 常駐はMemoryHigh=16GiB / MemoryMax=32GiB、LTMはHigh=1GiB / Max=2GiB。
  各プロセス内から適用値を検証し、`memory-limits.json` に保存した。
- 常駐はT2と各mmapのページテーブルを初期化時に温める。
  LTMはT2のキャッシュを落とし、非常駐を確認してからベンチマークを開始。
  初期化とキャッシュ準備は測定区間から除外した。
- 書込みを含む条件では、毎回同じcheckpointから独立した作業コピーを作る。
  常駐Insertだけは既存ベンチマーク同様に空のDBから開始する。
- throughputはGoogle Benchmarkの `items_per_second`。初回min_time=2秒、
  Insert/Deleteは3秒、YCSB-Eは既存の30秒とcheckpointスケジュールを使用した。
  Get等の反復数はGoogle Benchmarkが較正する。
- 再確認はmin_time=5秒、CPUを同じNUMAノードの16物理コア
  （論理CPU 0,2,4,…,30）に固定した。1スレッド条件も同じCPU集合内で動かした。
  CPU affinityの適用値も保存した。
  初回とは条件が違うため、両者の結果は合算しない。

以前の固定時間harnessの結果と、今回の絶対速度・改善率を直接比較しない。
今回は既存ベンチマークの較正も含む実行方式を使っており、要求列やキャッシュの履歴が異なる。
また、書込み混在では測定時間の変更によって更新済みキーの割合も変わり得る。
再確認で初回の低下が出なかった場合も、初回差の原因まで確定したとは扱わない。

## 再現・保存先

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py build
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py prepare
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py matrix
python3 implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py summarize
```

生データは `build/ltm/results/throughput-regression-20260922/`（Git管理外）。
`source/`、`candidate.patch`、`build-*.json`、`provenance.json` に対象ソース・ビルド条件を保存。
各 `r*/` に実行コマンド・環境・適用制限・生のbenchmark.json・throughput.json・終了コードを保存した。
初回集計はinitial-summary.json、完了した追加比較はcompleted-recheck-summary.json。
再確認の計画と完全なコマンドはrecheck-plan.json/recheck-command.json。
停止記録はstopped-by-scope-change.json。計画した全条件を完了したわけではない。
既存の完成済みケースはスキップする。新規比較では保存先を変更する。
