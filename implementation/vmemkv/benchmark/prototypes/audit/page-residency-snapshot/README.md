# 初期スナップショット付き常駐ヒント：監査用資料

対象ブランチは **`codex/page-residency-snapshot`**。mainではなく、このブランチを参照してください。

目的は、**案1のLTM性能改善を維持しつつ、常駐Getの低下を解消すること**です。今回の試作は、不変baseのページ跨ぎ小レコードに、BPFで失効させる常駐ビットマップと初期スナップショットを追加します。

## 最初に読む文書

- [基礎からの独立解説](../../../../docs/benchmark/page_residency_snapshot_explained.md)
- [測定条件・結果・限界](../../../../docs/benchmark/20260923_page_residency_snapshot.md)
- [これまでの判断とブランチ状況](../../../../../../AGENTS.md)

案1比の8条件比較では、常駐+3.14〜+4.60%、LTM−0.83〜+0.88%。元実装との常駐直接比較はUniform1/16が−1.78%/+1.58%、Zipf1/16が−0.68%/−0.47%でした。各版2回の最小比較で、全条件の劣化解消は確認できていません。初期化時間はthroughputと別に記録しています。

## 実際に測定したコード

`source/`は、Git管理外だった実験ソースから**内容を変更せず**取り出した閲覧用コピーです。製品ツリーにあるGetは案1までなので、今回のヒント経路の監査にはこちらも必要です。

| 資料 | 内容 |
|---|---|
| [元実装のGet](source/original/read_path.hpp) / [案1のGet](source/proposal1/read_path.hpp) | 基準となる2版 |
| [今回のGet](source/snapshot/read_path.hpp) | ヒントによるmincore省略と既存mmap/preadへの接続 |
| [ビットマップ](source/snapshot/page_residency_hints.hpp) | 参照、登録、世代・登録禁止の扱い。現行試作の名前は`page_residency_bitmap.hpp` |
| [初期スナップショット](source/snapshot/page_residency_snapshot.hpp) | mincore前の世代取得、範囲ごとの確認・登録 |
| [BPF](../../page_residency_bitmap.bpf.c) / [ローダー](../../page_residency_loader.py) | ページ追加・削除、共有map、対象ファイルの識別 |
| [Get/ハーネス差分](read-path-and-harness.patch) | 元実装→案1、案1→今回の変更 |
| [今回のハーネス](source/snapshot/fixed_pread_benchmark.cpp) / [共通ベンチ本体](source/bench_kv.cpp) | 初期化、キャッシュ準備、Get、計時の方法 |
| [32実行のランナー](../../run_page_residency_snapshot_matrix.py) / [元実装比較のランナー](../../run_snapshot_original_resident.py) | 実行順、メモリ予算、CPU配置、sudo/一般ユーザーの分離 |

ほかのsrc/includeはこのブランチの[製品ソース](../../../../src/)・[公開ヘッダー](../../../../include/)と一致することを保存時に確認しました。[manifest.json](manifest.json)に元の保存場所とSHA256があります。ヘッダーは閲覧しやすい場所へ移したため、この`source/`だけを独立したビルドツリーとしては扱わないでください。

## 測定データ

- [matrix.json](results/matrix.json)：常駐/LTM × Uniform/Zipf × 1/16スレッド、案1/今回、各2回の32実行。
- [original-resident.json](results/original-resident.json)：常駐4条件の元実装/今回、各2回の16実行。
- [fixed-count.json](results/fixed-count.json)：常駐Uniformで案1/空ビットマップ/初期化版を100万Get固定で比較。初期費用と省略率の別計測も含む。
- [skip-rate.json](results/skip-rate.json) / [registration-audit.json](results/registration-audit.json)：空の表での省略率と登録・保持の確認。

各ファイルには集計に加え、個々の測定JSON・実行コマンド・CPU/メモリ制限・終了コード・測定時のprovenanceをまとめています。内容を一つのJSONへまとめただけで、数値は変更していません。異なる実験の絶対速度や改善率を合算しないでください。

記録中の`/home/...`、`build/...`、元のコミットや未コミット状態は測定当時の履歴です。DB・バイナリ・全旧実験の生成物は収録していません。既存ランナーにはこのホストの絶対パスや先行実験の成果物への依存があり、新しい環境でそのまま再実行できるパッケージではありません。コードと既存結果をGitHub上で監査するための保存です。

## 監査で区別してほしいこと

この試作は固定checkpoint・1 Storeの読取りに限定し、製品全体への組込みは未実施です。常駐ヒントはページ固定の保証ではありません。実装が意図を満たしているか、競合や測定方法に問題がないか、性能低下を招く余計な処理や見落としがないかを、既存の結論に同意する前提を置かず確認してください。改善案はこの構成を維持するものに限定する必要はありませんが、同期の単件Get・同じメモリ予算を前提とし、MultiGetは採用しません。
