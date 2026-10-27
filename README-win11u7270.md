# Japan Stock Multi-Strategy Trading System

日本株自動売買システムのデータ基盤・バックテスト・予測モデル研究。
**第1段階の最小データ基盤について、仕様5.8の完了条件を確認しました。**
実APIでの取得・保存・品質検査・再取得と、取得ジョブ完了根拠の保護を検証しています。
対象範囲・残るデータ拡充項目は[完了条件の確認報告](docs/stage1-readiness.md)を参照してください。
第2段階の初期バックテスト基盤は、明示した合成データで再現性・約定・会計を検証します。
第3段階の初期モデル研究では、合成データだけで特徴量・学習・期間分離・再現性を検証します。

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
完了済み条件のfetch-pricesを `--refresh` なしで再実行しても、HTTPを呼ばずDuckDBから
Parquetを再出力します。欠落ファイルや中断したlatest.jsonの復旧にも使用できます。
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
- [複数データソースのカバレッジ監査設計](docs/coverage-audit.md)
- [カバレッジ監査の作業報告](docs/coverage-stage-report.md)
- [確認した公式仕様とデータ対応一覧](docs/official-specs.md)
- [今回の作業報告](docs/stage1-report.md)
- [銘柄マスター・取引カレンダー追加の作業報告](docs/reference-stage-report.md)
- [株価PITとParquet復旧の作業報告](docs/price-pit-stage-report.md)
- [第1段階の最小データ基盤・完了条件の確認報告](docs/stage1-readiness.md)

株価の`Store.as_of`は、判断時点までに完了から1秒以上経過した最新の1検索を返します。
検索条件を指定すると完全一致する検索に限定します。複数検索を結合した全銘柄一覧ではありません。
返却行のsnapshot_job_idで出所を確認できます。最新の確定検索が空・品質異常なら空となり、
途中・失敗した再取得は以前の確定検索を置き換えません。最新試行の失敗を検知する用途には
カバレッジ監査を使います。保存済みの行単位available_atだけをPIT判定に使わず、
完了根拠と合わせる詳細を[設計](docs/architecture.md)で確認してください。

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
取得完了後に限定します。財務の公表日時と取得完了日時は区別して保存します。カレンダー自体の
暦日欠落を検査し、株価・TOPIXと営業日を照合する範囲は[カバレッジ監査設計](docs/coverage-audit.md)
を参照してください。上場履歴全体の網羅は未確認です。
[追加設計](docs/reference-data.md)と[TOPIX・財務概要設計](docs/market-financial-data.md)を参照してください。

## TOPIX・財務概要

第1段階の追加データとして、TOPIX日次OHLCとJ-Quants財務・決算概要を取得します。
TOPIXは期間を明示し、財務概要は `--code`、`--date` の少なくとも一方を指定します。
再取得する場合は `--refresh` を追加します。

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli fetch-topix --from 2026-10-01 --to 2026-10-22
.\.venv\Scripts\python.exe -m trading_system.cli fetch-financials --code 86970
.\.venv\Scripts\python.exe -m trading_system.cli fetch-financials --date 2026-10-22
```

保存先・自然キー・品質検査・時点管理、実接続の検証状況は
[TOPIX・財務概要設計](docs/market-financial-data.md)と[作業報告](docs/market-financial-stage-report.md)を参照してください。
TOPIXでは休場日の行を生成せず、財務の空欄はゼロに置き換えません。株価・TOPIX・
取引カレンダー間の期間カバレッジ確認は[監査設計](docs/coverage-audit.md)と
[作業報告](docs/coverage-stage-report.md)を参照してください。実APIでの取得成功と
モック検証は区別して報告します。異常終了後はDuckDBを確定状態の基準とし、同じ条件で再実行して
Parquetを再出力してから利用してください。`latest.json` はPoint-in-Timeの証明ではありません。
契約終了時はRaw・DB・Parquet・同期コピーを含む廃棄が必要になるため、保管場所をまとめています。

## 合成データのバックテスト基準実験

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli backtest-benchmark --output data/backtests/synthetic-1.json
.\.venv\Scripts\python.exe -m trading_system.cli backtest-benchmark --output data/backtests/synthetic-2.json
```

同じ入力の`input_hash`と`result_hash`は一致します。出力は入力シナリオ全体、仮定、注文、
約定、現金台帳、保有、資産曲線、指標を含みます。価格と制度・費用は説明用の合成値です。
基準戦略は既知の終値2点を比べる固定数量のモメンタムで、注文は次のセッションの寄付で
評価します。約定不能や期間末の未決済保有も残します。実市場の投資成果を示すものではありません。

