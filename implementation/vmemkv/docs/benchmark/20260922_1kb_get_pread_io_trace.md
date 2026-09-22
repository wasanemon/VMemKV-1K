# 2026-09-22: 案1のpreadとデバイスI/Oの対応

## 確認できたこと

**両ページが非常駐で、ストレージ上でも連続している境界またぎレコードでは、
現行Getの「4KBを読み終えてから次の4KBを読む」が、案1では「8KBを1回で読む」に変わった。**
GetスレッドのI/O待機も2回から1回に減った。対象128回すべてで同じ対応を確認した。

| 境界またぎの単件Get | 現行 | 案1 |
|---|---:|---:|
| 観測回数 | 128 | 128 |
| pread呼出し | 0回 | 1回、1072B要求・1072B返却 |
| ファイルシステムから出たbio | 4KB × 2件 | 8KB × 1件 |
| sdaへのrequest発行・完了 | 4KB × 2件 | 8KB × 1件 |
| 合計読込み量 | 8192B | 8192B |
| Get中のD状態への切替え | 2回 | 1回 |
| Get中のmajor fault | 2回 | 0回 |
| block層のbio front/back merge | 0回 | 0回 |

現行は**1件目の完了が2件目の発行より前**で、128回すべてが直列だった。
案1はpreadの入口・出口の間に、8KBのrequest発行・完了が各1件あった。
後からブロック層で2件を合流したのではなく、**最初のbioがすでに8KBだった**。
pread側のrequestはRA（read-ahead）として記録されたが、今回新たに読んだのは必要な2ページだけだった。

これにより、案1が「読込み量を削減せず、読み方と待ち方を変えて速くする」機序を、
この条件では実際のイベントで裏付けられた。
以前の[全体比較](20260922_1kb_ltm_get_hit_prototype_results.md)の15〜27%改善について、
すべてのGet・改善幅全体をこの機序だけで説明したことにはならない。

## 同じキーでの時系列の例

レコード位置5,338,587,016B、実サイズ1064B、サイズヒント1072B。
時刻は各Get開始からの相対値。単位はµs。

| イベント | 現行 | 案1 |
|---|---:|---:|
| pread入口 | — | 6.205 |
| 1件目のrequest発行 | 15.357（4KB） | 14.715（8KB） |
| 1件目の完了 | 96.364 | 81.558 |
| 2件目のrequest発行 | 107.771（4KB） | — |
| 2件目の完了 | 187.813 | — |
| pread出口 | — | 86.585 |
| Get終了 | 193.353 | 89.149 |

現行の2件は隣接するsdaセクター770,507,304と770,507,312へ、それぞれ8セクターを読んだ。
案1は770,507,304から16セクターを読んだ。1セクターは512B。
この例を含む全128回で、案1のpreadのoffset・count・返却値もGetの対象と一致した。

トレースなしの対照でも同じI/O件数・読込み量・待機回数だった。
境界またぎのGet時間中央値は現行181.4µs、案1 96.0µs。
トレースありでは182.7µs、97.1µs。
毎回キャッシュを落とした少数の単件Getなので、この倍率を通常のLTM全体の改善率には使わない。

ページ内に収まる16キー×2回も対照にした。両版とも32回すべてが
preadなし、4KBのrequest 1件、I/O待機1回で、変更対象以外の読込みは一致した。

## 対応付けの方法と条件

- 既存のGCC 13.4 / Release、Linux 5.15.0-186-generic / ext4 / sdaを使用。
  コミットは `e75746c2d03e0d53edaf566c157aad95172b887c` と既存作業。
  製品のsrc/includeは変更せず、既存試作の現行Get・`cross_page_get()`をそのまま呼んだ。
- 元データは8,259,552キー、16Bキー、約80%が1024B・約20%が8Bの既存密詰めcheckpoint。
  今回はI/O機序を確認するため、**その中の1024B値だけ**を選んだ。
  通常のUniform/Zipfの要求割合を再現する性能測定ではない。
