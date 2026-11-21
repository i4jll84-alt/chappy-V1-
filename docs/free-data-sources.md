# 無料公開資料とJ-Quants依存の確認

確認日: 2026-11-22。契約変更・本番データ源の切替・既存データの削除は行っていない。

## 現在の依存

- research/swing_daily.py: J-Quantsの最新日足、TOPIX、上場マスター、カレンダー、財務を取得する。
- research/swing_short_sources.py: J-Quantsの信用残高と公表日を保存する。B/Cの信用条件に使う。
- AのJSF残高・制限措置、決算予定、JPXの権利・規制等には既存の無料公開資料取得がある。
- 楽天RSS・カブコムの収集経路は存在するが、全候補銘柄の日足・財務をJ-Quantsと同じ条件で供給する経路は未完成。

## 無料で使える候補

| 入力 | 候補 | 確認範囲と残作業 |
| --- | --- | --- |
| 信用残高 | JPXの銘柄別信用取引残高・全銘柄PDF | 申込日・公表日・1株単位・総残高と一般/制度別を確認。保存済みJ-Quantsとの1日分全銘柄照合を実施。定常取得・訂正・異なる日の表形式を検証してから接続する。 |
| JSF・制限措置 | 日証金公式 | 既存取得あり。東証全体の信用取引残高とは別データとして扱う。 |
| 決算予定・規制等 | JPX公式 | 既存取得あり。取得時刻・予定変更履歴を残す。 |
| 日足・売買代金・TOPIX | 楽天RSS等 | 取得可能な項目を確認。銘柄数制限・ログイン・分割調整・欠損・時刻を含む全銘柄経路は未検証。 |
| 日足 | JPX東証日報 | 無料で四本値・売買高等を掲載。ただし翌営業日公開なので、当日22時の代替にはならない。翌朝9時前の安定到着も未検証。 |
| 財務 | 金融庁EDINET API、TDnet・企業IR | EDINET APIは登録すれば無料。有報・半期報告書等が対象で、最新の四半期短信すべての代替とはならない。TDnet閲覧は無料だが公式が自動取得を控えるよう求めているため、無断のスクレイピングを代替経路にしない。 |
| 投資部門別売買・業種等 | JPX公式 | 無料の表があるが、現在使うJ-Quantsデータとの変換・照合は未実装。 |

J-Quants Freeの直近12週間の遅延を、最新日の無料取得と誤認しない。
無料信用PDFの公開一覧は直近5申込日。10年履歴が無料で遡及できるとは確認していない。
日付が同じことと、過去の判断時刻までに取得できていたことは別である。
今回ダウンロードした時刻を保存し、過去の提供時刻を推測で埋めない。
個別検証と保存元はdata/offline-verification/free-data-audit-20261122/に保存する。

## 契約判断

Freeだけでは現在の毎日スクリーニングを維持できない。
信用残高を無料公開表に切り替えた後は、最新日足・財務・TOPIXをLightで賄う構成も候補になる。
ただし現在の10年研究の取得データを継続使用することと、契約ダウングレードを両立できるとは限らない。

J-Quants公式説明では、解約時は契約終了後に取得データを、ダウングレード時は上位プランのみで取得できたデータを削除する。
対象には変換形式・バックアップ等の複製も含む。元データを復元できない集計やモデルには別の取り扱いがある。
実際の移行は必要なデータ・派生物・複製の一覧と代替手段を確定してから判断する。
この調査は削除や解約の承認ではない。

## 公式資料

- [プランと料金](https://jpx-jquants.com/ja)
- [Freeの遅延・プラン変更](https://jpx-jquants.com/ja/help/plan)
- [契約終了・ダウングレード後の利用](https://jpx-jquants.com/ja/help/usage)
- [JPX信用取引残高](https://www.jpx.co.jp/markets/statistics-equities/margin/01.html)
- [東証日報の掲載日時・項目](https://www.jpx.co.jp/markets/statistics-equities/daily/01.html)
- [楽天RSS関数と取得項目](https://marketspeed.jp/ms2_rss/onlinehelp/ohm_002/ohm_002_07.html)
- [楽天RSSの上限](https://marketspeed.jp/ms2_rss/onlinehelp/ohm_002/ohm_002_08.html)
- [TDnetの開示情報閲覧](https://www.jpx.co.jp/listing/disclosure/01.html)

- [EDINET APIは登録後無料](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0090_001.html)
- [四半期報告書廃止と半期報告書](https://disclosure2dl.edinet-fsa.go.jp/guide/static/submit/WZEK0120.html)
