# Japan Stock Multi-Strategy Trading System

ユーザーの基本仕様は docs/development-instructions.md を参照する。
第1段階の最小完了条件は docs/stage1-readiness.md の範囲で検証済み。
第2段階の初期バックテスト基盤は docs/stage2-report.md の合成入力範囲で検証済み。
第3段階の初期予測モデル基盤は docs/stage3-report.md の合成入力範囲で検証済み。
第8章の固定配分・リスク判定の初期基盤は docs/portfolio-stage-report.md の合成入力範囲で検証済み。
複合時系列シミュレーションは docs/portfolio-simulation-report.md の合成入力範囲で検証済み。
第4段階の初期モック注文管理は docs/mock-execution-report.md の範囲で検証済み。
ExcelでRSS読取接続と3セルの市況取得を確認済み。Pythonから実行中Excelの3セルを
読み取る初期実装と3回の実機観測を確認済み（docs/rss-live-report.md）。RSS提供元との
現在の接続・鮮度・取引時間中の更新は未検証。値不変だけで切断と認定しない。
保存済みライブ観測のRaw保持・DuckDB取り込み・Parquet再生成は storage/rss.py を使用する。
範囲は docs/rss-storage-report.md。観測IDの内容変更は上書きせず拒否し、市場訂正と推定しない。
RSS保存ストアの読取専用監視は monitoring/rss.py、範囲は docs/rss-monitoring-report.md。
整合性成功を接続・鮮度・観測の網羅性と混同しない。監視で修復・削除・再取得は行わない。
元の観測との照合は監視の --runs を明示する。指定ディレクトリ内の網羅性だけを確認し、
未保存の観測や市場全体の網羅性は保証しない。docs/rss-coverage-report.md を参照する。
有限回の読取・保存・照合の一括実行は rss_workflow.py。--run は保存済み観測だけを扱う。
自動再接続・常駐・発注は含めない。範囲は docs/rss-workflow-report.md。
RSSの導入状況と接続前の未確認事項は docs/rss-readiness.md を参照する。
Excel未導入によるRSS保留後、2026-10-23にMicrosoft 365 x64とExcel本体の導入を確認。
同日にRSSアドイン登録、Excel実起動、接続中・発注不可表示と保存ブックを確認した。
保存済みRSSブックの検査は data_sources/rss_snapshot.py を使用する。保存キャッシュの検査を
現在の接続・鮮度・取引時間中の自動更新の証明にしない。「現在日付」と時刻から約定日時を
推定しない。取得データと元ブックはGit対象外。保存済みモック状態の読取専用監視は
docs/monitoring-stage-report.md の範囲とする。監視から注文復旧・停止解除を行わない。
保存済み株価取得の読取専用監視は docs/price-monitoring-report.md を参照する。
取得完了の経過時間を、市場データの網羅性・提供元の更新時刻・実API疎通と混同しない。
保存済み合成モデル結果の監視は docs/research-monitoring-report.md の範囲とする。
監視で再学習・推論・最終評価の再実行・モデル差替えをせず、実市場の劣化を認定しない。
第1段階のデータ制約・PIT制約は継続して適用する。

- 公式仕様で確認したAPIのみ使用する。認証情報をGit・ログ・画面へ出さない。
- Rawを保持し、異常値を自動削除・補完しない。無約定日の全nullと異常欠損を区別する。
- 公表・提供元更新日時が不明ならnull。取得日時とは区別する。
- 初期PITは取得完了以降のみ利用可。履歴未保存の過去時点を再現できると主張しない。
- 同一内容の再取得と訂正を区別し、訂正前バージョンも保存する。
- ページ保存とcheckpointをDBトランザクションで確定。Parquetは再生成可能にする。
- 特徴量・学習・評価と固定配分の初期検証は合成データとする。実市場データの不足は継続し、
  時点付きの残高・保有・予約・制度・相関を明示してリスク判定する。証券会社への発注は対象外。
  実資金注文は別途明示承認が必要。模擬データの成功を実市場での有効性と扱わない。
- テスト結果・未確認・制限を正直に記録する。モック成功を実接続成功と扱わない。
- 重要変更前はGitで復元できる状態を確保する。秘密と取得データはコミット禁止。
- ユーザー指定により、設計/重要レビューはGPT-6 Astra High、通常実装は
  GPT-6.1 Sol Medium〜Highを推奨。実際に指定・確認できたモデルのみ記録する。
- 検証: .venv/Scripts/python.exe -m pytest、ruff check .、ruff format --check .
