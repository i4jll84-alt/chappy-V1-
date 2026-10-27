# 16時停止・次回起動時の後処理再開（段階A）

実装・確認日: 2026-10-26。作業場所:
`C:\Users\i4jll\Documents\ChatGPT\個別株自動売買ツールV1`

## 確認できた範囲

今回の成果物は **PaperBroker相当の既存PaperAdapter、固定応答、仮想時計を使う運用制御**。
実口座・J-Quantsへの通信、認証、発注・訂正・取消は実施していない。
Windows常駐登録、自動起動、PC電源操作、Excel・証券会社ソフトの終了操作も行っていない。

2026-10-27追記: RSS取得ループにも共通のAsia/Tokyo・16時期限を接続した。
RSSの停止・Raw復旧は [専用手順](rss-1600-stop.md) を参照。以下の日次ジョブ全体は
引き続き段階Aの模擬実装で、実データ収集や売買候補の作成まで接続したものではない。
モック口座の照合成功は、実口座の照合成功を意味しない。

## 変更ファイル

- `src/trading_system/runtime/models.py`: Asia/Tokyo、連続した明示カレンダー、段階A設定。
- `runtime/store.py`: SQLiteジョブ・試行・チェックポイント・出力・終了履歴。
- `runtime/handlers.py`: 固定応答取得、既存品質検査の再利用、内部モックの下流処理。
- `runtime/service.py`: 起動分類、終了準備、16時停止、復元、優先順位、操作ゲート。
- `runtime/__main__.py`, `runtime/__init__.py`: オフラインシナリオ実行入口。
- `brokers/router.py`, `brokers/paper/adapter.py`: 任意のライフサイクル操作ゲートを追加。
  既存利用者がゲートを接続しない場合の動作は維持。
- `tests/test_runtime_lifecycle.py`: 受入・障害注入・別プロセス排他テスト。
- `config/lifecycle-paper-scenario.json`: 固定時計・模擬カレンダー・固定応答の設定例。
- 本書、README、`data/lifecycle-verification.json`: 操作方法と検証記録。

変更前ソースは `data/development-backups/lifecycle-20261026-204631/before.zip`、
既存の追跡差分は同じ場所の `tracked.patch` に保存。既存の未コミット変更を保持した。

## 起動と停止の規則

実行権限と処理目的は別フィールド。今回の権限は常にpaperで、
read_only/liveは設定検証で拒否する。後処理への移行で権限を変更しない。

| 起動・実行状況 | 処理 |
|---|---|
| 営業日の寄り前 | 状態復元、模擬照合、当日の準備。市場時間・余力の既存ゲートは維持 |
| 営業日の場中 | 現在管理の照合を先に実行し、その後、上限付きデータ処理を1ステップ |
| 引け～16時 | 終了準備。新規注文やデータ処理を開始せず、照合・状態保存 |
| 日中起動のまま16時到達 | 再構成、注文・予約・照合結果・終了理由を保存して正常終了 |
| 同日16:10 / 19:00 / 22:00 | 後処理。16時超過を即終了条件にしない |
| 翌朝8:30 / 翌営業日10:00 | 現在管理優先。古いレポートは日中に実行しない |
| 休日 / 連休 / 数日ぶり | 明示カレンダーにより不足営業日を再構成。休日に日中セッションを開始しない |

`run(runtime, sleep)` は明示的に起動した段階Aプロセス向けのループ。
データ処理の前後と照合後にも時計を再確認する。1ステップを超える同時実行はしない。
後処理は現在実行可能な仕事がなくなるか、設定されたステップ上限に達したら終了する。
未公開データのために待機し続けない。未完了は次回起動へ残す。

保存失敗時は新規操作を拒否し、`normal_exit=false` と例外種類を返す。
ローカル状態を修復してから手動再起動する。正常終了と偽らず、自動強制終了・再起動は行わない。
呼出元は失敗結果を非正常として扱う（CLIは終了コード2）。
読み取りも不能な場合は件数をnullとして表示し、推測でゼロにしない。

## 永続化・復旧

`--state-root` の下に模擬口座の `paper/` と運用状態の `lifecycle/` を分離する。
既存のOSファイルロックを再利用し、同じ保存先の第2プロセスは拒否する。
プロセス死亡時にはOSが所有権を解放する。時刻だけで生存中のロックを奪わない。

`lifecycle/lifecycle.sqlite` のテーブル:

| テーブル | 記録 |
|---|---|
| jobs | 種別・対象日・口座/モード・定義版のハッシュを一意キーにし、依存・入力・状態・次回判定・所有者・途中ページを保存 |
| attempts | 試行UUID、開始・終了、結果、回数、入力スナップショット、所有者 |
| outputs | ジョブキーに対して一度だけ保存する出力本文とSHA-256 |
| meta | 初回対象日・口座の固定、定義ハッシュ、現在状態、最終正常終了日時 |
| history | 終了準備・状態表示・正常終了等の履歴 |

出力本文・ハッシュ・完了記録・試行終了を同じSQLiteトランザクションで確定する。
中間ファイルを完成出力として公開しない。出力ハッシュを依存解除前に再検証する。
レポートはスナップショット保存で、再開時に損益を足し直さない。

前所有者のrunningジョブはinterrupted試行として保持し、
チェックポイント付きretry_waitへ戻す。保存済みページを重複追加しない。
試行回数と再試行間隔の上限に達したものは要確認にする。
同一キーでの入力差し替えは禁止。訂正テストは定義版を上げて別出力を残す。

初回対象日から直近の引けまでの営業日を起動時にも検査する。
終了処理前に落ちても、未登録ジョブを再構成する。
定義版・対象範囲を変えて過去の不完全データを無言で削除・上書きすることはない。

## データと依存

prices / topix / master / financials → quality → features → 当日のcandidates。
日次featuresは前営業日のfeaturesにも依存する。古い候補は再実行・発注しない。
reportは現在準備の後に回す。再学習・最適化・全期間バックテスト・モデル採用ジョブはない。
注文・訂正・取消をこのキューへ登録すると拒否する。

固定応答でも対象日、全ページ取得、期待キー集合、重複・件数を照合する。
取得済みページは入力応答のハッシュに固定し、途中で別版のページを混ぜない。
既存のprices/reference/market_financial品質検査を再利用する。
未公開・不完全・確認できない空応答は公開待ち、異常内容は要確認、
下流は依存待ちとなる。財務の「開示なし」は明示的根拠付き固定応答だけ許可。
休日には取得ジョブを作らない。

対象日、公表日時（不明はnull）、取得日時、内容ハッシュ、ページ取得日時を区別する。
設定例のavailable_atは**模擬応答が取得可能になる仮想時刻**であり、
実際の提供完了時刻や公表時刻を主張しない。

## 注文と資金予約

既存のExecutionManager・リスク検査・注文照合を利用し、別の注文再試行器を作らない。
日中の新規リスク追加には当日準備と照合が必要。データ不足のみで決済買いを一律禁止しない。
ただし既存の価格・余力・期限・信用区分・不明注文・権限ゲートは引き続き適用される。

終了や後処理からのsubmit/cancel/modifyを共通RouterとPaperAdapter双方で拒否する。
運用制御中の証券会社切替も拒否し、口座・ジョブの対応を維持する。
停止は取消・決済と結び付けない。
受付不明・部分約定・取消不明は、残数量、元注文、余力評価または未検証理由を保存する。
照合タイムアウトでも受付結果を作り上げず、再開時の照合対象として残す。
例外メッセージはログへ転記せず、分類のみ記録する。

## 設定例の実行方法（すべて模擬）

PowerShellでローカルのプロジェクトを作業場所にする。

```powershell
Set-Location -LiteralPath 'C:\Users\i4jll\Documents\ChatGPT\個別株自動売買ツールV1'
.venv/Scripts/python.exe -m trading_system.runtime --scenario config/lifecycle-paper-scenario.json --state-root data/lifecycle-offline-demo
.venv/Scripts/python.exe -m trading_system.runtime --scenario config/lifecycle-paper-scenario.json --state-root data/lifecycle-offline-demo --at 2026-10-26T16:10:00+09:00
.venv/Scripts/python.exe -m trading_system.runtime --scenario config/lifecycle-paper-scenario.json --state-root data/lifecycle-offline-demo --at 2026-10-26T19:00:00+09:00
.venv/Scripts/python.exe -m trading_system.runtime --scenario config/lifecycle-paper-scenario.json --state-root data/lifecycle-offline-demo --at 2026-10-27T08:30:00+09:00
```

`--at` は現在のPC時刻を変更せず、仮想時計を指定する。
模擬ブローカーは既存の固定デモ口座を使うため、その価格・余力証拠の有効期間も固定。
この例は新規の注文を一件も生成しない。
本番データの保存先、RSSの稼働DB、既存の口座状態を指定しないこと。