独自の合成入力は`--scenario scenario.json`で指定します。保存した結果の`scenario`項目を
独立したJSONファイルへ取り出すと、同じ入力として再実行できます。未知のルール・在庫・費用を
推定する既定値はなく、必要な項目の欠落や不正な日時は拒否します。APIキーや取得用設定は不要です。
`config/stage1.toml`は引き続きデータ取得用で、バックテストコマンドの設定とは分かれています。

実株価のPIT事前検査はPython APIの`backtest.pit.load_price_snapshot`と
`validate_real_pit`で提供します。初回取得以前の判断時点は受け入れず、価格の検査が通っても
過去の投資対象・制度・権利・信用売り在庫の根拠が揃わないため実データの売買実験は有効になりません。
詳細は[バックテスト設計](docs/backtest-design.md)、[公式資料](docs/backtest-official-sources.md)、
[作業報告](docs/stage2-report.md)を参照してください。このバックテストコマンドは証券会社への発注や
モデル学習を行いません。

## 合成データのモデル研究

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli model-benchmark --registry-root data/research --output data/research/model-result.json
```

合成した420セッションから、判断時刻までに既知の株価だけで特徴量を作り、5・10・20・60
セッション先のリターンを予測します。比較対象はゼロ予測、学習期間平均、Ridge、固定設定の
LightGBMです。訓練・検証・最終評価を時系列で分け、将来のラベルが境界を越える行を除外します。
未知の企業情報や現在の実市場データを補って学習する機能はありません。

候補の選択は検証期間だけで行い、最終評価の前に設定を固定します。候補の失敗・不採用と
学習履歴を保存し、最終評価は同一実験につき一度だけ消費します。同じコマンドの再実行は
保存済みの最終結果を読み、再学習・再評価せず同じハッシュを返します。中断・失敗した最終評価も
消費済みのままです。別の実験IDを作って同じ未使用期間を再利用してよいという意味ではありません。

出力には入力全体、特徴量・ラベルの来歴、全候補履歴、予測、JSON形式のモデル、費用控除後の
合成バックテスト比較を含みます。pickleは使いません。予測精度と売買損益は分けて記録し、
費用比較が完了しなければ`cost_evaluation_complete: false`と終了コード3を返します。
選択の改善幅は研究上の規則であり、統計的な有意性や実運用への採用を示しません。

独自入力は`--scenario scenario.json`、期間境界・候補設定は
`--research-config research-config.json`で指定します。対象は権利イベントのない合成シナリオに
限り、実市場での学習・投資有効性・発注は有効になりません。取得用設定やAPIキーも読みません。
モデル・履歴の保存方法と制限は[第3段階設計](docs/model-research-design.md)と
[作業報告](docs/stage3-report.md)を参照してください。

## 合成データの固定配分・リスク判定

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli portfolio-benchmark --output data/portfolio/plan-result.json
```

短期・スイング・中期の候補を固定資金枠で統合し、銘柄・業種の集中、保有・未約定予約、
現金、空売り担保・在庫、損失条件、銘柄間相関を検査します。必要な根拠がない場合や
上限超過は拒否理由を残します。予測の大きさや信頼度から数量を増やしません。

独自の合成入力は`--plan plan.json`で渡します。入力・出力ハッシュと候補の判定結果を保存し、
同じ入力から同じ結果を再現できます。APIキーや取得用設定は不要です。
終了コード0は判定処理の完了であり、全候補の受入を意味しません。個別拒否は`rejections`に
残ります。口座全体の判定停止は終了コード3、不正な入力形式は2です。
これは新規候補の計算であり、約定・決済・証券会社への発注を行うコマンドではありません。

第3段階の予測はPython APIの`portfolio.adapter.candidates_from_research`で接続できます。
口座・制度・相関は別に明示する必要があります。合成結果の再生に限定します。
詳細と検証結果は[設計](docs/portfolio-design.md)と[作業報告](docs/portfolio-stage-report.md)を参照してください。

## 複合戦略の合成時系列シミュレーション

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli portfolio-simulation --output data/portfolio/simulation-result.json
```

固定配分の候補を次の寄付へ送り、約定・保有・費用・決済を追跡します。
複合実行と、各戦略を元の資金枠のまま個別に動かした結果を保存します。
初期現金・制度・費用は全実行で同じです。個別実行の余った資金枠を再配分しません。

独自の合成入力は`--scenario scenario.json`で指定します。出力の`input`を取り出すと
同じ入力として再実行できます。結果には注文、台帳、保有、戦略別集計、リスク判定を含みます。
途中停止した実行を除外して比較せず、比較未完了と終了コード3を返します。
完了は処理の完走を意味し、全注文の約定や全保有の決済を意味しません。

実API・証券会社への接続は行いません。合成価格による会計と時点の検証です。
担保計算や初期対応範囲は[設計](docs/portfolio-simulation-design.md)、
実行結果は[作業報告](docs/portfolio-simulation-report.md)を参照してください。

## モック注文管理の障害試験

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli execution-mock --state-root data/execution-mock --output data/execution-mock/result.json
```