- seed=20260922で、境界をまたぐ64キーと収まる16キーを選択。
  キー間を2MiB以上離し、同じ80キーを両版で読み、2回目は版の順序を逆転した。
  各観測実行は320 Get。トレースなし・ありの計640 Getで値の全バイトを走査し、
  生成元の値のチェックサムとの一致と正常終了を確認した。
- 1スレッド。独立したuser scopeでMemoryHigh=1GiB / MemoryMax=2GiBを適用・実測確認。
  初期化と初回のコード・バッファ準備は観測前に実施。
  Getごとに専用T2の対象周辺1MiBだけをmadvise/fadviseでキャッシュから外し、
  対象を含む16ページがすべて非常駐であることをmincoreで確認した。
  初期化・キャッシュ操作・観測間の待機はGet時間に含めない。
- 各Getの開始・終了をCLOCK_MONOTONICで記録し、同じ時計のperfイベントへ対応付けた。
  preadとsched_switchは対象スレッドに限定。blockイベントはsdaおよびその上の対象論理デバイスの読込みに限定。
  blockイベントの全体450件の発行・完了から、Get区間内の448件を対応付けた。
  区間外の2件はGetの集計に含めない。
- filefragのextentから対象ファイルのページをdm-2のセクターへ変換し、
  対象スレッドのbio_queueと一致することを確認。
  物理sdaのbio、request発行、完了のセクター・長さも各Get内で一致した。
  I/Oエラー0、各イベントのsample_period=1、イベント欠落の報告なし。
- `/sys/block/sda/stat` はホスト全体の統計なので単独で断定に使わず、
  上記トレースとプロセスのread_bytesで照合した。
  各Get直前の2msの空区間では読込み0件。各Getの統計差分もトレースと一致した。

## 適用限界

確認したのは、対象の2ページがともに非常駐で、ファイルの物理配置が連続するケース。
片方だけ常駐、extent境界で物理配置が不連続、多スレッドの通常LTM要求列については
今回のイベント対応付けの対象外。
したがって「preadは一般に必ず1回のデバイスI/Oになる」とは言わない。
requestの発行・完了までを観測しており、SSD内部のフラッシュ読出し回数は観測していない。

カーネルのbuffered readはページキャッシュが欠けるとreadahead/readpageで補充する。
またLinux 5.15のreadaheadには、小さなランダム要求を要求ページ数の範囲で読む経路がある。
今回の「1072B要求が必要な2ページを覆う8KBのbioになる」という観測はこの仕組みと整合する。
ただしカーネル関数別の呼出し経路をトレースしたわけではない。
参照: [filemap.c](https://github.com/torvalds/linux/blob/v5.15/mm/filemap.c) /
[readahead.c](https://github.com/torvalds/linux/blob/v5.15/mm/readahead.c)。

## 再現と保存先

```bash
source build/ltm/env.sh
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_io.py build
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_io.py stats
python3 implementation/vmemkv/benchmark/prototypes/run_get_hit_io.py trace
```

traceがreadyを書いて待機した後、別ターミナルで実行する。

```bash
sudo bash implementation/vmemkv/benchmark/prototypes/capture_get_hit_io.sh
```

利用者のsudo認証で10秒間採取した。sysctl・tracefsの権限・既存ftrace設定は変更しない。
採取が終わってから解析する。

```bash
filefrag -v build/ltm/data/get-hit-prototypes-20260922/dense.t2chk > build/ltm/results/get-hit-io-20260922/filefrag.txt
python3 implementation/vmemkv/benchmark/prototypes/analyze_get_hit_io.py
```

生データは `build/ltm/results/get-hit-io-20260922/`（Git管理外）。
`stats/` と `trace/` のoperations.jsonl、command.json、before/afterのメモリ制限を保存した。
`trace/perf.data`、perf-script.txt、perf-events.txt、perf.logがカーネル観測、
`correlated-operations.json` が全Getとイベントの対応、`summary.json` が集計。
ビルドコマンド、コンパイラ、コミット、ソース・バイナリのSHA256はprovenance.json等に保存。
既存の結果を上書きしないため、再実行時は各スクリプトの保存先を新しいディレクトリへ変更する。
