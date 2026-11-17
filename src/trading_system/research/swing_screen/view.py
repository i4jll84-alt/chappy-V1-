"""Read-only history and results viewer with an explicit daily publication button."""

import threading
from datetime import datetime
from tkinter import StringVar, Toplevel, ttk
from tkinter.scrolledtext import ScrolledText

import pandas as pd

from .prepare import JST, daily, load, root


def show(parent, project):
    win = Toplevel(parent)
    win.title("順張りスイング研究")
    bar = ttk.Frame(win)
    bar.pack(fill="x")
    which = StringVar(win, value="A")
    date = StringVar(win)
    state = StringVar(win)
    methods = ttk.Combobox(bar, textvariable=which, values=list("ABCD"), width=4, state="readonly")
    methods.pack(side="left")
    dates = ttk.Combobox(bar, textvariable=date, width=22, state="readonly")
    dates.pack(side="left")
    text = ScrolledText(win, width=111, height=27, wrap="none")
    text.pack(fill="both", expand=True)
    horizontal = ttk.Scrollbar(win, orient="horizontal", command=text.xview)
    text.configure(xscrollcommand=horizontal.set)
    horizontal.pack(fill="x")
    home = root(project)

    def display(msg):
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", msg)
        text.configure(state="disabled")

    def refresh(event=None):
        paths = {
            p.parent.name + " " + group: p
            for folder, group in (("daily", "確定"), ("previews", "研究"))
            for p in (home / folder).glob("*/first.json")
        }
        first_labels = sorted(paths, reverse=True)
        labels = []
        for label in first_labels:
            first = paths[label]
            labels.append(label)
            original = load(first)["version"]
            for revision in sorted(first.parent.glob("*/summary.json")):
                if revision.parent.name != original:
                    item = label + " 訂正版 " + revision.parent.name[:8]
                    paths[item] = revision
                    labels.append(item)
        dates["values"] = labels
        if date.get() not in paths:
            date.set(labels[0] if labels else "")
        if not date.get():
            display("確定候補はまだありません。営業日の当日分保存後に作成します。")
            return
        path = paths[date.get()]
        meta = load(path)
        m = which.get()
        base = path.parent / meta["version"] if path.name == "first.json" else path.parent
        evidence = load(base / "inputs.json")
        receipt = evidence["inputs"].get("daily/" + meta["date"], {}).get("completed_at")

        def local_time(value):
            return (
                datetime.fromisoformat(value).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
                if value
                else "未確認"
            )

        frame = pd.read_parquet(base / "features.parquet")
        columns = [
            "CoName",
            "m20",
            "m60",
            "rs20",
            "ma20",
            "ma60",
            "atr",
            "next_earnings",
            "swing_cost" if m != "A" and "swing_cost" in frame else "cost",
            "reason_" + m,
        ]
        selected = frame.reindex(meta["candidates"][m])[columns].copy()
        selected.insert(0, "順位", range(1, len(selected) + 1))
        for col in ("m20", "m60", "rs20"):
            selected[col] = selected[col].map(lambda v: f"{v:+.2%}" if pd.notna(v) else "欠測")
        selected.columns = [
            "順位",
            "銘柄名",
            "20日",
            "60日",
            "対TOPIX20日",
            "20日線",
            "60日線",
            "ATR",
            "次回決算",
            "100株費用",
            "選定理由",
        ]
        desc = {
            "A": "流動性上位100から5日値幅順",
            "B": "20日上昇率順",
            "C": "TOPIX超過20日順",
            "D": "20・60日上昇と移動平均の上昇",
        }
        display(
            f"{date.get()}　翌営業日 {meta['target']}　"
            f"判定 {local_time(meta['asof'])}（日本時間）\n"
            f"方式{m}　{desc[m]}　対象 {meta['universe']:,}　候補 {len(selected)}\n"
            f"日足一式の受領 {local_time(receipt)}　候補保存 {local_time(meta['published'])}\n"
            "決算予定は保存済みの公表情報を表示。空欄は未確認。\n\n"
            + selected.to_string(na_rep="未確認", float_format=lambda x: f"{x:,.2f}")
        )

    def compare():
        file = home / "latest-result.json"
        display(
            (home / load(file)["folder"] / "結果.md").read_text(encoding="utf-8")
            if file.is_file()
            else "比較バックテストの完了待ち"
        )

    def paper():
        files = sorted((home / "paper").glob("*.json"))
        if not files:
            display("模擬口座は最初の確定候補の翌営業日から記帳します。")
            return
        data = load(files[-1])
        display(
            data["date"]
            + "\n"
            + "\n".join(
                f"{k}　資産 {v['nav']['nav']:,.0f}円　保有 {v['nav']['positions']}銘柄"
                + ("　評価暫定" if v["nav"]["uncertain"] or v["nav"]["stale"] else "")
                for k, v in data["accounts"].items()
            )
        )

    def create():
        button.configure(state="disabled")
        state.set("受領済みの当日データを照合中")
        result = []

        def worker():
            try:
                result.append(daily(project, datetime.now(JST).date().isoformat())["state"])
            except Exception as exc:
                result.append(str(exc))

        def poll():
            if not result:
                win.after(200, poll)
                return
            messages = {
                "ready": "当日の候補を保存済み",
                "holiday": "休場日",
                "awaiting_daily_receipt": "当日の日足一式の受領待ち",
                "outside_publication_window": "確定可能時間は引け後16時30分から翌立会日の9時前まで",
                "publication_deadline_passed": "寄付前の期限を経過",
                "stopped": "研究用の停止設定が有効",
            }
            state.set(messages.get(result[0], result[0]))
            button.configure(state="normal")
            refresh()

        threading.Thread(target=worker, daemon=True).start()
        win.after(200, poll)

    button = ttk.Button(bar, text="当日候補を作成", command=create)
    button.pack(side="left")
    for name, fn in [
        ("過去候補", refresh),
        ("方式比較・バックテスト", compare),
        ("模擬口座", paper),
    ]:
        ttk.Button(bar, text=name, command=fn).pack(side="left")
    ttk.Label(win, textvariable=state).pack(anchor="w")
    methods.bind("<<ComboboxSelected>>", refresh)
    dates.bind("<<ComboboxSelected>>", refresh)
    refresh()
    return win
