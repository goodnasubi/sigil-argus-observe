# sigil-argus-observe

Sigil サービス向けのログ・メトリクスエミッターです。[ADR 0067](https://github.com/goodnasubi/sigil/blob/60cc4f9acf2e7c7a69cec1c60af5e2e4515ac71a/docs/decisions/0067-argus-observe.md) の決定 2・3・7 に基づきます。

## 利用方法

`argus_observe.sigil` を利用側の Sigil プロジェクトに置き、`argus_observe` モジュールから必要な関数と型を取り込みます。

- `log_line(&ts, &component, Level.Info, &msg, &context)` がログを生成します。任意項目は `log_context(trace_id, span_id, target, err, fields)` に `Some` で渡します。時刻は呼び出し側で取得し、ミリ秒またはマイクロ秒精度の UTC `Z` 形式で渡します。
- 1 ログ行は末尾の LF を含めて最大 65,536 バイトです。本文内の CR と LF は空白に置き換えます。上限を超える本文は JSON エスケープ後のバイト数に合わせて UTF-8 文字境界で切り詰め、`"truncated":true` を付けます。本文を空にしても必要なフィールドと切り詰めマーカーが収まらない場合は `LogTooLarge` を返します。
- `metric(&component, &name, kind, &labels)` がゼロから始まる系列を作成します。`record(value)` にカウンターの加算量、ゲージの現在値、またはヒストグラムの観測値を渡します。`snapshot(&ts)` が同一時刻のサンプル行を返します。ヒストグラムは昇順の有限境界を受け取り、`_count`、`_sum`、累積 `_bucket{le="…"}`、および `le="+Inf"` を出力します。
- ラベルの `component` は出力元で上書きされます。監視対象は `target_component` に指定します。出力時の上限は 10 個で、利用側のラベルは最大 9 個です。ヒストグラムでは `le` 用の枠を確保するため最大 8 個です。系列に固定の低カーディナリティ値を指定し、ユーザー ID、生 URL、時刻などはラベルとして使用しないでください。メトリクス名には量に応じて `_total`、`_bytes`、`_seconds` などの単位を付けます。
- ログとメトリクスの行は `emit_stdout(&line)` で標準出力へ、`buffer()` で作成したバッファの `append(&line)` でメモリへ出力できます。`drain()` でバッファの内容を取り出して空にできます。標準出力は Sigil の `console.log` を使います。複数の実行スレッドで標準出力を共有する場合、呼び出し側で書き込みを直列化してください。

## 検証

Linux x86-64 で Sigil の `60cc4f9acf2e7c7a69cec1c60af5e2e4515ac71a` を使用します。Sigil のチェックアウトで `cargo build --locked --bin sigil` を実行し、このリポジトリで次を実行します。

```sh
SIGIL=/path/to/sigil/target/debug/sigil python3 tests/check_emitter.py
```

Sigil の評価器とネイティブバイナリで契約テストを実行し、両者の出力がバイト単位で一致することを確認します。Python の JSON パーサーで出力を個別に検証します。