実発注しないローカル環境で、送信応答の喪失、再起動、部分約定後の取消、取消前の約定を
再現します。注文側とモック側の状態を別のSQLiteファイルに保存します。
同じ保存先での再実行は既存状態を読み、注文を再送しません。監査履歴は追記されるため、
全体の結果ハッシュは再実行で変わる場合があります。

デモは最後に新規注文の緊急停止を有効にします。実RSS・Excel・証券口座には接続せず、
APIキーも必要ありません。モックの価格と口座設定は固定の検証用です。
既存の保存先を消して再試行する必要はありません。独立した実験には別の`--state-root`を指定します。
設定が異なる状態の再利用は拒否します。

配分結果との接続、照合方法、制限は[設計](docs/mock-execution-design.md)、
検証結果は[作業報告](docs/mock-execution-report.md)を参照してください。

## RSSの導入状況を確認する

```powershell
powershell -NoProfile -File scripts/check_rss_environment.ps1
```

Excelの導入情報とRSSアドインの配布ファイルを読み取り、JSONで結果を返します。
アプリの起動・ブックの再計算・注文は行いません。ファイルが見つかっても実接続成功を
意味しません。公式仕様、診断の制限、次の確認事項は[RSS接続準備](docs/rss-readiness.md)を参照してください。

Excel未導入による保留後、Microsoft 365 x64とExcel本体の導入を確認しました。
RSSアドイン登録後、Excelの「接続中・発注不可」表示と、市況3項目の取得・保存を確認しました。
Pythonから起動中Excelの3セルを読み取る観測を確認しました。
RSS提供元との接続、取引時間中の自動更新、切断・再接続は未検証です。

専用の確認ブック（Sheet1のA1:現在値、A2:現在値詳細時刻、A3:現在日付）を読み取るには:

```powershell
.\.venv\Scripts\python.exe -m trading_system.data_sources.rss_snapshot --workbook "$env:USERPROFILE\Documents\RakutenRSS\RSS接続確認.xlsx" --code 7203.T
```

Excelを起動・再計算せず、保存されたRSS数式とキャッシュ値を検査します。
元ブックを `data/rss-snapshots/raw` に内容ハッシュ別で保持し、読取ごとの結果を
`data/rss-snapshots/observations` に保存します。同じ元ファイルは重複保存しません。
このハッシュはファイルの同一性であり、市場データの訂正判定ではありません。
終了コードは0=キャッシュ検査成功、3=品質異常または非対応ブック、2=ファイル操作等の失敗です。
検査成功はライブ接続・鮮度を保証しません。取得日付と更新時刻を結合した約定日時も生成しません。

## 起動中ExcelのRSSセルを読み取る

専用ブックをExcelで開いた状態で、リポジトリのルートから実行します。

```powershell
.\.venv\Scripts\python.exe -m trading_system.data_sources.rss_live --workbook "$env:USERPROFILE\Documents\RakutenRSS\RSS接続確認.xlsx" --code 7203.T --samples 3 --interval 2
```

既存Excelに接続してSheet1!A1:A3の数式と値を読み、各観測と集計を
Git対象外の `data/rss-live` に保存します。セル書込・再計算・マクロ・発注は行いません。
Windows PowerShellと、このリポジトリの `scripts/read_rss_excel.ps1` が必要です。
終了コードは0=観測値の検査成功、3=注意あり、2=実行失敗です。
Excel読取成功はRSS接続・鮮度の証明ではありません。通常は値不変を異常扱いしません。
`--expect-updates --unchanged-after 60` を明示した場合のみ、観測中の値不変を注意として記録します。
その場合も切断とは断定しません。実機結果と制限は[ライブ読取報告](docs/rss-live-report.md)を参照してください。

## RSS観測をDuckDB・Parquetに保存する

`rss_live` が出力した実行フォルダーを指定します（`<run-id>` は実際のIDに置換）。

```powershell
.\.venv\Scripts\python.exe -m trading_system.storage.rss --run data/rss-live/<run-id>
.\.venv\Scripts\python.exe -m trading_system.storage.rss --export-only
```

