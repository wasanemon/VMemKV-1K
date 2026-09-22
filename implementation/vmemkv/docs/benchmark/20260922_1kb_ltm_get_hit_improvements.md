# 2026-09-22: 1KB LTM Get (Hit) の改善候補

**追記:** 案1・案2の試作比較を完了。
[検証結果](20260922_1kb_ltm_get_hit_prototype_results.md)ではLTM全4条件で改善を確認した。
以下は検証前の候補整理。案3（MultiGet）は依頼により見送り、実装・測定していない。

## 判断

改善の余地はある。ただし、同じメモリ予算で非常駐の値を取得するI/O自体はなくせない。
Get (Hit) はキーの存在を意味し、RAMへの常駐を意味しない。
目標は **Getの処理時間・I/O量・直列の待ちを減らすこと**。
`pread()`へ変えてmajor faultの計数だけが減っても、高速化の証拠にはならない。

[既存調査](20260922_1kb_ltm_get_hit_bottleneck.md)の結論を前提とした候補整理であり、
ボトルネックの再計測は行っていない。候補整理時点では、以下の改善効果は未測定だった。

## 第一候補: ページをまたぐ小レコードだけ既存の読込み経路へ回す

現行の `src/vmemkv/read_path.hpp:248` は `size_hint <= 4096` で小レコードと判定し、
Getでは無条件に直接mmapで読む。しかし、レコード長が4KB以下でも、開始位置次第では2ページを読む。
T1のpayloadにoffsetとサイズのヒントがあるため、T2ヘッダーを読む前にこの場合を判別できる。

最小試作では、Get側の直接mmap条件だけを次のように狭める。
Scanのサイズ分類は変更しない。

```cpp
// BaseReader::kGet 内の if (is_small) の置換案。未適用。
if (is_small && size_hint <= kPageSize - (offset % kPageSize)) {
  return read_base_record_via(Selector::for_get_small(mem), offset, base_boundary);
}
```

以後の既存分岐を使い、通常構成のページまたぎレコードは
`read_large_get_cold()` の **常駐確認 → 常駐ならmmap、非常駐なら範囲を限定したpread** に回す。
RandomOnly / SeqOnlyの固定ポリシー、変更可能なtailのseqlock経路は既存のまま。
既存の不変baseに限定するため、API・保存形式・永続化手順の変更を必要としない。

- 狙い: ヘッダーのページで待ち、次に値のページで待つ直列読込みを、レコード単位の要求で改善できるか確認する。
- 未確認: 1回の `pread()` が1回のデバイスI/Oになる保証はない。カーネルの先読みによる余分なI/Oもあり得る。
- コスト: 対象Getに `mincore()`、非常駐時にバッファへのコピーが加わる。常駐時やZipfで悪化する可能性もある。
- サイズヒントは上限寄りなので、ページに収まる一部のレコードも対象になる。ヘッダーを先に読んで厳密化すると、避けたいフォルトを先に起こしてしまう。

これは小さい変更で可否を判断するための第一候補であり、大幅改善を約束する案ではない。

## 第二候補: レコードをページ内に収める配置

確認したABIでは、ヘッダー24B + キー16B + 値1024B = **1064B**。
8B整列で連続配置すると、オフセットの512レコード周期中132件（**25.78%**）が4KBページをまたぐ。
これは配置の計算であり、実測のmajor fault発生率や待ち時間の割合ではない。
8B値はT1インラインなので、この計算はT2に置く1KBレコードが対象。
実際の追記失敗等による隙間や更新後の配置はモデル化していない。

ページ末尾に余白を入れれば境界またぎをなくせるが、1ページには3レコードしか入らず904B余る。
連続配置に比べT2の必要領域は **約28.32%増**。
同じRAMで保持できるレコード数が減るため、追加フォルト削減とキャッシュ効率低下の両方を測る必要がある。
割当て、checkpoint/recovery、defragmentの走査・生存範囲管理も確認対象になる。
各レコードを4KB整列する案はさらに容量効率が悪く、優先しない。

## 大きい変更: 複数GetのI/Oを並行発行する

MultiGetまたは非同期Getで複数キーを受け取り、T1から位置を決め、`io_uring`等で読込みを並行発行する。
独立した要求の待ちを重ね、特にUniformでスループットを上げる方向。
デバイス帯域・IOPS上限は既存調査では未確認なので、改善幅は未定。

現行ベンチマークの1スレッドは、Getの完了後に次のGetを発行する。
このまま1件ずつsubmit/waitしても、要求間の並列性は生まれない。
バッチ化はAPI・ワークロード条件が変わるため、既存の単件Get改善率とは分けて評価する。
複数キーにまたがるスナップショット保証の有無、完了までのバッファ寿命も定義する必要がある。

Zipf向けの値単位キャッシュも候補だが、既存ページキャッシュとの二重保持でRAMを消費する。
このベンチはキー順にデータを作り、Zipfもキー番号に直接適用するため、人気キーが物理的にも近い。
値単位キャッシュがOSのページキャッシュより有利とはまだ言えず、第一候補にはしない。
また値は疑似乱数バイト列なので、通常の圧縮による大幅改善を前提にはしない。

## 次に比較する条件

第一候補と現行版を、既存の8,259,552キー、16Bキー、80% 1024B / 20% 8B、
`MemoryHigh=1GiB / MemoryMax=2GiB`、GCC 13.4 / Releaseで比較する。
Uniform・Zipf、1・16スレッドを対象とし、各構成を独立したscopeで実行する。
同じデータ内容・初期キャッシュ条件・初期化を除いた計測区間を使い、順序を入れ替えて反復する。
既存の対照用メモリ条件で常駐時の退行も比較する。

主指標はops/sとデバイス読込B/Get。CPU時間、major fault/Get、メモリ・Swapは補助指標。
pread経路を採用する場合、major faultだけで優劣を判断しない。
実装時の正しさ確認はページ境界前後・base/tail・checkpoint後の更新値を対象に絞る。

## 今回の確認記録

- 対象: `e75746c2d03e0d53edaf566c157aad95172b887c` と既存の未コミット作業。
  今回、製品コードと既存ベンチマークには変更を加えていない。
- 参照: `src/vmemkv/read_path.hpp`、`src/t2_flat_file/t2_flat_file.{hpp,cpp}`、
  `src/vmemkv/write_path.hpp`、`src/vmemkv_impl.hpp`、`benchmark/bench_kv.cpp`、
  `docs/specification/{high_level_design,low_level_design}.md`。
- ABI・配置のみGCC 13で小さな計算プログラムを実行。DBを開かず、I/O性能は測定していない。
  コンパイルは `g++-13 -std=c++23 -O2 -Iimplementation/vmemkv/src -Iimplementation/vmemkv/include`。
  `t2_flat_file/t2_flat_file.hpp` をincludeし、実際の `sizeof(ValueRecordHeader)` と
  `record_aligned_len(sizeof(ValueRecordHeader), 16, 1024)` を使用した。
  `i=0..511` について `(i*1064)%4096 + 1064 > 4096` を数えた結果が132件。
  サイズヒント1072Bで同じ判定をすると133件。容量増は `4096/(3*1064)-1` で計算した。
- syscallの参照: [pread(2)](https://man7.org/linux/man-pages/man2/pread.2.html)、
  [madvise(2)](https://man7.org/linux/man-pages/man2/madvise.2.html)、
  [io_uring_enter(2)](https://man7.org/linux/man-pages/man2/io_uring_enter.2.html)。
