"""State and operator guidance for the two independent research recorders."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def active_status(broker, data, now):
    state, reason = data["state"], data.get("reason")
    updated = datetime.fromisoformat(data["updated_at"])
    if updated.utcoffset() is None:
        raise ValueError("timestamp without time zone")
    saved = updated.astimezone(JST)
    local = now.astimezone(JST)
    if saved.date() != local.date() or saved > local:
        return "起動中・今回の状態を待っています。"
    if state == "panel_mismatch":
        return "銘柄の照合待ち。研究用Excelの銘柄・シートを確認してください。"
    if state == "clock_regression":
        return "読取時刻に不整合があります。PCの日時を確認してください。"
    if state == "waiting_for_station":
        return "API待ち。kabuステーションにログインし、API設定を確認してください。"
    if state == "prompt":
        return "入力待ち。PCの専用画面で本番用APIパスワードを入力してください。"
    if state == "authenticating":
        return "API認証中。応答をお待ちください。"
    if state in {"not_started", "stopped", "failed", "needs_review"}:
        return "終了処理中。保存結果をお待ちください。"
    if state == "starting":
        return "研究用Excelを準備中。開くまでお待ちください。"
    if state == "unavailable":
        if reason in {
            "panel",
            "sheet_count",
            "symbol",
            "header",
            "formula",
            "chart",
            "chart_header",
        }:
            return "研究用Excelの構成を照合できません。銘柄・見出し・取得用数式を確認してください。"
        if reason in {"excel_not_registered", "workbook_not_found"}:
            return "Excel待ち。研究用分足.xlsx が開いているか確認してください（自動再確認）。"
        return "Excelの読取待ち。処理中はそのままお待ちください（自動再確認）。"

    clock = local.time()
    preopen = clock < time(9)
    lunch = time(11, 30) <= clock < time(12, 30)
    regular = time(9) <= clock < time(11, 30) or time(12, 30) <= clock < time(15, 30)
    stamp = saved.strftime("%H:%M:%S")
    if state == "read" and broker == "panel":
        total = data.get("target_symbols", 31)
        if data.get("chart_issue"):
            return "板は保存中。分足の切替・読取を再試行しています。RSS接続を確認してください。"
        disconnected = data.get("connection_required_charts", 0)
        if disconnected:
            return (
                f"RSS接続待ち {disconnected}/{total}銘柄。MARKET SPEED IIのログインを確認し、"
                "Excelが「未接続」なら押してください。"
            )
        if (local - saved).total_seconds() > 90:
            return f"読取更新待ち（最終保存 {stamp}）。ExcelとRSSの接続を確認してください。"
        n = data.get("current_day_chart_symbols")
        streaming = data.get("streaming_charts")
        if local.weekday() >= 5:
            return f"週末・直近分足を確認中（最終保存 {stamp}）。操作は不要です。"
        if preopen and streaming == total:
            return (
                f"準備完了・寄り付き待ち（RSS配信中 {total}/{total}、最終保存 {stamp}）。"
                "操作は不要です。"
            )
        if n is None:
            return f"Excel読取中（最終保存 {stamp}）。当日分足の確認を待っています。"
        if preopen:
            return f"寄り前・配信確認中（最終保存 {stamp}）。ExcelのRSS接続を確認してください。"
        if lunch or not regular:
            phase = "昼休み" if lunch else "引け後"
            return f"{phase}・当日分足 {n}/{total}銘柄（最終保存 {stamp}）。"
        if n == 0:
            if clock < time(9, 5):
                return f"寄り付き・当日分足待ち（最終保存 {stamp}）。初回配信を待っています。"
            return (
                f"当日分足 0/{total}銘柄（最終保存 {stamp}）。取引日ならRSS接続を確認してください。"
            )
        latest = data.get("latest_chart_at")
        bar = datetime.fromisoformat(latest).astimezone(JST) if latest else None
        if bar and (local - bar).total_seconds() > 300 and not time(12, 30) <= clock < time(12, 35):
            return (
                f"分足の更新待ち（最新足 {bar:%H:%M}、保存 {stamp}）。RSS接続を確認してください。"
            )
        return f"当日分足受信中 {n}/{total}銘柄（最終保存 {stamp}）。自動取得中です。"

    if state == "recording" and broker == "kabu_panel":
        if reason in {"board_http_429", "token_http_429"}:
            return "APIの流量制限・自動再試行中。そのままお待ちください。"
        if reason == "network_request_failed":
            return "通信再試行中。kabuステーションのログイン・API設定を確認してください。"
        if reason == "token_expired":
            return "APIを再認証中。応答をお待ちください。"
        if reason not in {"observed_book", "incomplete_or_crossed_book", "ok", None}:
            return "API応答を再確認中。kabuステーションの状態を確認してください。"
        count = data.get("board_reads", 0)
        if not count:
            return "API接続・初回の板待ち。応答をお待ちください。"
        if (local - saved).total_seconds() > 90:
            return f"板の巡回更新待ち（前回 {stamp}）。kabuステーションの接続を確認してください。"
        if local.weekday() >= 5:
            return f"週末・板を巡回中（状態更新 {stamp}）。操作は不要です。"
        n = data.get("current_day_board_symbols")
        if n is None:
            # Older recorders only report the number of polls.
            phase = "寄り付き待ち" if preopen else "板を巡回中"
            return f"{phase}（累計{count}回照会、状態更新 {stamp}）。自動取得中です。"
        last = data.get("last_board_at")
        if not last:
            return "初回の板待ち。自動で再確認します。"
        last = datetime.fromisoformat(last).astimezone(JST).strftime("%H:%M:%S")
        if preopen:
            return f"準備完了・寄り付き待ち（板応答保存 {last}）。操作は不要です。"
        if lunch or not regular:
            phase = "昼休み" if lunch else "引け後"
            return f"{phase}・当日時刻の板・約定 {n}/31銘柄（最終保存 {last}）。"
        if n == 0:
            if clock < time(9, 5):
                return f"寄り付き・当日データ待ち（板応答保存 {last}）。自動取得中です。"
            return f"当日時刻の板・約定 0/31銘柄（保存 {last}）。取引日なら接続を確認してください。"
        return f"板を巡回中・当日時刻 {n}/31銘柄（最終保存 {last}）。自動取得中です。"
    return "起動中・今回の状態を待っています。"


def terminal_guidance(state, reason):
    if reason == "daily_panel_missing":
        return "本日用の候補がありません。前営業日の日足と候補の保存結果を確認してください。"
    if reason == "stop_or_cutoff":
        return "開始前に停止または受付終了。次の取得時間に開始してください。"
    if reason == "collector_already_active":
        return "別の取得が実行中です。終了後に研究取得ボタンを押してください。"
    if reason == "daytime_1600":
        return "16時終了・保存完了。操作は不要です。"
    if state == "stopped" and reason == "one_pass_complete":
        return "一括取得・保存完了。操作は不要です。"
    if reason in {"manual_stop", "stop_requested", "prompt_cancelled"}:
        return "停止。再開するには研究取得の開始ボタンを押してください。"
    if reason == "outside_start_hours":
        return "開始時間外。日中の研究取得は6:00からです。"
    if reason == "registration_limit":
        return "登録上限。kabuステーションの登録銘柄を確認して再開してください。"
    if reason == "station_wait_timeout" or reason == "network_request_failed":
        return "接続未完了。kabuステーションの起動・ログイン・API設定を確認して再開してください。"
    if reason == "api_password_format_invalid":
        return "APIパスワードを確認して研究取得を開始し、専用画面に再入力してください。"
    if reason and reason.startswith("token_http_"):
        return "API認証・通信を確認してください。ログインとAPI設定を確認して再開してください。"
    if state == "failed":
        return "異常終了。API設定・保存先を確認して再開してください。"
    if state == "needs_review":
        return "取得が未完了です。API設定・保存先を確認して再開してください。"
    return None