保存先は `data/rss-store`（`--root` で変更可能）。Rawを内容ハッシュ別に保持し、
観測と取込済み記録を同一トランザクションでDuckDBへ保存します。同一観測の再実行は重複しません。
同じ観測IDの内容変更は上書きせず停止します。別の観測は同値でも残します。
ParquetはDBから再生成でき、完成した世代を `parquet/current.json` が指します。
終了コードは0=完了、3=対象に注意・拒否記録あり、2=保存・整合性等の失敗です。
保存済みファイルだけを読み、Excel接続・発注は行いません。
詳細は[RSS保存工程報告](docs/rss-storage-report.md)を参照してください。

## RSS保存ストアの読取専用監視

```powershell
.\.venv\Scripts\python.exe -m trading_system.monitoring.rss --root data/rss-store
```

Rawのハッシュ、Rawから再検査した値とDB、取込済み記録と観測、公開ParquetとDBの一致を
確認します。未参照Raw・不完全な別世代も報告します。正常な過去世代は件数のみ表示します。
DBは読取専用で開き、修復・削除・再取得・Excel操作は行いません。
終了コードは0=保存整合性確認、3=注意あり、2=検査不能です。
整合性が一致してもRSS接続・鮮度・全観測の網羅性は保証しません。
詳細は[RSS監視工程報告](docs/rss-monitoring-report.md)を参照してください。

元の観測実行フォルダー全体との取り込み照合も行う場合:

```powershell
.\.venv\Scripts\python.exe -m trading_system.monitoring.rss --root data/rss-store --runs data/rss-live
```

`coverage` に未取り込み件数、内容変更件数、元ファイルのない取込済み件数、
完了記録と連番・集計の一致を返します。書込処理を止めてから実行してください。
`--runs` 省略時は元観測の網羅性を検査しません。
確認範囲は[取り込み照合報告](docs/rss-coverage-report.md)、
取引時間中の検証手順は[RSS実機検証手順](docs/rss-intraday-check.md)を参照してください。

## RSSの観測・保存・照合を一括実行する

```powershell
.\.venv\Scripts\python.exe -m trading_system.rss_workflow --workbook "$env:USERPROFILE\Documents\RakutenRSS\RSS接続確認.xlsx" --code 7203.T --samples 20 --interval 2
```

既存の読取専用観測、保存、整合性・取り込み照合を順番に実行します。回数を指定した有限実行です。
書込・発注・自動再接続は行いません。工程別の結果は `data/rss-workflows` に保持します。
観測は完了したが保存以降で失敗した場合は、実行IDを指定して再実行できます。

```powershell
.\.venv\Scripts\python.exe -m trading_system.rss_workflow --run data/rss-live/<run-id>
```

`--run` はExcelに接続せず、完了記録と連番を確認した保存済み観測を扱います。
注意を含む観測も保存して注意を返します。終了コードは0=処理完了、3=注意あり、2=失敗。
処理完了はRSS鮮度の保証ではありません。詳細は[一括実行報告](docs/rss-workflow-report.md)を参照してください。

## 保存済みモック状態の監視

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli monitor-mock --output data/monitoring/current.json
```

既存のモック注文DBから現金・保有・注文状態を読み、停止状態や古い根拠を報告します。
ExcelやAPIキーは不要です。注文を送らず、取消・復旧・停止解除も行いません。
デモは古い固定日時の根拠と緊急停止を保持するため、注意判定になるのが正常です。
終了コード0は読取検査の完了、3は注意項目あり、2は読取・入力エラーです。
実運用への準備完了を意味しません。詳細は[監視の範囲と手順](docs/monitoring-stage-report.md)を参照してください。

## 保存済み株価取得の監視

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli monitor-prices --code 86970 --from 2026-10-01 --to 2026-10-22 --max-age-hours 24 --output data/monitoring/prices-current.json
```

指定条件の最新取得ジョブ、品質情報、保存メタデータの整合性、取得完了からの経過時間を
確認します。最新取得が失敗していても、古い成功で正常判定しません。
24時間は検証例の条件です。取得完了の新しさと、必要な営業日・最新株価の網羅性は別です。
API再取得やDB更新は行いません。詳細は[株価取得監視](docs/price-monitoring-report.md)を参照してください。

## 保存済み合成モデル結果の監視

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli monitor-research --max-mse-ratio 1.2 --max-mean-shift 1 --output data/monitoring/research-current.json
```

保存済みの4予測期間について、基準モデルとの誤差比較と、学習時からの特徴量平均の変化を
確認します。しきい値は検証用の例です。既存の検証結果を読み、新たな学習・推論や
モデル差替えは行いません。実市場での劣化を認定するものではありません。
対象・時刻の制約・判定方法は[モデル結果監視](docs/research-monitoring-report.md)を参照してください。
