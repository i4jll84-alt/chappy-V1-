# Japan Stock Multi-Strategy Trading System

日本株自動売買システムの第1段階：J-Quants V2のデータ取得・保存・品質管理。
**実APIでの日次株価取得・保存・品質検査・再取得検証まで成功しました。第1段階のデータ拡充を継続します。**

現在はデータ取得とペーパートレードの段階です。実発注・実注文の訂正・取消は禁止です。
[最新の開発状況と引き継ぎ情報](docs/current-context.md)を先に確認してください。
GitHub保存先: https://github.com/i4jll84-alt/chappy-V1- 。取得データ・認証情報・台帳・ログは含みません。

## 準備

Windows / Python 3.12。プロジェクト専用 `.venv` を使用します。
すでに初期導入済みですが、再構築する場合は以下を実行します。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

設定は `config/stage1.toml`。契約プランはユーザー申告のStandardです。
APIキーはコードや `.env` に書かず、Windowsユーザー環境変数 `JQUANTS_API_KEY` に登録します。
次のスクリプトはローカルで入力を隠して登録し、キーを画面・ファイルに出力しません。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\set-api-key.ps1
.\.venv\Scripts\python.exe -m trading_system.cli doctor
```

doctorの `api_key_present: true` を確認してください。登録済みキーはアプリ再起動なしで読み取れます。
APIキーをチャットに貼る必要はありません。`.env.example` は説明用で、自動読込みしません。

## 1銘柄を取得

86970は接続確認用の例であり売買推奨ではありません。対象期間は明示指定します。

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli fetch-prices --code 86970 --from 2026-10-01 --to 2026-10-22
.\.venv\Scripts\python.exe -m trading_system.cli status
```

同じコマンドで途中再開できます。完了期間を改めて取得する場合は `--refresh` を追加します。
差分更新は `--incremental --refresh` を追加し、`--from` に最初の下限、`--to` に更新先日付を指定します。
末尾7暦日を重ねるため訂正を一部拾えますが、全訂正の保証には全期間のrefreshが必要です。

データは `data/jquants/` に保存されます。

- `raw/`：元のJSON本文。全応答を変更せず保存。
- `catalog.duckdb`：正規化データ、版履歴、品質結果、取得履歴。
- `normalized/latest.json`：最新の検証済みParquetファイル一覧。
- `logs/`：取得結果のreceipt。APIキーを含まない。
- `processed/`：将来の分析用。現段階では空。

Parquetを再出力する場合は `python -m trading_system.cli export`（専用環境のPython）を使用します。
世代フォルダーは履歴なので一括glob読込みせずlatest.jsonに記載されたファイルを使います。
品質エラーは自動修正せず保存し、取得結果をquality_failedにします。終了コードは0=成功、
2=設定/接続/処理失敗、3=空応答または品質異常です。

## 検証

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

実APIテストを明示的に有効にすると、実取得→Parquet→DuckDB→再取得の件数不変まで確認します。

```powershell
$env:JQUANTS_LIVE_TEST = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_live.py -q
Remove-Item Env:JQUANTS_LIVE_TEST
```

接続先/期間は `JQUANTS_TEST_CODE`、`JQUANTS_TEST_FROM`、`JQUANTS_TEST_TO` で変更できます。
実データはテスト用の一時領域に置かず、上記プロバイダー保存先に保持します。
モック成功だけでは第1段階の完了とみなしません。

## 設計・制限・進捗

- [基本仕様](docs/development-instructions.md)
- [設計と既知の制限](docs/architecture.md)
- [確認した公式仕様とデータ対応一覧](docs/official-specs.md)
- [今回の作業報告](docs/stage1-report.md)

## 銘柄マスターと取引カレンダー

第1段階の追加機能として、基準日を指定した銘柄一覧と、全暦日を含むカレンダーを取得します。

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli fetch-master --date 2026-10-22
.\.venv\Scripts\python.exe -m trading_system.cli fetch-calendar --from 2026-10-01 --to 2026-10-22
```

masterは `--code 86970` で単一銘柄に絞れます。完了済みの条件を再確認する場合は
`--refresh` を追加します。保存先は `data/jquants/reference/master/` と
`data/jquants/reference/calendar/` で、それぞれRaw、DuckDB、Parquet世代を保存します。
既存の日次株価のDBは従来の場所です。

休日を指定したmasterは翌営業日の情報を返す場合があります。要求日と実際の適用日を混同せず、
日付差を記録します。カレンダー区分3は大阪取引所の祝日取引で、東証個別株の営業日ではありません。
現在の銘柄一覧を過去の日付に流用しません。ETF/REITを含む元マスターは保持し、
後続の対象選定で区別します。商品区分だけで普通株と優先株を完全に区別することはできません。

初回取得前の訂正履歴や当時の提供日時は再現できません。取得済み情報の利用可能日時は
取得完了の1秒後に限定します。営業日の欠落検知、マスター照合、財務/TOPIX等の取得は今後の拡張です。
契約終了時はRaw・DB・Parquet・同期コピーを含む廃棄が必要になるため、保管場所をまとめています。


## 証券会社3社共通基盤（Phase 1・ペーパー専用）

楽天RSS・立花e支店API・三菱UFJ eスマートkabuステーションAPI向けの共通契約を追加しました。
実接続アダプターは未実装で、資格情報なしで動くペーパーアダプターのみ利用できます。
初期設定は楽天を選択し、他2社は無効です。実注文有効化の設定は拒否します。

```powershell
.\.venv\Scripts\python.exe -m trading_system.brokers doctor
.\.venv\Scripts\python.exe -m trading_system.brokers demo --state-root data/broker-phase1-demo
```

これは固定日時・架空銘柄の契約試験です。既存のRSS常時取得・仮想取引は別経路です。
[設計・対応表・設定例・検証結果・未実装事項](docs/brokers-phase1.md)を参照してください。
既存のCLI・データ取得先登録漏れも修正し、オフライン全体テスト990件が成功しました。
RSS終了時の件数上限は2026-10-27に解消し、全件照合へ変更しました。

### 16時停止と次回起動時の後処理再開（段階A）

模擬口座・仮想時計での16時停止、永続ジョブの再開、公開待ちの持越しを追加しました。
操作例・受入テスト・未接続箇所は [運用手順](docs/lifecycle-stage-a.md) を参照してください。
**RSS取得プログラムは日本時間16時に新しい読取を止め、保存・照合後に自動終了します。**
16時以降の起動は保存済みRawのオフライン復旧だけを実行します。
[RSSの16時停止・復旧手順と検証](docs/rss-1600-stop.md)を参照してください。
実口座操作、OS常駐登録、モデル再学習は行いません。
