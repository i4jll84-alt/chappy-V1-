# 証券会社3社共通基盤 — Phase 1

作業日: 2026-10-26。対象はローカルの個別株自動売買ツールV1。
今回の追加指示を優先し、既存の戦略・学習・バックテスト・RSS取得処理は変更せず、
共通注文契約とオフラインのペーパートレード経路を追加した。

## 実装範囲

| 対象 | 共通インターフェース | 認証・実接続 | 実注文 | ペーパー |
|---|---|---|---|---|
| 楽天証券 | 適合 | 新アダプターでは未実装・未検証 | 無効・送信コードなし | 対応 |
| 立花証券 e支店 | 適合 | 未実装・未検証、初期無効 | 無効・送信コードなし | 設定で有効化後に対応 |
| 三菱UFJ eスマート証券 | 適合 | 未実装・未検証、初期無効 | 無効・送信コードなし | 設定で有効化後に対応 |

楽天RSSの既存の読取専用3セル取得は別経路として維持している。
それが動作したことを、新共通アダプターの実接続検証済みとは扱わない。

- 接続・状態・株価・板・現物買売・信用新規売り／返済買い・注文照会／取消／訂正・約定・建玉・余力・売建可能情報・費用制限の共通契約を定義。
- ペーパーは独立した模擬証券側SQLiteとローカル注文管理SQLiteを照合する。
- 信用新規買い／返済売りは入力モデルで拒否。信用区分は制度信用／一般信用を必須指定。
- 返済買いは口座・信用区分・保有ポジションIDを確認し、過剰返済と他区分との相殺を拒否。
- ペーパーの訂正は明示的な未対応。自動的な取消→再発注への置換はしない。
- 株価・板・信用在庫は明示された模擬データのみ。取得時刻・提供元更新時刻・鮮度確認を分離。
- 売建許可／在庫／区分／有効期間が不明・期限切れなら新規売りを拒否。
- 既存の数量・単位・呼値・価格帯・余力・保証金・日次損失・総エクスポージャー制限を再利用。
- 手数料は明示された推定値。貸株料・逆日歩・スプレッド・スリッページの未取得値はnull。
- 内部注文IDはUUIDを発行可能。既存戦略のIDがある場合は from_intent でそのまま維持。
- 内部IDと送信先口座を送信前に永続化。注文受付番号は応答後に対応付ける。
- 同じIDの異なる内容・口座は拒否。同じ内容の再実行は照会のみ。
- 応答不明、送信前後の中断、照合不一致では新規注文を止め、無条件再送しない。
- 部分約定は一意な約定IDと模擬証券側の累積約定証拠から反映。ローカルの発注意図だけでは約定にしない。
- 緊急停止は新規エントリー停止を永続化。既存建玉決済・注文取消は自動では行わない。
- 切替時に両方の口座の注文・建玉を照会。未約定・不明注文・建玉がある場合は拒否する保守的な実装。
- 既存IDの照会・取消は元の口座へ固定。切替後の新しい口座へ付け替えない。
- OSファイルロックで同一状態ディレクトリの二重起動を拒否。Windows依存処理はロック内部だけに限定。
- 再起動後の不明注文は照会可能だが、新規エントリーの停止は自動解除しない。

## ファイルと既存機能との関係

追加:
- src/trading_system/brokers/base.py: 抽象インターフェースと未実装時の明示例外
- src/trading_system/brokers/models.py: 注文・約定・建玉・余力・信用区分・市場証拠・費用
- src/trading_system/brokers/capabilities.py: 仕様上の機能とソフトウェア実装／検証状況を分離
- src/trading_system/brokers/registry.py: 厳格な設定、無効会社の接続禁止、口座別生成
- src/trading_system/brokers/router.py: 永続ルーティング、切替、重複防止、照合、緊急停止
- src/trading_system/brokers/locking.py: 排他的な状態ファイル利用
- src/trading_system/brokers/rakuten/adapter.py
- src/trading_system/brokers/tachibana/adapter.py
- src/trading_system/brokers/mufg_esmart/adapter.py
- src/trading_system/brokers/paper/adapter.py: 既存MockGateway／ExecutionManagerの再利用
- src/trading_system/brokers/demo.py、__main__.py、各__init__.py
- config/brokers.toml、config/paper-broker-example.json
- tests/test_broker_contract.py
- docs/brokers-phase1.md（本書）

