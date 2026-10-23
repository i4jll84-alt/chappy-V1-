# Japan Stock Multi-Strategy Trading System

日本株自動売買システムの第1段階：J-Quants V2のデータ取得・保存・品質管理。
**実APIでの日次株価取得・保存・品質検査・再取得検証まで成功しました。第1段階のデータ拡充を継続します。**

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

初回取得前の訂正履歴や当時の提供日時は再現できません。取得済み情報の利用可能日時は
取得完了の1秒後に限定します。営業日の欠落検知、マスター照合、財務/TOPIX等の取得は今後の拡張です。
契約終了時はRaw・DB・Parquet・同期コピーを含む廃棄が必要になるため、保管場所をまとめています。
