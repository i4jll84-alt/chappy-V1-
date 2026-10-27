# 起動中ExcelのRSSセル読取（2026-10-23）

## 1. 工程名

RSS読取専用ライブ観測の初期実装。起動中Excelの専用ブック3セルをPythonから読み取る。
証券会社への発注・注文有効化・再接続・マクロ・セル変更・再計算は行わない。

## 2. 使用モデル・推論設定

実行モデル・推論設定は独立確認できない。別モデルによる重要レビューは未実施。
変更前の復元基点は `cd76a5f`。

## 3. 実装した機能

Windows PowerShellの短命な補助プロセスで、登録済みのExcelインスタンスに接続する。
指定された絶対パスのブックを探し、Sheet1!A1:A3の数式・値を各2回読む。
Excelやブックを新規起動しない。Python側のタイムアウトで補助プロセスを終了するが、
Excel自体は終了しない。数式・銘柄不一致、価格・時刻・日付異常、読取途中の値変更を検査する。

観測回数・待機・タイムアウトを制限する。既定は3回、各読取後2秒待機。
各観測のRawと検査結果、開始・完了時刻を `data/rss-live/<run-id>` にJSONで保持する。
実データはGit対象外。出力に他のブック名・パス・例外本文を含めない。
異常値は補完しない。同じ値の観測も残し、値の変化を訂正や新しい約定とは認定しない。

`excel_read_verified` はセル読取の確認であり、`rss_connection_status` は常に `unknown`、
`freshness_verified` はfalse。`quote_date`、`published_at`、`source_updated_at` はnull。
「現在日付」と時刻を結合した約定日時は作らない。取得完了前のPIT根拠に使用しない。
取引判断・学習・バックテストへの接続はない。

通常は値不変を注意としない。明示的に `--expect-updates` を指定した場合だけ、
`--unchanged-after` 秒以上の値不変を注意にする。無約定でも値は変わらないため切断と断定しない。
読取失敗時は値不変の観測区間をリセットする。実行間で区間を引き継がない。

公式仕様:
[GetActiveObject](https://learn.microsoft.com/en-us/dotnet/api/system.runtime.interopservices.marshal.getactiveobject?view=netframework-4.8.1)、
[Workbook.FullName](https://learn.microsoft.com/en-us/office/vba/api/excel.workbook.fullname)、
[Application.Ready](https://learn.microsoft.com/en-us/office/vba/api/excel.application.ready)、
[Range.Formula2](https://learn.microsoft.com/en-us/office/vba/api/excel.range.formula2)、
[Range.Value2](https://learn.microsoft.com/en-us/office/vba/api/excel.range.value2)、
[RSS取得項目](https://marketspeed.jp/ms2_rss/onlinehelp/ohm_002/ohm_002_07.html)。

## 4. 作成・変更ファイル

- `scripts/read_rss_excel.ps1`：Excel読取補助プロセス。
- `src/trading_system/data_sources/rss_live.py`：検査・有限回観測・記録・CLI。
- `tests/test_rss_live.py`：合成入力・補助プロセス応答の検証。
- `README.md`、`AGENTS.md`、`docs/rss-readiness.md`、本書：手順・最新状況。

既存の `docs/coverage-stage-report.md` の未コミット変更は今回の対象外。

## 5. 実行したテスト

単体テストで異常価格、時刻・日付、銘柄・数式不一致、値変更、値不変の明示判定、
失敗時の観測区間リセット、回数制限、時計逆行、補助プロセスのタイムアウト・異常応答、
エラー情報の非公開、入力不変を検証した。障害系は合成入力・モック応答である。

実機では初回に `excel_not_registered` を検出した。ユーザーがブックを開いた後、
2026-10-23 18:31 JST頃に3回の読取すべてが `observed`、注意0件となった。
観測間の値変化は0件。実行中Excelの読取成功を確認した。
実データの詳細はGit対象外の `data/rss-live/b2ff58475c194852a8ff7d77f32ff863` に保持する。

## 6. テスト成功数・失敗数

新規単体テスト34成功。全体は819成功、失敗0、既存の実APIオプトイン検証5スキップ
（54.05秒）。通常ユーザーの未使用の短い一時フォルダーを `--basetemp` に指定した。
直前の全体実行はセッションIDを再取得できず最終結果を確認できなかったため、再実行した。
再実行ログはGit対象外の `data/rss-live-tests.log`。Ruff check成功、format --checkは113ファイル適合。

## 7. 発見した問題

ExcelがCOMに登録されていない場合は取得できない。これはExcelが閉じていると断定する根拠ではない。
取得できない状態を明示的に記録し、自動でアプリを起動・再接続しない。

## 8. 未確認事項

RSS提供元との現在の接続、取引時間中の更新と鮮度、実際の通信切断、複数銘柄、
実口座照合、実データのペーパートレードは未検証。取引時間外の値不変を切断試験と扱わない。
RSS専用DuckDB/Parquet保存は未実装であり、JSON観測はDBトランザクションの代替ではない。

同日の後続工程で保存済み観測のDuckDB/Parquet取り込みを実装した。
最新の保存範囲と検証結果は[RSS保存工程報告](rss-storage-report.md)を参照する。

## 9. 既知のリスク

複数Excelインスタンスでは取得したインスタンスに目的ブックがない場合がある。
この場合のエラーはExcel全体でのブック不在を意味しない。2回同値でも価格と時刻の
原子的な読取は保証しない。Excelの応答待ち・編集中などは取得失敗となり得る。
Windows PowerShellとリポジトリ内の補助スクリプトが必要で、単体wheel配布には対応していない。
常駐監視・自動復旧機能はなく、途中失敗時の観測JSONは残るが集計が未作成の場合がある。

## 10. 次工程

観測Rawを保持したRSS専用DuckDB/Parquet取り込みを設計する。その後、取引時間中に
更新の実測と障害時の記録を検証する。日時不明・未検証データを売買判断へ渡さない。

## 11. 次工程の推奨モデル

ユーザー指定に従い、設計・重要レビューはGPT-6 Astra / High、通常実装は
GPT-6.1 Sol / Medium〜Highを推奨する。

## 12. ユーザー操作

今回の読取実機検証は完了し、追加操作は不要。次回の取引時間中の実機検証では、
Excelの専用ブックとマーケットスピード IIを起動・ログイン済みにする必要がある。
実注文は対象外で、発注不可の状態を維持する。
