# J-Quants公式仕様の確認（2026-10-23）

契約はユーザー申告のStandard。契約状態を取得する公式APIは今回確認できていないため実装しない。
利用可否は契約画面と実応答で別途検証する。403はaccess_deniedであり未契約とは断定しない。
200空配列はempty、400は入力/期間等のエラー、401は認証失敗として区別する。

## 接続

- [クイックスタート](https://jpx-jquants.com/ja/spec/quickstart)：V2はx-api-key認証。
- [日次株価](https://jpx-jquants.com/ja/spec/eq-bars-daily)：`GET https://api.jquants.com/v2/equities/bars/daily`、code/from/to。
- [ページング](https://jpx-jquants.com/ja/spec/pagination)：data配列とpagination_key。固定条件で最後まで取得。ページ間の同一スナップショットは保証されない。
- [レート制限](https://jpx-jquants.com/ja/spec/rate-limits)：Free/Light/Standard/Premiumは毎分5/60/120/500回。429は標準120秒待機。
- [更新時刻](https://jpx-jquants.com/ja/spec/data-update)：日次株価の目安は16:30頃。実際の利用可能時刻として確定保存しない。

## データ対応状況

以下の利用範囲は[プラン別公式表](https://jpx-jquants.com/ja/spec/data-spec)による公表仕様。
Standardは基本10年前まで、ただしデータ自体の提供開始日を超えて取得できない。
Freeは直近12週間を除く2年間、Lightは5年、Premiumは最長20年という違いがある。

| 優先度/データ | 確認済みV2パス | Standard・提供開始 | 更新の目安 | 現実装 |
|---|---|---|---|---|
| A 日次株価/出来高/代金/調整係数 | /equities/bars/daily | 可・2008-05-07 | 日次16:30 | 取得・保存・検査 |
| A 銘柄/業種/市場 | /equities/master | 可・2008-05-07 | 日次17:30/翌8:00 | 取得・保存・検査 |
| A 財務・決算概要 | /fins/summary | 可・2008-07-07 | 日次18:00/24:30頃 | 第1段階の取得・保存・検査 |
| A TOPIX日次 | /indices/bars/daily/topix | 可・2008-05-07 | 日次16:30 | 第1段階の取得・保存・検査 |
| A 取引カレンダー | /markets/calendar | 過去10年/翌年末・2008-01-01 | 不定期 | 取得・保存・検査 |
| B 信用残 | /markets/margin-interest | 可・2012-02-10 | 現在は日次16:00 | 未実装 |
| B 空売り報告 | /markets/short-sale-report | 可・2013-11-07 | 日次17:30 | 未実装 |
| B 投資部門別 | /equities/investor-types | 可・2008-01-16 | 原則週次 | 未実装 |
| C 大株主 | /edinet/major-shareholders | 可・2016-06-01 | 平日随時 | 未実装 |
| C 大量保有 | /edinet/large-volume-shareholders | 可・2021-07-01 | 平日随時 | 未実装 |

時点項目について、実装済み日次株価・TOPIXの日次データの実提供日時は未提供。
TOPIXの日次列・休場日行の仕様は[TOPIX日次仕様](https://jpx-jquants.com/ja/spec/idx-bars-daily-topix)を参照。
財務は[財務サマリー仕様](https://jpx-jquants.com/ja/spec/fin-summary)の `DiscDate`/`DiscTime` を公表日時として扱う。
公式例は `HH:MM:00`（秒は常に00）だが、
実APIでは `HH:MM` の応答も55件中8件で観測された。実装は両形式を分精度として受け入れ、Rawは原文のまま保持する。
正規化した秒 `00` は実際の公表時刻が秒00だったことを保証せず、時刻信頼度は `publication_minute_and_observed`。
この提供仕様と観測結果の差を記録し、取得Rawを保持して検査する。訂正は別の `DiscNo` を持つ開示として
提供される。訂正フラグはなく、`DiscNo` は時系列順を保証せず、提供元が同じレコードを後から上書きする場合もある。
このため同一内容の再取得と同一キー内容の変更を分け、観測した各内容を保持する。
財務概要の空欄は未提供/不明でありゼロに置き換えない。数値項目も公式応答では文字列である。
詳細財務 `/fins/details` はPremium限定で、この段階では取得しない。
財務API専用レート制限は[公式レート制限表](https://jpx-jquants.com/ja/spec/rate-limits)に記載の毎分60回で、通常API枠とは独立している。
独立した分割・併合イベント一式のデータセットは今回確認できず、日次のAdjFactor等を保持する。
調整済み系列は将来の分割等で遡及改変される。日次株価の訂正前全履歴はAPIから復元できない。
[データFAQ](https://jpx-jquants.com/ja/help/data)

## 契約と保管

銘柄マスター/カレンダーの詳細と時点管理は[追加設計](reference-data.md)を参照。

TOPIX・財務概要の取得、版管理、欠損とPoint-in-Timeの扱いは[市場・財務データ設計](market-financial-data.md)を参照。

[利用規約](https://jpx-jquants.com/ja/termsofservice?lang=ja)第7条/15条に、契約終了・退会時の
データおよび複製物の廃棄条件がある。プロバイダー単位のdata_rootにRaw、DB、Parquet、
ログをまとめ、削除対象を識別可能にする。契約終了後の保持を前提にしない。
加工物・学習モデルの継続利用の権利は未確認であり、後続段階前に確認する。
作業場所はOneDrive配下なので、同期コピー・版履歴・バックアップも廃棄対象の検討に含める。
この実装は契約状態を自動監視せず、削除も自動実行しない。終了時は同期先を含めて確認して廃棄する。
