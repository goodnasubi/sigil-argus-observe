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

## 受信口とクエリ API（第3段）

`argus_receive.sigil` が 1 つの待ち受けポートでログ・メトリクスを受信し、`sigil-orchestra` のダッシュボードへデータを提供します。既定は `127.0.0.1:7490` です。受け取った行は `argus_observe.accept_line` で検証・正規化（component の正規化、ラベル数上限 10 個、1 行 64 KB 上限）してから、第2段の保存コアに書き込みます。

`argus_receive.default_config(db_path)`、`open_hub`、`listen_on`、`share_hub`、`serve` で起動します。データベースは `sigil-db` の `db_path` です。接続数・期限・本文・要求数の上限は `with_limits`、クエリの大きさは `with_query_limits` で設定します。

- **HTTP/1.1**（先頭が `{` 以外）: `POST /ingest` は NDJSON を受け取り `{"accepted":N,"rejected":M,"reasons":[…]}` を返します。無効な行は理由付きで数え、有効な行は保存します。同じメトリクス標本の再送は `rejected` に数えます。`GET /healthz`。
- **生 TCP**（先頭が `{`）: 1 行 1 レコードを送ると、1 行ごとに `OK` または `ERR <理由>` が返ります。64 KB を超える行は `ERR line_too_large` の後に切断します。
- **クエリ API**（`GET`、JSON）:
  - `/api/logs?component=&level=&since=&until=&prefix=&limit=` は直近のログを新しい順に返します。`prefix` は `msg` の前方一致です。
  - `/api/metrics?component=&name=&since=&until=` は件数・合計・最小・最大・最新値を返します。`complete` が `false` なら走査上限で打ち切っています。
  - `/api/components` は `heartbeat_ping` を送ってきた部品の状態（`healthy` / `degraded` / `dead`）を返します。30 秒受信が途絶えると `dead` です。

## データ保持ローテーション・流量制御・負荷境界（第4段）

ADR 0067 決定 7 に基づき、ディスク容量上限（既定 10 GB）および保持期間（既定 7 日）の FIFO ローテーション・削除、高頻度ログ流入時の流量制御（レートリミット）、負荷境界の防御挙動を提供します。

- **保持期間（retention）と容量上限（max_disk_bytes）**:
  - `with_retention(config, retention_ms, max_disk_bytes)` で設定します。既定は保持期間 7 日（`604,800,000` ms）、容量上限 10 GB（`10,737,418,240` バイト）です。
  - 上限超過時は古いログから FIFO で自動破棄（`prune_oldest_logs`）し、稼働継続を最優先します（ADR 0067「適合の限界: データ破棄の許容」）。
  - 保持期限切れのログは `prune_expired_logs` によりタイムスタンプ基準で自動削除されます。
- **流量制御（レートリミット）**:
  - `with_rate_limit(config, max_rate)` で 1 秒あたりの最大受信レコード数を設定します（既定 10,000 件/秒）。
  - 超過時、HTTP は `429 Too Many Requests`（`{"error":"rate_limited"}`）、生 TCP は `ERR rate_limited\n` を即座に返し、次の一秒窓で回復します。
- **負荷境界**:
  - `scan_limit` 到達時の打ち切り（`/api/metrics` で `complete: false`、`scanned: N`）。
  - `query_max` による上限クランプ（`/api/logs` の `limit` 制限）。
  - 要求本文上限超過（413 Payload Too Large）、要求ヘッダー上限超過（431 Request Header Fields Too Large）、生 TCP の 1 行 64 KB 上限超過（`ERR line_too_large` 切断）。

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

第3段の検証では、第2段と同じ `SIGIL` と `SIGIL_DB` を指定します。

```sh
SIGIL=/path/to/sigil/target/debug/sigil SIGIL_DB=/path/to/sigil-db python3 tests/check_stage3.py
```

`argus_accept_test.sigil` は受信行の検証を、`argus_receive_test.sigil` はループバックでサーバーとクライアントを同一プログラムで動かし、HTTP と生 TCP の取り込み、3 種のクエリ、heartbeat の状態遷移、不正・過大・低速・過剰接続・高頻度の各境界を評価器とネイティブの両方で確認します。評価器ではコミットが遅いため、評価器の実行に数分かかります。

第4段の検証では、第3段と同じ `SIGIL` と `SIGIL_DB` を指定します。

```sh
SIGIL=/path/to/sigil/target/debug/sigil SIGIL_DB=/path/to/sigil-db python3 tests/check_stage4.py
```

`argus_stage4_test.sigil` は、容量超過時の FIFO ローテーション、保持期間超過ログの破棄、HTTP/生 TCP の流量制御（429 / ERR rate_limited）と回復、メトリクス走査上限（complete: false）、クエリ上限クランプ、413/431/64 KB 超過の各境界を評価器とネイティブの両方で確認します。

