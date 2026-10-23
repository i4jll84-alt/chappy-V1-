# Japan Stock Multi-Strategy Trading System

ユーザーの基本仕様は docs/development-instructions.md を参照する。
現在は第1段階のみ。実APIで非空株価取得、Parquet保存、DuckDB読出し、品質検査、
再実行の重複防止、pytest/Ruffを確認するまで第2段階へ進まない。

- 公式仕様で確認したAPIのみ使用する。認証情報をGit・ログ・画面へ出さない。
- Rawを保持し、異常値を自動削除・補完しない。無約定日の全nullと異常欠損を区別する。
- 公表・提供元更新日時が不明ならnull。取得日時とは区別する。
- 初期PITは取得完了以降のみ利用可。履歴未保存の過去時点を再現できると主張しない。
- 同一内容の再取得と訂正を区別し、訂正前バージョンも保存する。
- ページ保存とcheckpointをDBトランザクションで確定。Parquetは再生成可能にする。
- 発注・AI学習・バックテストをこの段階に追加しない。実資金注文は明示承認が必要。
- テスト結果・未確認・制限を正直に記録する。モック成功を実接続成功と扱わない。
- 重要変更前はGitで復元できる状態を確保する。秘密と取得データはコミット禁止。
- ユーザー指定により、設計/重要レビューはGPT-6 Astra High、通常実装は
  GPT-6.1 Sol Medium〜Highを推奨。実際に指定・確認できたモデルのみ記録する。
- 検証: .venv/Scripts/python.exe -m pytest、ruff check .、ruff format --check .