追加: src/trading_system/cli_offline.py（既存オフライン機能へのCLI経路復旧）。
変更:
- README.md: 操作と制約の案内追加。
- src/trading_system/cli.py: 既存バックテスト・ポートフォリオ・研究・各監査へのCLI登録を復旧。
- src/trading_system/data_sources/jquants.py: 既存TOPIX・財務機能で使用する公式URLと財務専用レート枠の登録漏れを修正。
- tests/test_reference.py: ランダムなファイル名末尾による検査除外を解消し、別テストの実時計と固定時計の混在を修正。
既存 execution/、RSS、バックテスト、学習、戦略の中核ソースは変更前バックアップとバイト単位で一致。
CLI復旧は既存の実装への入口を戻す変更で、売買戦略や学習方式を変更していない。
追加依存パッケージはない。

## 起動と設定

PowerShellの作業場所をローカルへ指定して実行する。

```powershell
Set-Location -LiteralPath 'C:\Users\i4jll\Documents\ChatGPT\個別株自動売買ツールV1'
.\.venv\Scripts\python.exe -m trading_system.brokers doctor
.\.venv\Scripts\python.exe -m trading_system.brokers demo --state-root data/broker-phase1-demo
```

doctorは認証情報を読まず、接続を試さず、機能表と設定を表示する。
demoは固定された過去日時と架空銘柄MOCK-Aで、100株の買い／売り、
各40株＋60株の部分約定、手数料控除、残高照合を実行する。
再実行しても同じIDを再送・再約定しない。これは当日の取引や運用成績ではない。

config/brokers.toml は初期 paper / rakuten / live_trading_enabled=false。
立花／三菱UFJの enabled=true はこのPhaseではペーパー口座の選択だけを許可する。
実口座番号、パスワード、APIトークンは不要。
mode=live や live_trading_enabled=true は設定検証で拒否する。
実発注有効化の承認手順・実装自体は次段階で設計するため、フラグ変更だけでは有効化できない。

config/paper-broker-example.json は明示された模擬リスク条件・営業セッション・板・在庫の例。
PaperFixture.model_validate_jsonでロードでき、BrokerRouterへ渡せる。
異なる条件で実験するときは別のstate-rootを使用する。
同じ口座データベースの初期資金・市場証拠・信用条件を後から書き換えることは拒否する。
state-root削除による重複防止履歴の消失に注意する。

## 共通経路の利用例

```python
from pathlib import Path
from trading_system.brokers.registry import load_config, PaperFixture
from trading_system.brokers.router import BrokerRouter
from trading_system.brokers.models import from_intent

config = load_config(Path("config/brokers.toml"))
fixture = PaperFixture.model_validate_json(
    Path("config/paper-broker-example.json").read_text(encoding="utf-8")
)
router = BrokerRouter(Path("data/paper-experiment"), config, fixture)
try:
    # signal は既存戦略の OrderIntent。信用の場合はcreditと返済対象を別途指定。
    # order = router.submit(from_intent(signal, router.active), simulation_time)
    # router.reconcile(simulation_time)
    # router.switch("tachibana", simulation_time)  # 設定有効、両口座がフラットの場合のみ
    print(router.adapter.connection_status())
finally:
    router.close()
```

simulate_fillは明示的な模擬約定通知用。価格・出来高から自動的に約定を生成するモデルは未実装。
reconcileは呼び出し時の照合であり、常時監視スケジューラはここでは起動しない。
信用建玉は模擬口座内の銘柄・区分単位で管理し、異区分の同時建てを拒否。
実口座の複数建玉ID・決済順序・日々の貸株料や逆日歩計上はPhase 2以降の課題。

## 機能表の読み方と公式仕様

vendor_support=unverified は「APIにない」ではなく「この項目の詳細仕様を未確認」。
documented_family は公式資料で現物／信用の注文機能群を確認しただけで、
個別引数・対象銘柄・在庫・価格規制・利用資格まで確認した意味ではない。
implementation=not_implemented と verification=not_live_verified を維持する。
paperでは simulated / offline_tests_only と表示する。

