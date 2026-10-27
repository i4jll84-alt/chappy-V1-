# RSS取引時間中の有限回観測手順

本手順は未実施。現在の実機確認は取引時間外の3回読取・保存・照合まで。
実注文や発注有効化は対象外。観測・保存・監視の順に実行し、同時実行しない。

## 準備

マーケットスピード IIをログイン済みにし、専用Excelブックを開く。
Excelの「接続中・発注不可」を人が確認し、その確認時刻を記録する。
PythonはこのUI表示を取得しないため、画面確認とPythonの検証結果を混同しない。
専用ブックはSheet1のA1:A3に、同一銘柄の現在値・現在値詳細時刻・現在日付を設定済みのものを使う。

## 有限回観測

観測・保存・照合をまとめて実行する場合は次のコマンドを使える。
以下の個別コマンドは工程を分けて確認したい場合に使用する。

```powershell
.\.venv\Scripts\python.exe -m trading_system.rss_workflow --workbook "$env:USERPROFILE\Documents\RakutenRSS\RSS接続確認.xlsx" --code 7203.T --samples 20 --interval 2
```

工程別結果は `data/rss-workflows` に残る。観測済みの実行を再利用する場合は
`rss_workflow --run data/rss-live/<run-id>` を使用し、新規観測を重ねない。
この一括コマンドの実機検証は保存済みデータを使う経路のみで、取引時間中の経路は未検証。

リポジトリのルートで、対象銘柄の取引時間中に実行する。

```powershell
.\.venv\Scripts\python.exe -m trading_system.data_sources.rss_live --workbook "$env:USERPROFILE\Documents\RakutenRSS\RSS接続確認.xlsx" --code 7203.T --samples 20 --interval 2
```

20回の観測で終了する。観測間に2秒待つため、実際の間隔には読取時間も加わる。
`report_dir` の実行IDを記録する。Excelへの書込や再計算は行わない。
`changed_observations` が0でも、無約定・値不変の場合があるため切断と断定しない。
値変化があっても、それだけで提供元への接続や市場データの鮮度を証明しない。

終了コード3は注意あり。記録した観測を保持し、理由を確認する。
途中停止ではsummary.jsonがない場合がある。途中のJSONを削除したり、完了記録を手作業で作らない。

## 保存と照合

観測処理の終了後、`<run-id>` を出力された実行IDに置換して実行する。

```powershell
.\.venv\Scripts\python.exe -m trading_system.storage.rss --run data/rss-live/<run-id>
.\.venv\Scripts\python.exe -m trading_system.monitoring.rss --root data/rss-store --runs data/rss-live
```

正常終了した実行について、対象ファイル数・完了記録が一致し、未取り込みと内容変更が0であることを確認する。
過去の未完了実行も照合対象になる。注意を消すための削除・上書きはしない。
取込途中の失敗は原因確認後に同じ取込コマンドを再実行する。
Parquet出力だけの失敗は原因確認後に `storage.rss --export-only` で再生成できる。
監視コマンド自体は修復しない。

## 記録する結果

- 実行日時、実行ID、観測件数、正常読取数、注意数、値変更回数。
- UI接続表示を確認した時刻（未確認なら未確認）。
- 保存件数、元観測との一致件数、未取り込み・変更・元ファイル欠落の件数。
- 取引時間中の値変化を観測できたか。観測できなければ理由を推定しない。

約定日・提供元更新日時の不明は維持する。「現在日付」と時刻から補完しない。
保存ファイルの整合性成功を、売買システムの実運用準備完了と扱わない。
