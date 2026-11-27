# sigil-argus-observe

Sigil サービス向けのログ・メトリクス収集と死活監視のライブラリです。[ADR 0067](https://github.com/goodnasubi/sigil/blob/60cc4f9acf2e7c7a69cec1c60af5e2e4515ac71a/docs/decisions/0067-argus-observe.md) の決定 2〜5・7 に基づきます。

## 利用方法

`argus_observe.sigil` を利用側の Sigil プロジェクトに置き、`argus_observe` モジュールから必要な関数と型を取り込みます。

- `log_line(&ts, &component, Level.Info, &msg, &context)` がログを生成します。任意項目は `log_context(trace_id, span_id, target, err, fields)` に `Some` で渡します。時刻は呼び出し側で取得し、ミリ秒またはマイクロ秒精度の UTC `Z` 形式で渡します。
- 1 ログ行は末尾の LF を含めて最大 65,536 バイトです。本文内の CR と LF は空白に置き換えます。上限を超える本文は JSON エスケープ後のバイト数に合わせて UTF-8 文字境界で切り詰め、`"truncated":true` を付けます。本文を空にしても必要なフィールドと切り詰めマーカーが収まらない場合は `LogTooLarge` を返します。
- `metric(&component, &name, kind, &labels)` がゼロから始まる系列を作成します。`record(value)` にカウンターの加算量、ゲージの現在値、またはヒストグラムの観測値を渡します。`snapshot(&ts)` が同一時刻のサンプル行を返します。ヒストグラムは昇順の有限境界を受け取り、`_count`、`_sum`、累積 `_bucket{le="…"}`、および `le="+Inf"` を出力します。
- ログとメトリクスの出力元 `component` は、ADR 0067 の決定 2・3 に従い `[a-z]+(-[a-z]+)*` の形式で指定します。数字を含む名前（例 `sigil-os2`）は `InvalidComponent` になります。数字入りの正式な部品名が必要になった時点で、ADR の命名規則と検証を合わせて見直します。
- ラベルの `component` は出力元で上書きされます。監視対象は `target_component` に指定します。出力時の上限は 10 個で、利用側のラベルは最大 9 個です。ヒストグラムでは `le` 用の枠を確保するため最大 8 個です。系列に固定の低カーディナリティ値を指定し、ユーザー ID、生 URL、時刻などはラベルとして使用しないでください。メトリクス名には量に応じて `_total`、`_bytes`、`_seconds` などの単位を付けます。
- ログとメトリクスの行は `emit_stdout(&line)` で標準出力へ、`buffer()` で作成したバッファの `append(&line)` でメモリへ出力できます。`drain()` でバッファの内容を取り出して空にできます。標準出力は Sigil の `console.log` を使います。複数の実行スレッドで標準出力を共有する場合、呼び出し側で書き込みを直列化してください。

## 収集・永続化

ビルドルートに `sigil-db` の [固定リビジョン](https://github.com/goodnasubi/sigil-db/tree/9483e36f08c35bfc62b3beeb008a531887506037) の `db/` と、`argus_observe.sigil`、`argus_collect.sigil` を配置します。`db.database.open_database` で開いたハンドルに `argus_collect.initialize(&mut database)` を呼び出します。

- `batch(max_bytes)` でメモリ上のバイト上限を指定し、`append(&line)` で第1段の `Line` を追加します。`flush(&mut database)` は同じバッチのログとメトリクスを単一の WAL トランザクションで確定し、確定した件数を返します。長い行も元の NDJSON バイト列のまま分割して保存します。
- ログは追記 ID を持ち、時刻・部品名の SHA-256・水準で検索できる索引を持ちます。メトリクスの一意キーは部品名・名前・時刻・正規化済みラベル集合の SHA-256 に基づきます。ラベル順や時刻のミリ秒／マイクロ秒表記が違っても、同じ標本のキーは一致します。
- 既存のキーと重複するメトリクスを含むバッチは全体をロールバックし、バッファに保持します。`clear()` で明示的に破棄できます。`CommitUnknown` の場合は同じバッチの再送が停止されるため、DB を開き直してコミット結果を確認してから処理を再開します。WAL のチェックポイントには `db.database.checkpoint_database` を使用します。

## 死活監視

`argus_health.sigil` の監視インスタンスを対象ごとに用意し、同じ単調時計の非負ミリ秒を渡します。

- `default_pull()` で初回は即時、以降は開始時刻から 10 秒間隔です。`poll(&Probe.Http(host, port), now_ms)` は平文 HTTP/1.x の `GET /healthz` に対する 2xx を成功とします。`Probe.Tcp(host, port)` は接続成功で判定します。`host` は数値 IPv4 または IPv6 アドレスです。既定の 3 秒は接続・送信・応答に共通の期限です。連続失敗中は `Degraded`、3 回目で `Dead`（ADR のプル型異常判定）、成功時に `Healthy` に復旧します。
- `default_heartbeat(now_ms)` で受信猶予を開始します。ハートビート受信時に `ping(now_ms)` を呼び、`status(now_ms)` で状態を取得します。最後の受信から 30 秒で `Dead` になり、次の受信で `Healthy` に復旧します。

## 検証

Linux x86-64 で Sigil の `60cc4f9acf2e7c7a69cec1c60af5e2e4515ac71a` を使用します。Sigil のチェックアウトで `cargo build --locked --bin sigil` を実行し、このリポジトリで次を実行します。

```sh
SIGIL=/path/to/sigil/target/debug/sigil python3 tests/check_emitter.py
```

Sigil の評価器とネイティブバイナリで契約テストを実行し、両者の出力がバイト単位で一致することを確認します。Python の JSON パーサーで出力を個別に検証します。

第2段の検証では上記の `sigil` と、固定した `sigil-db` のチェックアウトを指定します。

```sh
git clone https://github.com/goodnasubi/sigil-db.git /path/to/sigil-db
git -C /path/to/sigil-db checkout 9483e36f08c35bfc62b3beeb008a531887506037
SIGIL=/path/to/sigil/target/debug/sigil SIGIL_DB=/path/to/sigil-db python3 tests/check_stage2.py
```

モック時計で期限の境界と復旧を、ループバックで HTTP/TCP の接続と応答タイムアウトを確認します。保存テストでは混在バッチのロールバックと WAL からの再オープン、最大サイズのログの復元を確認します。