参照日2026-10-26:
- 楽天公式: https://marketspeed.jp/ms2_rss/onlinehelp/ohm_002/ohm_002_06.html
  現物・信用新規／返済・訂正／取消の関数群を確認。
- 立花公式: https://www.e-shiten.jp/api/
  現物／信用、株価・板、デモ環境の案内を確認。
- 立花API資料入口: https://www.e-shiten.jp/e_api/mfds_json_api_menu.html
  認証・現行版・デモ利用条件の詳細確認は次段階。
- 三菱UFJ公式API資料: https://kabucom.github.io/kabusapi/reference/
  注文APIと信用区分を含むリクエストの存在を確認。詳細の接続／発注検証は未実施。

未確認: 各社の全エラーコード、スロットリング、注文訂正制約、在庫照会の意味と更新頻度、
制度／一般／日計り等の実際の信用区分、受付時間、注文の重複キー保証、
タイムアウト後の検索条件、認証更新・セッション失効、手数料と貸株料、
デモ環境の最新利用条件。これらの値を推測で実通信へ使用していない。

## テストと残課題

最新の機械可読結果は data/brokers-phase1-verification.json に保存。
新規契約テストではネットワーク接続を禁止し、3社の未実装応答、無効設定、
4売買方向、部分約定、取消、在庫不足、未知・未来・期限切れ証拠、
休場／受付時間外、信用区分違い、ID衝突、応答喪失、再起動、
永続ルート不明、切替前後照会、残高分離、緊急停止、ロックを検証する。

変更前の全体テストは852成功・84失敗・5除外だった。
失敗は既存CLI関連が中心で、今回の変更で隠したりテストを無効化していない。
変更前後の失敗テストIDの比較結果も検証JSONに保存する。
最終結果: **990成功・0失敗・実APIテスト5除外**（62.90秒）。
新規共通基盤の契約テストは54件。関連する注文・RSS・CLI回帰は231件成功。
Ruff check . と Ruff format --check . は成功（156ファイル）。
初期失敗84件は、CLI登録漏れ・J-Quants取得先／財務専用レート枠の登録漏れ、
既存テストのランダムなファイル選択と時計混在の修正で解消した。
取得データの異常履歴を削除したり、テストを無効化して成功扱いにする修正は行っていない。
実行環境はWindows/Python 3.12。Linux実機・実口座・各社デモ環境は未検証。
J-Quants登録の確認資料:
https://jpx-jquants.com/en/spec/migration-v1-v2
https://jpx-jquants.com/ja/spec/rate-limits
実APIリクエストは送らず、既存MockTransportテストで登録を検証した。

Phase 2:
1. Windows／Excel／MarketSpeed IIの読取専用ブリッジを新共通契約へ接続。
2. 実データの株価・板・注文・約定・建玉・余力を個別に検証し、データ提供時刻と鮮度を証明。
3. 公式の銘柄制限／営業カレンダー／注文受付時間／在庫／費用を導入。
4. 実注文輸送と明示承認の仕組みを分離して実装し、承認前は送信不能を維持。
5. 切断・再接続・プロセス再起動・部分約定・不明状態を照合し、定期照合と緊急停止を統合。
6. 立花・三菱UFJは資格情報なしで仕様モックを進め、実接続は口座開設後に別検証。

本日のRSS長時間試験で見つかった終了時audit_run_limit（1000バッチ超）と
10000行の監査上限は本変更の範囲外で、まだ修正していない。
既存RSSの再長時間運転前に修正・終了時再照合テストが必要。

## セキュリティ・運用上の注意

実通信・Excel操作・認証情報保存・発注トリガー変更は追加していない。
口座識別子は秘密を含まないローカル別名を使用する。
設定・エラーをそのまま画面へ出さず、CLIでは秘密を含まないblocked結果を返す。
ローカルの状態DBは注文履歴・資金を含むため、公開・Gitコミット対象にしない。
ペーパーの成功は実接続や収益性の証明ではなく、従来のPIT・OOS・
ウォークフォワード・TOPIX比較等の検証要件を緩和しない。
