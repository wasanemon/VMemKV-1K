# VMemKV 1K 性能改善の作業方針

- 第一目標は、[公開ベンチマーク](https://vmemkv.pages.dev/)で報告されている VMemKV の 1K（1KB）性能について、有望な改善案を見つけ、フォーク元の正規管理者である上司に提言すること。
- 作業は **1. ボトルネックの特定 → 2. 改善点の提案** の順に進める。
- 対象は 1KB の In-Memory / Larger Than Memory。値サイズの混在条件（20% は 8B）を含め、対象レポートの測定条件を確認する。
- `implementation/vmemkv/` の実装・設計仕様・テスト・ベンチマークを根拠に調査する。実装は提案の裏付けに必要な最小限の試作にとどめ、オーバーエンジニアリングや過剰な実装を避ける。
- `paper/` は参照・編集せず、検索対象からも除外する。
- 性能を比較する場合は環境・ワークロード・設定を揃える。整合性・永続性への影響は提言に必要な範囲で整理し、厳密な証明や網羅的な検証を追求しすぎない。未確認事項や保証を変える前提は明示する。
- 測定コマンド・環境・対象コミット・結果を記録し、確認済みの事実と仮説を区別する。
- 報告・作業記録は日本語で簡潔に記述する。
- push は既存の SSH 認証を使う：`git push git@github.com:wasanemon/VMemKV-1K.git <ブランチ名>`。

## ブランチと実装の反映状況（2026-09-23・最新）

**監査用保存：** 現在のブランチに作業をまとめてcommit/pushするユーザー指示を受け、[GitHub閲覧用の監査入口](implementation/vmemkv/benchmark/prototypes/audit/page-residency-snapshot/README.md)を追加。以降の個別実験に残る「未コミット・未push」は測定当時の履歴として読む。現在の製品実装は案1まで、BPF/初期化は試作と監査用コピーに保存する。

**「mainのコミット」と「未コミットの作業ツリー」を区別する。案1はmainのコミットには未反映だが、現在の製品作業ツリーには適用済み。** 現在の作業ブランチは `codex/page-residency-snapshot`（`codex/page-residency-bitmap`から既存の未コミット作業を保持して作成）。通常ソース（`src/`・`include/`）と、`build/`内の実験用ソースコピーは別ファイルである。

| 対象 | ブランチ・保存場所 | 現在の状態 |
|---|---|---|
| mainのコミット内の製品実装 | `main`、`implementation/vmemkv/src/` と `implementation/vmemkv/include/` | Getの条件はまだ `if (is_small)`。案1・値分離・A/B追加案はいずれも未適用 |
| 案1：ページ跨ぎ小レコードを既存mincore/preadへ回す | 現在の製品 `src/vmemkv/read_path.hpp` と実験用コピー | 実装・測定済み、有望で上司の確認待ち。現在の監査用ブランチに条件式を保存。mainには未反映 |
| 値・メタデータ分離 | `codex/packed-1kb-values` 上で作成した `packed_values_1kb.cpp` / `run_packed_values_1kb.py` | 読取り専用試作・最小比較が完了。製品実装には未反映。案1に追加したものではなく、元の密詰め形式と比較した独立案 |
| 案1への追加A/B | `main`と既存未コミット作業を引き継いだ `codex/get-read-policy` | 実験用コピーで実装・動作確認・最小測定まで完了。Aは見送り、Bはユーザー判断で棄却。製品src/includeには案1以外を適用していない |
| 小レコードを固定buffered preadで読む | `codex/get-read-policy`から未コミット作業を保持して切り出した `codex/fixed-pread-1kb` | `run_fixed_pread_1kb.py`が実験用コピーに適用。比較基準は案1、A/Bの制御は含めない。製品作業ツリーは案1のみ |
| ページ跨ぎ小レコードの両ページcold時だけDirect I/O | `codex/fixed-pread-1kb`から未コミット作業を保持して切り出した `codex/cold-direct-1kb` | 案1を基準に`run_cold_direct_1kb.py`が別の実験用コピーへ適用。固定buffered pread・A/Bは含めず、通常ソースは案1のみ |
| TSX/RTMでページを試し読み | `codex/fixed-pread-1kb`から切り出した`codex/tsx-probe-1kb` | 独立プローブでRTM利用不可を確認。その後ユーザー判断で棄却。有効化・Getへの組込み・性能測定は未実施。通常ソースは案1のみ |
| BPFで失効させるページ別常駐ヒント | `codex/tsx-probe-1kb`から切り出した`codex/page-residency-hints` | 実験用コピーで実装・BPF動作確認・値照合・1スレッドの4条件比較が完了。常駐Uniform低下、常駐Zipf改善、LTMの差は小さい。16スレッドへ拡大せず終了。通常ソースは案1のみ |
| 常駐ヒントのビットマップ化 | `codex/page-residency-hints`から切り出した`codex/page-residency-bitmap` | 実験コピーで実装・BPF確認・値照合・3版の24実行比較が完了。管理本体は約95.3%減、従来BPF版からのthroughput改善は確認できず。通常ソースは案1のみ |
| 常駐ヒントの初期スナップショット | `codex/page-residency-bitmap`から切り出した`codex/page-residency-snapshot`（現在） | 初回比較は常駐Uniform・1スレッドで案1比+3.36%。8条件比較が完了。案1比で常駐+3.14〜+4.60%、LTM−0.83〜+0.88%。元実装との常駐直接比較は−1.78〜+1.58%で、全条件の劣化解消には至らない。通常ソースは案1のみ |

- 監査用保存前は上記9ブランチが同じ `e75746c2d03e0d53edaf566c157aad95172b887c` を指し、試作は未コミットだった。今回ユーザーのcommit/push指示により、現在の `codex/page-residency-snapshot` に案1の製品差分、ここまでの試作・文書、測定済みの監査資料をまとめて保存する。他のブランチには各実験専用のコミットを作成していない。mainは変更しない。
- 案1の測定済み条件式は `build/ltm/results/throughput-regression-20260922/source/cross-pread/src/vmemkv/read_path.hpp` に残っている。`candidate.patch`も同じ結果ディレクトリに保存。当時の `implementation/vmemkv/benchmark/prototypes/run_regression_throughput.py` は案1未適用の製品ソースをコピーし、条件式を置換した。現在は案1適用済みなので、新しい `run_get_read_policy.py` がそのまま基準コピーを作る。旧ランナーを現状へ無条件に再適用しない。`build/`内はGit管理外。
- 既存の `bench_kv.cpp` / `run_bench.sh` の未コミット修正は保存先・パス・掃除処理等で、案1の製品実装への適用とは別。現在の製品 `src/` / `include/` のmainとの差分は、案1のGet条件式1箇所のみ。

### 常駐ヒントの初期スナップショット：常駐Uniformで改善（2026-09-23）

- ユーザーの提案を受け、`codex/page-residency-snapshot`を作成。既存ビットマップ版の実験コピーに、使用済み不変baseを1024ページ（4MiB）ずつ`mincore`で調べて初期化する処理のみを追加。空/初期化済み版は同一バイナリ・同一Get経路。BPF・既存登録処理・製品ソースは変更しない。未コミット・未push。
- 64ページごとの世代を各mincoreの前に取得し、結果のビットマスクを既存`confirm_bits`で登録する。競合時の登録取消し・再登録禁止を維持するが、既存の一時的な古いヒントや偽陰性の可能性は残る。回収を防ぐ保証にはしない。一時配列は1024B＋128B、管理本体は従来どおり643,680B。
- 比較は常駐Uniform・CPU 0・1スレッド、案1/空ビットマップ/初期スナップショットの3版×各2回、100万Get固定。T2とPTEの事前常駐条件を揃え、初期化時間・初期化後throughput・初期化＋Get時間を分離。BPFロード・ユーザー側map接続の追加初期費用も別記する。省略率は計数版で追加1回だけ確認し、性能測定へカウンターを混ぜない。過去のGB較正あり測定と絶対速度を合算しない。
- 案1・ビットマップ・別計数版のビルドに成功。1,025ページの専用匿名マッピングで常駐/非常駐混在・64ページ境界・1024ページチャンク境界・使用範囲の端を確認し、既存の世代変更/再登録禁止による登録拒否の確認も成功。これは実BPFとの競合試験ではない。
- **実BPF付き結果（各版2回平均）：** 案1 **317,709**、空ビットマップ **314,630（案1比−0.97%）**、初期スナップショット **328,391 ops/s（案1比+3.36%、空版比+4.37%）**。初期スナップショットの案1比は反復別+3.28%／+3.45%で2回とも改善。
- 初期スナップショットは平均**5.148ms**、BPFロード・map接続を含めた追加初期費用は**7.132ms**。この費用＋100万Getの合計は案1 **3.147536秒**→初期化版 **3.052282秒**（時間−3.03%）。共通のDB復元・事前常駐化・プロセス起動/終了は含まず、実運用全体の時間とは扱わない。
- 全1,716,438ページを1,677回のmincoreで確認・登録。別計数実行は**208,357/208,357対象Getでmincore省略（100%）**。両性能実行と計数実行でGet前後とも全ページのヒントを保持し、Get中のBPF世代増分0。空版は両回0→370,908ページ。以前の空版固定回数監査の省略率は約6.07%で、今回空版の再計数はしていない。
- 全7scope正常終了、各回の1,024キー全バイト照合とMiss確認に成功、BPF解除済み。**常駐Uniformでは初期学習の遅さが改善を隠していたという見立てを支持するが、LTMでの有効性・回収競合は未確認。** LTM/Zipf/16スレッド、64ページ遅延初期化、pread後登録へは拡大せず終了。記録：[試作と結果](implementation/vmemkv/docs/benchmark/20260923_page_residency_snapshot.md)。出力は`build/ltm/results/page-residency-snapshot-20260923/`。
- **その後のユーザー依頼による8条件比較が完了：** 案1/初期スナップショットの2版、常駐/LTM×Uniform/Zipf×1/16スレッド、各版2回の32実行。Get・初期化・BPFは初回と同じ実装を使い、既存Google BenchmarkのGet Hit測定（min_time=2秒）へ接続する。前回の100万Get固定とは絶対速度を合算しない。CPUは1スレッドが0、16スレッドが0,2,…,30。常駐High/Max=16/32GiB、LTM=1/2GiB。常駐はT2/PTEを温め、LTMは既存のcold準備を使い、スナップショットは各プロセス1回、GB較正前に実施。初期化時間・確認ページ数を別記し、省略率計測・他操作・原因調査へは広げない。通常ソースと過去の結果を保持。出力は同ディレクトリの`matrix/`、ランナーは`run_page_residency_snapshot_matrix.py`。両版ビルド・1/16スレッドのUniform/Zipf登録確認済み。Get・スナップショットのソースが保存済み版と同一であることを確認。全32scopeが正常終了、CPU/メモリ条件を確認し、各回BPF解除済み。

- **8条件の結果（案1比・各版2回平均）：** 常駐Uniform 1/16スレッド **+3.61%/+4.60%**、常駐Zipf **+3.14%/+4.44%**。全4条件で2回とも改善。LTM Uniform **+0.88%/−0.60%**、LTM Zipf **−0.20%/−0.83%**。LTM Uniform・1スレッドは反復で増減逆転、他のLTM3条件は両回とも低下（Zipf16は−0.15%/−1.51%）。LTMは概ね案1水準だが、劣化ゼロとは扱わない。初期化は常駐平均4.94〜5.16msで全ページ登録、LTM平均26.19〜28.58msで初期登録0。LTMはcold開始で、その後のGetで学習する構成。
- **ユーザーが採用基準を明確化：案1のLTM改善を維持し、元実装に対する常駐時の劣化を解消することが重要。** 案1比の回復だけで劣化解消とは断定せず、常駐4条件だけ元実装と初期スナップショットを各2回直接比較し完了。既存の診断用元実装バイナリ（mincore置換なし）と今回のsnapshotバイナリを再利用し、再実装・再ビルドなし。元実装/案1の保存ソースはGet条件式以外同一、harnessも同一と確認。CPU・予算・GB min_time=2秒は今回と同じ。LTM再測定・原因計測へは広げない。`measure_snapshot_original_resident.sh`で全16scope正常終了、CPU/予算を確認しBPF解除済み。出力は`page-residency-snapshot-20260923/original-resident/`。旧測定との改善率の合算はしない。

- **元実装との常駐直接比較（各版2回平均）：** Uniform1は元実装343,544→初期スナップショット337,413 ops/s（**−1.78%**）、Uniform16は5,446,982→5,533,136（**+1.58%**）、Zipf1は424,870→421,975（**−0.68%**）、Zipf16は7,202,003→7,168,408（**−0.47%**）。Uniform1は反復別−3.46%/−0.05%で元実装側のばらつきが大きく、平均低下幅を安定した劣化とは扱わない。Uniform16は両回改善、Zipf1/16は両回小幅低下。
- **最終評価：案1のLTM性能を概ね維持し、常駐低下の多くを回復したが、全条件で劣化を解消したとは言えない。** 各2回の最小比較で小差の統計的確定は行わない。過去の改善率と合算せず、BPF権限・運用、約0.614MiBの管理本体、初期走査の費用も残る。ユーザーによる正式な採用/棄却判断とは区別する。追加反復・原因調査・実装修正へ広げず終了。製品ソースは案1のみ、未コミット・未push。
- ユーザー依頼で、今回のアイデアだけを基礎から説明する[独立解説](implementation/vmemkv/docs/benchmark/page_residency_snapshot_explained.md)を作成。Get Hitと常駐の違い、ページ跨ぎ、共有ビットマップ、BPF失効、初期スナップショット、競合対策、測定結果と限界を具体例・図で整理した。文書追加のみで、実装・測定の追加はない。

### 常駐ヒントのビットマップ化：容量削減、throughputの追加改善なし（2026-09-23）

- ユーザーが圧縮案の実施を承認。`codex/page-residency-bitmap`を作成し、保存済みBPF版の実験コピーで管理ヘッダーを差し替えた。通常の製品src/includeは案1のみ、既存データ・成果物は保持。未コミット・未push。
- 64ページごとに常駐64bit・再登録禁止64bit・共有世代64bitを配置。計24B/64ページで、現T2の本体は643,680B（約0.614MiB）、従来13,731,504Bから約95.3%減。常駐ビットだけなら214,560Bだが、それを総容量とは扱わない。ヒットは通常1回の64bitロード、64ページ境界を跨ぐ場合は2回。
- BPFは削除で登録禁止→世代更新→常駐ビット解除、追加で常駐ビット解除→世代更新→登録禁止解除。別ページの同時イベントを考慮して全更新をatomic RMWにする。Getはmincore前の世代を保存し、登録前後の世代・登録禁止を確認。不一致なら登録を取り消す。64ページ内の別ページのイベントでも登録が取り消され得る。登録と後検査の間の一時的な古いヒント、正当な並行登録も消す偽陰性は許容し、ページ固定・厳密な常駐保証にはしない。64bit世代の周回は試作対象外。
- 圧縮だけの方向性を評価するため、pread後の登録・8MiB単位の確認・適応制御は加えない。初期ヒントは従来どおり未知。案1・従来8B/page・圧縮版を同じCPU 0／常駐・LTM／Uniform・Zipfで各2回、min_time=2秒、計24実行。16スレッド・他操作・原因別計測へ拡大しない。BPF mapとローダーを同じcgroup予算に入れ、Getは一般ユーザーで実行する。
- ビルド、専用65ページファイルのBPF追加・削除・再追加と隣のビット維持、圧縮版のBPFあり／なし両方で1,024キー×3回の全バイト照合とMiss確認に成功。24測定＋実BPF照合1回の25scopeが全て正常終了し、各回BPF解除済み。記録：[圧縮試作](implementation/vmemkv/docs/benchmark/20260923_page_residency_bitmap.md)。部品は`page_residency_bitmap.hpp`、`page_residency_bitmap.bpf.c`、`build_page_residency_bitmap.py`、`run_page_residency_bitmap.py`、`measure_page_residency_bitmap.sh`。旧ローダーに任意のmap要素サイズ・BPF objectを渡せる引数、旧runnerに実行スクリプト指定を追加したが、既定動作は従来どおり。
- **結果（1スレッド・各版2回平均）：** 常駐Uniformは案1 **327,367**／従来BPF **325,885**／圧縮 **325,652 ops/s**（圧縮の案1比−0.52%、従来比−0.07%）。常駐Zipfは **412,967／422,637／420,039**（+1.71%、−0.61%）。LTM Uniformは **18,409／18,552／18,527**（+0.64%、−0.13%）、LTM Zipfは **42,831／42,825／42,795**（−0.08%、−0.07%）。LTM Uniformの案1比は反復で+1.62%／−0.32%と逆転し、明確な改善とは扱わない。常駐Uniformの従来比も反復で増減が逆転した。
- map作成前後のcgroupメモリ増分は従来13.18〜13.20MiBから圧縮0.70〜0.75MiBへ減少（ローダー等も含む）。容量削減は成功したが、圧縮によるthroughputの追加改善は確認できなかった。13.1MiBのランダム参照が低下の主因という仮説は裏付けられず、参照・登録処理も変わるため原因の否定・確定はしない。16スレッド・他操作・原因別計測へ拡大せず終了。ユーザーによる正式な棄却判断とは区別する。
- 出力は`build/ltm/results/page-residency-bitmap-20260923/`。旧試作のバイナリ・測定結果は上書きしない。
- **省略率の追加確認が完了：** 同じブランチの別コピーに対象Get数／mincore省略数の2カウンターを追加。常駐Uniform・Zipf、CPU 0、各1回100万Getを連続実行し、初期0〜10万Getと後半90万〜100万Getを比較した。分母はページ跨ぎ小レコードの対象Get。**Uniformは126/20,746（0.61%）→2,384/20,630（11.56%）、Zipfは9,454/18,871（50.10%）→13,532/18,539（72.99%）**。Uniformでは後半も約88%でmincoreが残る。両分布とも区間ごとに省略率が増加し、初期未知ヒントの蓄積途中と整合するが、登録失敗・不要な失効とは断定しない。GB較正を通さない固定回数の確認で、速度測定中の省略率そのものとは扱わずthroughputも評価しない。両scope正常終了・BPF解除済み。BPF・登録・失効・経路選択は変更せず、pread後登録も追加していない。[確認記録](implementation/vmemkv/docs/benchmark/20260923_page_residency_skip_rate.md)、出力は`build/ltm/results/page-residency-skip-rate-20260923/`。製品ソースは案1のみ。
- **登録・保持の追加確認が完了：** ユーザー依頼で常駐Uniform・CPU 0・100万Getを1回だけ実行。対象208,357Getで実際の省略と独立記録の期待省略はともに12,637回、判定不一致0。常駐確認後の延べ391,440ページ照合で登録漏れ0、記録した370,908ページは終了時も全て保持。mincoreエラー・非常駐検出・Get中のBPF世代増分は全て0。**この条件では登録・保持の不備は見つからず、低い省略率は初期未知からこの対象経路でまだ確認・登録していないページの多さで説明できた。** 通常mmap経路などで読んだだけではヒントを登録しない。LTM・多スレッドの競合まで不備なしとは扱わない。BPF・元の登録処理は未変更、scope正常終了・BPF解除済み。追加確認はここで終了。出力は`build/ltm/results/page-residency-registration-audit-20260923/`、詳細は上記の省略率確認記録へ追記。

### TSX/RTMのページ試し読み：ユーザー判断で棄却（2026-09-23）

- 利用不可の確認後、ユーザーが明示的に棄却した。TSX有効化・再起動・追加実装・測定は進めない。次の依頼はBPFで失効させるページ別常駐ヒントの確認・計画作成であり、TSX案の続行ではない。

- ユーザー依頼で`codex/tsx-probe-1kb`を作成。`probe_rtm_1kb.cpp`をビルド・実行し、**このホストの現在の設定ではRTMを利用できない**と確認した。全96論理CPUで`rtm`・`hle`なし。CPU 0の直接CPUIDでもleaf 7/subleaf 0のEBX=`0xf3bfb7ef`、RTM bit 11=0。`tsxldtrk`があることはRTM利用可能を意味しない。
- プローブは対応ビットを確認してRTM命令を実行せず終了（`attempts=0`、`decision=use_proposal1`）。Getへの組込み・性能比較は行わない。提案の性能を棄却した結果ではなく、現在の環境では評価不能という判断。BIOS・OS・マイクロコード等のどこで無効化／非公開になっているかは未特定。設定変更・sudo・過剰な追加調査は行っていない。
- 案1を製品作業ツリーに残し、追加したコードは独立の利用可否プローブのみ。RTM対応時に温めた2ページを試すコードは含むが、このホストでは未実行。試し読み後の競合・Getフォールバック・LTM負担も未評価。旧案の判断は維持し、対応が有効な環境を得た場合にだけ次の試作を検討する。
- 記録：[利用可否の確認報告](implementation/vmemkv/docs/benchmark/20260923_tsx_probe_1kb.md)。ソースは`implementation/vmemkv/benchmark/prototypes/probe_rtm_1kb.cpp`。実行コマンド・CPU機能・CPUID・コミット／ブランチ・ハッシュは`build/ltm/results/tsx-probe-1kb-20260923/`（Git管理外）。未コミット・未push。

### BPFによるページ別常駐ヒント：最小試作完了・優位は限定的（2026-09-23）

- ユーザー依頼の[最小試作計画](implementation/vmemkv/docs/benchmark/20260923_page_residency_hint_plan.md)に従い、`codex/page-residency-hints`を作成し、案1の実験用ソースコピーへGetのヒント参照を追加した。対象は不変base・ページ跨ぎ小レコード、固定checkpoint1個のみ。通常の製品ソースは案1のみ、未コミット・未push。
- ホストにはBPF/BTF対応カーネル、clang 15、bpftool、libbpf.so.0.5.0がある。非特権BPFは禁止（`unprivileged_bpf_disabled=2`）。既存libbpfをctypes経由で利用し、追加パッケージ導入なしでビルドした。ユーザーのsudo実行で実機tracepoint定義とmap共有を確認済み。8KiB専用ファイルの状態は初期0→追加4→確認済み5→削除10→再追加12と変化した。BPFプログラムは終了時に解除し、ファイルも削除した。
- 競合対策は、削除通知で世代更新と再登録禁止、追加通知で新世代の未知状態へ戻し、mincore前後の世代一致をCASで確認して登録する方式。追加通知そのものを常駐確認の代わりにはしない。BPFなしのGetフォールバックと実BPF接続の双方で1,024キー×3回の値照合、Miss、世代変化・登録禁止のCAS確認に成功した。案1の1,024キー×2回照合も成功。ヒント参照直後の回収は防がず、競合の網羅的検証は行わない。
- 最小試作は世代＋状態を8B/pageで管理する案。現在のT2実使用範囲では約13.1MiB、8GiBなら16MiB＋map等の管理費。1bit/pageの256KiBは採用実装の総RAMとは扱わない。同じ測定メモリ予算に算入する。まず常駐/LTM×Uniform/Zipfの1スレッド・各版2回、効果がありLTMで明確に悪化しない場合だけ16スレッドへ進む。
- **結果（全て1スレッド・各版2回平均・案1比）：** 常駐Uniform **327,695→324,215 ops/s（−1.06%）**、常駐Zipf **412,136→421,120（+2.18%）**、LTM Uniform **18,391→18,542（+0.82%）**、LTM Zipf **42,892→42,839（−0.12%）**。常駐Uniformは2回とも低下し、LTM Uniformは反復で増減が逆転。明確な総合的優位は示せず、管理RAM・BPF運用の負担に対する見返りは弱い。現時点で案1より優先することは勧めないが、ユーザーによる正式な棄却判断とは区別する。
- 同じ既存1KB/8B混在データ、CPU 0、Google Benchmark min_time=2秒。常駐High=16GiB/Max=32GiB、LTM High=1GiB/Max=2GiB。map作成前後のmemory.current増分13,811,712〜13,856,768Bが測定用cgroupに計上されることを確認。両版でrootのPythonローダーも同じ予算に含め、Get子は一般ユーザーで実行した。ヒントは初期未知で通常Getを通じて蓄積。以前のローダーなし比較と絶対速度を合算しない。
- 測定前に定めた16スレッドへの目安（常駐2条件とも+1%超、LTM2条件とも−1%以上）を満たさず、計16測定＋2検証scopeで終了。全scope正常終了、各回BPF解除。閾値調整、ビット圧縮、他操作、原因別プロファイルへは拡大しない。
- 追加実装は`benchmark/prototypes/`の`page_residency.bpf.c`、`page_residency_loader.py`、`page_residency_hints.hpp`、`build_page_residency.py`、`run_page_residency.py`とsudo実行用の2スクリプト。結果・差分・ソースコピーは`build/ltm/results/page-residency-hints-20260923/`、測定のコマンド・制限・生データ・集計は同`measurements/`。記録：[試作結果・再現手順](implementation/vmemkv/docs/benchmark/20260923_page_residency_hints.md)。通常ソースは案1のみ、未コミット・未push。

### 両ページcold限定Direct I/O：最小試作完了・改善なし（2026-09-23）

- ユーザー依頼で`codex/cold-direct-1kb`を作成し、案1を基準に別ファイルの実験用コピーで試作した。通常の`src/`・`include/`は案1のみ。前回の固定buffered pread・A/Bは含めない。試作・記録は未コミット・未push。
- このホストのext4上の既存不変T2で、512B境界から1536B／2048BのDirect I/Oが成功し、buffered読取りと全バイト一致。読取り前後とも対象2ページがcoldで、buffered対照後は両方常駐。1BずらしたoffsetはEINVAL。今回の2ケースで細粒度DIO・ページキャッシュ非投入を確認したが、ブロックI/OトレースやSSD内部の転送量は測定していない。
- 既存1回のmincoreの結果を再利用し、両ページ常駐はmmap、片方だけ常駐はbuffered pread、両ページcoldかつ`size_hint <= 4096`だけDirect I/O。512B単位へ丸めた範囲をスレッド別の4096B整列8KiBバッファへ読む。別openしたO_DIRECT fdを初期化時に登録し、失敗時や丸め範囲が不変baseを超える場合はbufferedへ戻す。ページ内小レコード・T1インライン8B・tail・Scanは既存どおり。
- 両版で1,024キーを2回ずつ全バイト照合、Miss確認に成功。追加版は4通りの常駐状態で実際の返却先と値を確認し、DIO使用不可時のbufferedフォールバックも確認した。
- **案1比でLTM Get Hit・1スレッドは悪化：** Uniform **18,588→17,825 ops/s（−4.10%）**、Zipf **42,928→40,891（−4.75%）**。各版2回の平均で両分布とも2回とも低下。High=1GiB/Max=2GiB、CPU 0、既存8,259,552キーの1KB/8B混在、Google Benchmark min_time=2秒。計8実行で終了し、16スレッド・常駐・他操作・原因別計測へは広げなかった。今回の条件では採用を勧めない。前回の固定buffered preadの有望判断、案1の上司確認待ち、Bの棄却判断は維持する。
- トレードオフ：保存容量は増えないが追加RAMは8KiB/スレッド＋fd等。DIOではキャッシュが温まらず、再利用時の読込みが増え得る。mincore後の状態変化もあり得る。単一checkpoint読取り専用で、複数Store・checkpoint切替・更新・recovery等の汎用対応は未実装。大レコードの読取り方式は維持するが共用mincoreループの変更は含む。
- 記録：[比較報告・再現手順](implementation/vmemkv/docs/benchmark/20260923_cold_direct_1kb.md)。部品は`benchmark/prototypes/`の`probe_direct_1kb.py`、`cold_direct_1kb.hpp`、`cold_direct_verify.inc`、`run_cold_direct_1kb.py`。生データ・ソース・差分・コマンド・環境は`build/ltm/results/cold-direct-1kb-20260923/`（Git管理外）。

### 小レコードの固定buffered pread：最小試作完了

- `codex/fixed-pread-1kb`で、案1適用済みの作業ツリーを比較基準に試作・測定した。製品src/includeは案1のみのまま。`run_fixed_pread_1kb.py`が実験用コピーの`read_path.hpp`で、`read_large_get_cold()`に常駐確認を省く引数を加え、不変baseの`is_small`なGetから直接呼ぶ。Aのfd／先読み制御、Bの適応制御は含めない。
- 対象判定は`size_hint <= 4096`であり、厳密な1024B値限定ではない。今回の混在データでは外部1024B値が対象。ページ内・ページ跨ぎとも通常は事前mincoreなしのbuffered preadで読み、8BのT1インライン、mutable tail、Scan、大レコードの既存経路は維持する。既存バッファ・キー／長さ検証・失敗時フォールバックを再利用し、保存形式・容量は変更しない。
- **LTM Get Hitの案1比：** Uniform・1スレッド **18,597→18,853 ops/s（+1.37%）**、Zipf・1スレッド **42,967→44,560（+3.71%）**、Uniform・16スレッド **246,938→261,076（+5.73%）**、Zipf・16スレッド **807,741→880,910（+9.06%）**。各版2回の平均で、全条件で2回とも改善。追加改善の方向として有望。旧Bの棄却と案1の上司確認待ちは維持する。
- 同じ既存8,259,552キー（16Bキー、80%が1024B値・20%が8B値）、High=1GiB/Max=2GiB、1スレッドCPU 0・16スレッドCPU 0,2,…,30、Google Benchmark min_time=2秒。まず1スレッド8実行で両分布の改善を確認し、16スレッド8実行を追加して終了した。過去の異なるharness／CPU配置の比較と合算しない。
- 両版で1,024キーを2回ずつ全バイト照合し、Miss確認も成功。初回はLTM Getのみ。その後の常駐・Get/Update混在の追加比較は次項。ほかのサイズ・全テスト・原因別プロファイルへは広げていない。mmapのfault／マッピング／回収コスト軽減は仮説で、原因内訳は未測定。常駐ヒットにもシステムコールとコピーが入り、preadもページキャッシュを使うというトレードオフが残る。
- **追加比較（2026-09-23、ユーザー依頼・同じバイナリ・各版2回、計20実行）：** 常駐Get Hit Uniform・1スレッドは330,150→282,034 ops/s（**−14.57%**）、Zipf・1スレッドは416,090→369,272（**−11.25%**）、Uniform・16スレッドは5,338,202→4,078,169（**−23.60%**）、Zipf・16スレッドは6,922,034→5,049,816（**−27.05%**）。全4条件で2回とも低下。LTM Zipf Get/Update 50%ずつ・1スレッドは14,611→14,696（**+0.59%**）だが反復間で増減が逆転し、明確な改善とは扱わない。
- 追加条件は常駐High=16GiB/Max=32GiB、LTM High=1GiB/Max=2GiB、CPU配置・データ・min_time=2秒は初回と同じ。混在は毎回専用checkpointコピーへ戻した。**LTM Getへの有望判断は維持するが、一律適用では常駐Getの大きな低下を伴う。** 全条件で劣化なしとは扱わない。Scan・書込み単独・他サイズ・原因別計測へは広げず終了。追加ランナーは`run_fixed_pread_1kb_extra.py`、結果は同報告への追記と`build/ltm/results/fixed-pread-1kb-20260922/extra-20260923/`。混在用一時DBは削除し、元DB・ログは保持する。
- 記録：[比較報告・再現手順](implementation/vmemkv/docs/benchmark/20260922_fixed_pread_1kb.md)。ソース生成ランナーは`implementation/vmemkv/benchmark/prototypes/run_fixed_pread_1kb.py`。両版のソース・差分・バイナリ・完全なコマンド・制限・結果は`build/ltm/results/fixed-pread-1kb-20260922/`（Git管理外）。試作・記録とも未コミット・未push。

### 案1への追加A/B：最小試作完了

- ユーザーの合意により、未コミット作業を保持した`codex/get-read-policy`上で製品のGet条件式に案1を適用し、そのコピーを比較基準とした。mainのコミットに案1が入っているわけではない。値・メタデータ分離は混ぜていない。コミット・pushは行っていない。
- **A：** 初期化時に`/proc/self/fd/<read_fd>`を別途openし、同じinodeと`POSIX_FADV_RANDOM`設定成功を確認してGet用fdを置換。既存のpread・fd破棄処理を利用し、Scan側のopen file descriptionは変更しない。
- **B：** ページ跨ぎ小レコードだけ、スレッド別に32回の観測を集計。31回以上が常駐なら直接mmap、31回以上が非常駐なら直接pread、混在・情報不足時は毎回mincore。省略中も64回に1回確認し、逆の結果で毎回確認へ戻す。既存の境界・キー・読込み結果の検証は維持。閾値調整や複数Storeの寿命を扱う汎用実装は行っていない。
- **比較結果（案1比）：** AのLTM Uniformは18,587→18,636 ops/s（**+0.26%**）。BはLTM Uniformが18,611→18,665（**+0.29%**）、LTM Zipfが42,976→43,322（**+0.81%**）、常駐Uniformが328,829→341,028（**+3.71%**）。いずれも1スレッド・論理CPU 0固定・各版2回の平均。同じ既存1KB混在データ、LTM High=1GiB/Max=2GiB、常駐High=16GiB/Max=32GiB。A/B独立比較の計16実行で終了。
- **Bの追加比較（同じバイナリ、ユーザー依頼の3条件・各版2回、計12実行）：** 常駐Zipf Get Hit・1スレッドは418,833→431,572 ops/s（**+3.04%**）、LTM Uniform Get Hit・16スレッドは248,108→239,094（**−3.63%**）、LTM Zipf Get/Update 50%ずつ・1スレッドは14,589→14,763（**+1.19%**）。1スレッドCPU 0、16スレッドCPU 0,2,…,30へ固定。混在は毎回専用checkpointコピーで比較した。
- **Bはユーザー判断で棄却。今回の改善対象はLTMであり、常駐時の改善を採用理由にしない。** LTMの1スレッドでの上積みは小さく、LTM Uniform・16スレッドは2回とも低下（平均−3.63%）した。明示的な再検討依頼がない限りBの追加実装・測定を進めない。混在の差は小さい。原因別計測・閾値調整・追加拡大は行わず終了した。追加結果は同報告の追記と `build/ltm/results/get-read-policy-20260922/B-extra/`、追加ランナーは `run_get_read_policy_extra.py`。混在用の一時DBは削除し、元DBとログを保持する。
- Aは今回大きなthroughput改善を示さず見送り。Bの常駐コスト軽減は観測したが、上記のLTM重視の判断で棄却。案1は有望・上司の確認待ちのまま。元実装との再比較はしていないため、案1適用前の性能を完全に回復したとは断定しない。I/O量や実際の先読み量は未測定。
- 3版で1,024キーを2回ずつ全バイト照合し、Missも確認。Bの常駐・非常駐・混在・逆転・定期確認の状態遷移も確認した。初回はA+B、他操作、16スレッドへは広げず、その後のB追加範囲は上記3条件だけ。全テスト・再プロファイルは行っていない。
- ソース：`implementation/vmemkv/benchmark/prototypes/get_read_policy.hpp`、`get_read_policy_verify.inc`、`run_get_read_policy.py`。A/Bの実装差分はランナーが実験用コピーに適用する。記録：[比較報告](implementation/vmemkv/docs/benchmark/20260922_get_read_policy.md)。生データ・3版のソース・完全なコマンド・基準差分は `build/ltm/results/get-read-policy-20260922/`（Git管理外）。

## 改善案の判断・引き継ぎ（2026-09-22）

- **案1は有望で、上司の確認待ち。案2・案3はユーザー判断で棄却。** 明示的な再検討依頼がない限り案2・案3の実装・測定を進めない。過剰な確認は不要。必要な追加評価も判断に必要な最小限に絞り、性能指標はthroughputを対象とする。
- **案1：ページ境界をまたぐ小レコードを既存の常駐確認・pread経路へ回す。** `read_path.hpp` のGet分岐を `if (is_small && size_hint <= kPageSize - offset % kPageSize)` に変更した。`size_hint`、`mincore()`、`pread()`は既存処理を利用。試作・比較は完了。当初は実験用コピーのみだったが、現在はユーザー指示で製品作業ツリーにも未コミット適用済み（mainのコミットには未反映）。
  - 同一ホストのthroughput比較では、1KB LTM Get HitのUniform/Zipf × 1/16スレッドで **+8.9〜+22.8%**（各版2回の平均）。CPU配置を揃えた別の追加比較では、常駐1KB Get Hitが **1スレッドで−1.5〜−3.6%、16スレッドで−4.5〜−5.0%**。両比較は条件が異なるため合算しない。
  - トレードオフ：保存容量・形式の変更は不要だが、対象Getに常駐確認が加わり、非常駐時はバッファへのコピーも発生する。常駐時の低下は確認済み。2026-09-23の切り分けで、追加されたmincore呼出しのコストが主因と判断した（次項）。カーネル内部の処理別内訳は未測定。全条件で劣化なしとは扱わない。
  - **常駐低下の原因切り分け（2026-09-23、ユーザー依頼）：** 常駐1KB Get HitのUniform/Zipf × 1/16スレッドだけ、元実装・案1・同一案1バイナリのmincore置換版を各2回（計24実行）比較。全ページを事前に温め、LD_PRELOADでmincoreだけ常駐結果に置換し、追加条件式・vector処理・レコード検証・mmap選択は残した。元実装比の案1は順に**−2.54%、−3.69%、−4.55%、−5.46%**（Uniform1、Zipf1、Uniform16、Zipf16）、置換後は**+1.25%、+0.39%、−0.20%、−0.41%**まで回復。4条件とも2回ずつ案1の低下と置換による回復を確認し、mincore呼出しの追加コストを主因と判断する。
  - 原因調査はHigh=16GiB/Max=32GiB、全条件CPU 0,2,…,30の集合、Google Benchmark min_time=5秒で、元の退行追加比較に揃えた。診断版は常駐前提であり、製品・LTMには使わない。mincore内部の時間配分・特定ロック競合・小さな残差は未特定。sudo・perf・追加ワークロード・改善実装には広げず終了。Bの棄却判断は維持。記録：[原因調査報告](implementation/vmemkv/docs/benchmark/20260923_proposal1_resident_cause.md)。部品は`benchmark/prototypes/run_resident_mincore_diagnosis.py`と`resident_mincore_stub.c`、生データは`build/ltm/results/resident-mincore-diagnosis-20260923/`。当時の`codex/fixed-pread-1kb`で未コミット追加し、製品ソースは案1のみのまま。
- **案2：レコードをページ内に収める配置。棄却理由は容量増と、それに伴うキャッシュ効率・実装範囲のトレードオフ。** 試作比較は実施済み。
  - ヘッダー24B + キー16B + 値1024B = 1064Bのレコードを4KBページに3件配置すると904B余り、密詰めに比べ **T2の必要領域が約28.3%増える**。DB全体やRAM使用量が一律28.3%増えるという意味ではない。
  - 境界またぎの追加読込みを減らせる一方、同じページキャッシュ容量で保持できるレコード数が減る。製品化では割当て・checkpoint/recovery・defragmentも配置に対応させる必要がある。これらのオンライン処理は未実装で、試作の改善だけをもって採用とはしない。
- **案3：複数GetのI/Oを並行発行するMultiGet／非同期Get。棄却理由はAPI・ワークロード変更と実装・実行時コストのトレードオフ。** 実装・測定は行っていない。ユーザー方針としてMultiGetは採用しない。
  - 待ちを重ねてthroughputを上げる狙いだが、単件Getの完了後に次を呼ぶ現行1スレッド条件では要求間の並列性が生まれない。既存の単件Get改善率と同列には比較できず、デバイスが飽和していれば上積みも限定的。
  - 案2のような保存容量増は必須ではないが、同時要求数に応じた読込みバッファ・管理情報がRAMを使い、LTMのページキャッシュと競合する。要求管理・完了通知のCPU負担、混雑時の応答時間悪化、完了までのバッファ・ファイル寿命管理、呼出し側の非同期対応が必要になる。効果・コストの量は未測定。
- 根拠：[案1・案2の試作結果](implementation/vmemkv/docs/benchmark/20260922_1kb_ltm_get_hit_prototype_results.md)、[throughput比較](implementation/vmemkv/docs/benchmark/20260922_get_throughput_regression.md)、[案1のLTM Get Hitグラフ](implementation/vmemkv/docs/benchmark/20260922_1kb_ltm_get_hit_throughput.pdf)。以下のボトルネック節は改良前の調査記録であり、改善案の最新判断は本節を優先する。

## 追加案：1KB値とメタデータの分離（2026-09-22）

- ユーザーの明示依頼で `codex/packed-1kb-values` を作成し、読取り専用の最小試作を完了。旧案2の余白挿入とは別の配置案であり、旧案2・案3の棄却判断は維持する。
- 値を1024Bずつ4件/4KBページ、ヘッダー24B＋キー16Bを別ファイルへ40Bずつ密詰め。T1の16Bキー比較でキー全体を確定し、Getではメタデータを読まない。試作は既存の密詰めoffset / 1064をスロット番号として使い、T1や製品src/includeを変更していない。
- 同じ既存データ、LTM High=1GiB / Max=2GiB、Uniformのみ、1/16スレッド、各版2回（全8実行）。12秒ウォームアップ＋10秒計測の同一harness内で、throughputは1スレッド **12,612→15,490 ops/s（+22.8%）**、16スレッド **146,966→183,952 ops/s（+25.2%）**。前のGoogle Benchmark方式による案1の改善率とは直接比較しない。
- メタデータ込みの論理使用量は両版 **7,030,530,024B**、ファイルシステムの割当差は試作が **44KiB増**。旧案2の約28.3%増はない。追加のRAM索引もない。元DBはT1読込み・比較用に別途保持している。
- 変換時に全外部レコードの形式・キー・密詰めoffsetを確認。4,096キーの値照合と16件のMiss確認に成功。Zipf・常駐・他操作・原因別計測へは広げていない。
- 制約：固定16Bキー・外部値1024B・不変checkpoint限定。更新、可変長データ、mutable tail、通常checkpoint/recovery、defragment、Scanは未対応。配置変更に伴う製品化コストは残る。ユーザーは過剰な実装・計測を望んでおらず、現時点の方向性確認は完了。
- 詳細：[試作結果・再現手順](implementation/vmemkv/docs/benchmark/20260922_packed_values_1kb.md)。ソースは `implementation/vmemkv/benchmark/prototypes/packed_values_1kb.cpp`、ランナーは `run_packed_values_1kb.py`。生データは `build/ltm/results/packed-values-1kb-20260922/`（Git管理外）。

## 確認済み：1KB LTM Get (Hit) のボトルネック（2026-09-22）

- **主因は、T2の非常駐ファイルページをGetの実行スレッドが同期的に読み込む際のI/O待ち。ボトルネック特定は完了済みとして引き継ぎ、同じ結論を得るための再計測・perf再採取・環境調査を繰り返さない。** 改善案を求められたら、この結果を前提に検討を始める。追加計測は改善案の比較や条件変更など、新たな判断に必要なものに絞る。
- 確認範囲：`e75746c2d03e0d53edaf566c157aad95172b887c` と既存の保存先修正、GCC 13.4 / Release。`Bloom-T1InlineValue`、8,259,552キー、キー16B、値は80%が1024B・20%が8B。Zipf（パラメーター1.0）/ Uniform、1 / 16スレッド。`MemoryHigh=1GiB / MemoryMax=2GiB` の適用を確認し、初期化・クローン作成を除いた区間を測定した。
- 待ちの根拠：Uniform・1スレッドで27.819秒中CPU時間は4.313秒、**84.5%が非CPU時間**。major faultと主スレッドの自発的コンテキストスイッチはともに313,796回。待機場所の観測は55点中47点が `wait_on_page_bit_common`。
- 発生先の根拠：別実行のperfで、最終区間のmajor fault **3,251サンプル中3,250件（99.97%）がT2ファイル**。この割合はフォルトの採取先の内訳であり、全GetのI/O発生率や待ち時間の割合ではない。
- 実装上の位置：`implementation/vmemkv/src/vmemkv/read_path.hpp:134` のヘッダー読込がフォルトサンプルの78.5%、`implementation/vmemkv/benchmark/bench_kv.cpp:648` の `touch_bytes()` による値の全走査が20.8%。後者675件はすべて4KBページ先頭で発生し、1KB値でもページ境界をまたぐ際に追加フォルトすることを確認した。
- ボトルネック調査時の改良前1KB Getは **MAP_SHARED + MADV_RANDOM** のT2を直接読む。大きい値向けの `mincore()` / `pread()` 経路は使わない。8Bのインライン値はT1から返す。過去のMAP_PRIVATE版の大量Swapという説明を現在の実装に当てはめない。
- メモリの実測：T2実使用量6.55GiBに対し、LTM終了時のファイルキャッシュは約656〜667MiB、索引等の匿名領域は約324〜328MiB。Uniform・1スレッドは0.864 major fault/Get、デバイス読込3,541B/Get。平均値サイズ820.8Bに対する約4.3倍の読込量は、ページ単位の読込・ページ境界の通過・少量のSwap等を含む。
- 通常計測の速度：1スレッドはUniform **13,053** / Zipf **49,823 ops/s**、16スレッドはUniform **147,513** / Zipf **652,306 ops/s**。両分布・並列条件でもページ読込待ちが主因という傾向を確認した。
- 対照実験：同じデータ・バイナリのUniform・1スレッドで制限だけを `High=16GiB / Max=32GiB` に緩めると **332,362 ops/s（25.5倍）**、major faultとデバイス読込は0。この倍率はメモリ条件の差を表し、実装改良の実績・期待改善率として使わない。
- 切り分け：LTM通常4条件の終了時Swap使用量は3.3〜49.9MiBで、Swap不足を主因とする証拠はない。測定中の `Checkpoints`、`T1_Splits`、`Reorganize_Wait_Duration_us` はすべて0。T1検索やロック競合を主因とする証拠も得られていないが、カーネル内部のロック別時間やデバイス帯域上限は未検証。
- 適用限界：各条件1回。較正時のキャッシュ状態の影響・信頼区間は未評価。他バリアント・書込み混在・最大96スレッドへは断定を広げない。公開AWS測定とはホストが異なるため、速度差を改善率として比較しない。このボトルネック調査時点では改良実装は未着手（その後の試作・判断は上記「改善案の判断・引き継ぎ」を参照）。
- 詳細：[調査報告（Markdown）](implementation/vmemkv/docs/benchmark/20260922_1kb_ltm_get_hit_bottleneck.md) / [HTML版](implementation/vmemkv/docs/benchmark/20260922_1kb_ltm_get_hit_bottleneck.html)。生データ・完全な測定コマンド・観測スクリプトは `build/ltm/results/get-hit-20260922/`（Git管理外）。主要集計は `summary.json`、perfの分類は `profile-uniform-t1/fault-classification.json`。

## HTML文書の閲覧

- HTMLの調査報告を作成・更新したら、ファイルへのリンクに加えて、Codex内のブラウザーで文書として読めるプレビューを用意する。`open_in_codex` の `target.type="file"` はソースコード表示になるため、閲覧には `target.type="browser"` とHTTPのURLを使う。
- この作業環境はリモートホストで、リモートの `file://` URLは手元のブラウザーから直接開けない。実験ホストの `127.0.0.1` にプレビューサーバーを起動し、報告書と必要な参照ファイルに配信対象を限定する。サンドボックスでソケット作成が拒否された場合は、通常の制限外実行手順を使う。
- HTTP応答が正常で、`Content-Type: text/html` と文書の内容が一致することを確認してから、`open_in_codex` で開く。Codexの転送により手元のポート番号が変わる場合があるため、過去のポート番号を固定の閲覧先として扱わない。
- 閲覧中はプレビューサーバーを維持する。停止後は再起動してブラウザーのURLを更新する。プレビューはローカル閲覧用とし、外部への公開は別途依頼がある場合に行う。

## ローカル実験の引き継ぎ（2026-09-22）

- このホストでは準備済みの環境を再利用し、依存導入・環境調査・全テストを繰り返さない。1KB LTM Get (Hit)の性能計測・ボトルネック調査は完了済み（上記）。テスト本体は未実施。
- `build/vmemkv-gcc13` にGCC 13.4・Releaseでビルド済み。Google Benchmark v1.8.4 / doctest v2.4.11取得済み。RocksDB・LMDB・LeanStoreは無効。GCC 13の明示指定と `-DCMAKE_CXX_STANDARD_LIBRARIES=-ltbb` が必要な環境で、既存CMakeキャッシュに設定済み。
- 実験前に `source /home/wasanemon/project/VMemKV-1K/build/ltm/env.sh`。DB・一時ファイル・結果は `build/ltm/{data,tmp,results}` に保存する。ランナーの保存先・掃除処理は修正・検証済み。`bench_kv` を直接実行するときは結果JSONに `--benchmark_out` も指定する。
- ランナーには `--build-dir build/vmemkv-gcc13 --no-rocksdb --no-lmdb --no-leanstore` を渡す。全ケースをいきなり回さず、まず **1KBのケース**から。標準LTMの全体実行には64KBも含まれるため対象を絞る。
- LTM標準条件は算定メモリ予算1GiB・倍率8、実際の制限は `MemoryHigh=1GiB / MemoryMax=2GiB`。`systemd-run --user --scope` で適用できることを確認済み。サンドボックス内ではユーザーバス接続が拒否されるため、必要なら制限外で実行する。実験時も制限の適用値を記録する。
- Swapは総量4GiBのまま、今回のGet (Hit)計測を完了した。増設は不要だった。T2は通常ファイルの `MAP_SHARED` で、RAM超過分すべてがSwapを使うわけではない。書込み時のピークは未確認であり、今回の読み取り結果をそのまま適用しない。`MemorySwapMax=1TiB` は許可上限で、実容量ではない。
- 詳細・再現コマンドは `build/vmemkv-gcc13/SETUP.md` と `build/ltm/{STORAGE_SETUP,MEMORY_CHECK,SWAP_ESTIMATE}.md`。これらと成果物はGit管理外のローカルファイル。別ホストや削除後には再利用できない。