実行済み結果:
- 16時: 正常終了、後処理15件を保持。
- 16:10: 完了8件、公開待ち4件、依存待ち3件で正常終了。
- 19時: 15件完了、再開に伴う重複出力なし。
- 翌8:30: 日中運用、模擬の当日準備済み。1ステップ後にCLIの仮想時刻列が終わる。
  最後のnormal_exit=falseは16時終了イベントに未到達であることを表し、保存失敗ではない。

## 受入確認と制限

受入テストには以下を含む:
16時停止、照合TimeoutError、SQLite/ファイル保存失敗、
16:10/19時/22時、未公開/一部欠損/品質異常、開示なし/休場、
ページ取得中・計算中・保存トランザクションの前後のクラッシュ、
別プロセス排他、翌朝/場中優先順位、金曜～月曜相当・祝日・複数日、
受付不明/部分約定/取消不明の予約保持、訂正版と取得時点の履歴、
処理上限、後処理注文拒否、終了前未登録ジョブの再構成。
別プロセスのos._exitはテスト専用子プロセスに限定し、実運用プロセスを終了しない。

**未実装・未接続**
- 日次ジョブ全体と実RSS取得ループの統合。16時停止とRaw復旧のみ組込み済み。
- 実カレンダーの検証済み保存版からの読取り、契約プランに応じた実データ収集への接続。
- 日次の本物の特徴量更新・売買候補生成。今回のfeatures/candidatesは内部モックで
  actionable=false。current_data_readyも段階Aの模擬準備完了だけを示す。
- 過去営業日の確定した日次損益レポート、バックアップ・ログ保守の実運用ジョブ。
  今回のreportは模擬口座の現在スナップショットであり、過去日の損益と偽らない。
- read_only/live運用、外部通信のタイムアウト制御と再接続。
  今回は短い固定ハンドラーのみ。任意の無期限ブロッキング処理を強制中断する機構はない。
- 実データのページスナップショット更新を運用版に組み込む際の、版・再取得範囲の管理。
  固定シナリオの入力・カレンダー変更には新しいdefinition_versionが必要。

既存研究パイプラインは学習ラベル等を伴うため、日次運用から無条件実行していない。
これらの未接続箇所は段階Aのモック成功で本番完了と判定しない。

## 公式仕様の確認

2026-10-26に公式資料を確認:
- [JPX 売買立会時](https://www.jpx.co.jp/equities/trading/domestic/01.html):
  午後立会の終了は15:30。16:00は本システムの運用終了目標。
- [J-Quants 更新タイミング](https://jpx-jquants.com/ja/spec/data-update):
  更新予定時刻は目安として扱う。
- [J-Quants データ内容・仕様](https://jpx-jquants.com/ja/help/data):
  更新完了通知API/Webhook・版番号/ETagを前提にしない。

契約プラン・実応答の検証は行っていない。
開示・訂正を取得した日時より前の判断へさかのぼって適用しない。

## 実行した検証コマンドと結果

実行場所は上記ローカルプロジェクト。すべて外部通信を使わない試験。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_runtime_lifecycle.py tests/test_broker_contract.py -q --basetemp .test-tmp/lifecycle --junitxml=data/lifecycle-targeted.xml
.venv/Scripts/python.exe -m pytest -m 'not live' -q --basetemp .test-tmp/lifecycle-full --junitxml=data/lifecycle-full.xml
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
git diff --check
```

- 停止・再開50件＋既存ブローカー契約54件: **104 passed (9.59秒)**。
- 全回帰: **1040 passed, 5 deselected (70.65秒)**。
- Ruff check / format、git diff --check: 成功。
- 実接続用liveテスト5件: 今回は実口座・外部通信禁止のため未実行。
- 実運用での16時停止、Windowsスリープからの復帰、外部照合タイムアウト、実データ公開判定: 未実施。
- 初回の障害注入テストではテスト側のsqlite3 import不足が1件あり、修正後に関連テストと全回帰を実行した。

証跡: `data/lifecycle-targeted.xml`、`data/lifecycle-full.xml`、`data/lifecycle-full.log`、
`data/lifecycle-*-demo.json`、`data/lifecycle-verification.json`。
本書に記載した結果以外を実口座・本番運用の成功として扱わない。
