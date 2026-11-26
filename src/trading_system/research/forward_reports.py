"""Publish read-only forward paper reports and keep prior report versions locally."""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from trading_system.brokers.locking import single_writer
from trading_system.research.daytrade_universe import latest_closed_session
from trading_system.storage.store import atomic_write

JST = ZoneInfo("Asia/Tokyo")
TITLE = "個別株自動売買ツールv1フューチャーテスト"
LABELS = {"day": "日次", "week": "週次", "year": "年次"}


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_output(path):
    path = Path(path).expanduser().resolve()
    if any(part.casefold().startswith("onedrive") for part in path.parts):
        raise ValueError("report_output_must_not_use_onedrive")
    return path


def settings(project):
    path = project / "data/forward-reports/config.json"
    if path.exists():
        return json.loads(path.read_bytes())
    return {"output": str(Path.home() / "Desktop" / TITLE)}


def renderer_python(config):
    configured = Path(config["renderer_python"]) if config.get("renderer_python") else None
    if configured and configured.is_file():
        return configured
    if importlib.util.find_spec("reportlab") is not None:
        return Path(sys.executable)
    base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    choices = sorted(
        base.glob("OpenAI.CodexPrimaryRuntime.*/dependencies/python/python.exe"), reverse=True
    )
    for path in choices:
        if path.is_file():
            return path
    raise FileNotFoundError("pdf_runtime_with_reportlab_required")


def render(packet_path, target, config):
    executable = renderer_python(config)
    source = Path(__file__).with_name("forward_report_pdf.py")
    completed = subprocess.run(
        [
            str(executable),
            "-B",
            "-X",
            "utf8",
            str(source),
            "--packet",
            str(packet_path),
            "--output",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode:
        # Do not include arbitrary provider strings or local account payloads in logs.
        raise ValueError("pdf_renderer_failed")
    return json.loads(completed.stdout)


def validate_render(manifest, staging):
    reports = manifest["reports"]
    if set(reports) != set(LABELS):
        raise ValueError("report_periods_missing")
    checked = {}
    for period, row in reports.items():
        path = Path(row["path"]).resolve()
        if not path.is_relative_to(staging.resolve()) or path.suffix.lower() != ".pdf":
            raise ValueError("invalid_render_output_path")
        reader = PdfReader(path)
        if len(reader.pages) != row["pages"] or not reader.pages:
            raise ValueError("pdf_page_count_mismatch")
        charts = []
        for raw in row.get("charts", []):
            # Renderer returns standalone chart paths.
            chart = Path(raw).resolve()
            if not chart.is_relative_to(staging.resolve()) or chart.suffix.lower() != ".svg":
                raise ValueError("invalid_chart_output_path")
            if b"<svg" not in chart.read_bytes()[:1000]:
                raise ValueError("invalid_chart_svg")
            charts.append(chart)
        checked[period] = {"path": path, "pages": len(reader.pages), "charts": charts}
    return checked


def publish_file(source, destination, root):
    body = source.read_bytes()
    if destination.exists():
        old = destination.read_bytes()
        if old == body:
            return
        old_sha = hashlib.sha256(old).hexdigest()
        backup = root / "変更前のレポート" / old_sha[:16] / destination.relative_to(root)
        if backup.exists() and backup.read_bytes() != old:
            raise ValueError("report_archive_conflict")
        atomic_write(backup, old)
    atomic_write(destination, body)


def output_valid(manifest):
    for row in manifest.get("reports", {}).values():
        if not Path(row["path"]).exists() or digest(row["path"]) != row["sha256"]:
            return False
        for chart in row.get("charts", []):
            if not Path(chart["path"]).exists() or digest(chart["path"]) != chart["sha256"]:
                return False
    return set(manifest.get("reports", {})) == set(LABELS)


def publish_index(output, manifest):
    links = []
    for period, label in LABELS.items():
        path = Path(manifest["reports"][period]["path"])
        relative = path.relative_to(output).as_posix()
        links.append(f'<li><a href="{quote(relative)}">{label}レポート</a></li>')
    status = "検証済み記録の集計" if manifest["status"] == "complete" else "未確認項目あり"
    body = f"""<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TITLE}</title><style>body{{font:17px Meiryo,sans-serif;max-width:850px;
margin:48px auto;padding:20px;color:#193348}}li{{margin:18px 0}}a{{color:#16698e}}</style>
<h1>{TITLE}</h1><p>{escape(manifest["asof"])} 引け時点 | {status}</p>
<ul>{"".join(links)}</ul>
<p>資金推移は初期資金＋売却済み取引の費用控除後損益です。保有株の評価損益は別表です。</p>
<p>ペーパーテストの模擬約定を集計しています。週次・年次は期間途中の更新を含みます。</p>
<p>作成日時: {escape(manifest["generated_at"])}</p></html>"""
    atomic_write(output / "レポート一覧.html", body.encode())


def rendering_signature(config):
    sources = [
        Path(__file__),
        Path(__file__).with_name("forward_report_pdf.py"),
        Path(__file__).with_name("forward_report_data.py"),
    ]
    content = {
        "sources": {p.name: digest(p) for p in sources},
        "runtime": config.get("renderer_python"),
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def run(project, *, asof=None, now=None, output=None, builder=None, runner=None):
    project = Path(project).resolve()
    now = now or datetime.now(JST)
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    now = now.astimezone(JST)
    config = settings(project)
    destination = local_output(output or config["output"])
    root = project / "data/forward-reports"
    if asof is None:
        asof = latest_closed_session(project / "data/jquants", now).isoformat()
    elif not isinstance(asof, str):
        asof = asof.isoformat()
    if builder is None:
        from .forward_report_data import build_report_packet

        builder = build_report_packet
    with single_writer(root / "dispatch"):
        packet = builder(project, asof, now=now)
        fingerprint = packet["dataset_sha256"]
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("invalid_report_dataset_sha256")
        render_id = rendering_signature(config)
        publication_id = hashlib.sha256(
            (fingerprint + render_id + str(destination)).encode()
        ).hexdigest()
        previous_path = root / "daily" / asof / "latest.json"
        previous = json.loads(previous_path.read_bytes()) if previous_path.exists() else None
        complete = packet.get("status", "partial")
        if complete not in ("complete", "partial"):
            raise ValueError("invalid_report_packet_status")
        if (
            previous
            and previous.get("dataset_sha256") == fingerprint
            and previous.get("rendering_sha256") == render_id
            and previous.get("output") == str(destination)
            and output_valid(previous)
        ):
            publish_index(destination, previous)
            return {**previous, "state": "unchanged"}
        if (
            previous
            and previous.get("status") == "complete"
            and complete == "partial"
            and output_valid(previous)
        ):
            blocked = {
                "state": "partial",
                "asof": asof,
                "reason": "preserved_previous_complete_report",
                "dataset_sha256": fingerprint,
                "generated_at": now.isoformat(),
            }
            write_json(root / "last-attempt.json", blocked)
            return blocked
        run_dir = root / "runs" / asof / publication_id
        run_dir.mkdir(parents=True, exist_ok=True)
        packet_path = run_dir / "packet.json"
        if packet_path.exists():
            saved_packet = json.loads(packet_path.read_bytes())
            if saved_packet.get("dataset_sha256") != fingerprint:
                raise ValueError("existing_packet_mismatch")
        else:
            write_json(packet_path, packet)
        frozen_manifest_path = run_dir / "manifest.json"
        frozen_manifest = (
            json.loads(frozen_manifest_path.read_bytes()) if frozen_manifest_path.exists() else None
        )
        with TemporaryDirectory(prefix="render-", dir=root) as temp:
            staging = Path(temp)
            rendered = (runner or (lambda p, d: render(p, d, config)))(packet_path, staging)
            checked = validate_render(rendered, staging)
            if frozen_manifest:
                for period, row in checked.items():
                    old = frozen_manifest["reports"][period]
                    if digest(row["path"]) != old["sha256"]:
                        raise ValueError("restored_pdf_differs_from_frozen_version")
                    if [digest(p) for p in row["charts"]] != [
                        item["sha256"] for item in old.get("charts", [])
                    ]:
                        raise ValueError("restored_charts_differ_from_frozen_version")
            published = {}
            for period, row in checked.items():
                target = destination / LABELS[period] / row["path"].name
                publish_file(row["path"], target, destination)
                chart_rows = []
                for chart in row["charts"]:
                    chart_target = destination / "チャート" / asof / chart.name
                    publish_file(chart, chart_target, destination)
                    chart_rows.append({"path": str(chart_target), "sha256": digest(chart_target)})
                published[period] = {
                    "path": str(target),
                    "pages": row["pages"],
                    "sha256": digest(target),
                    "charts": chart_rows,
                }
        manifest = {
            "state": "created",
            "status": complete,
            "asof": asof,
            "generated_at": now.isoformat(),
            "dataset_sha256": fingerprint,
            "rendering_sha256": render_id,
            "publication_id": publication_id,
            "output": str(destination),
            "reports": published,
            "packet": str(packet_path),
            "packet_sha256": digest(packet_path),
        }
        if frozen_manifest:
            manifest = {**frozen_manifest, "state": "restored"}
        else:
            write_json(run_dir / "manifest.json", manifest)
        write_json(previous_path, manifest)
        write_json(root / "latest.json", manifest)
        write_json(destination / "最新レポート.json", manifest)
        publish_index(destination, manifest)
        return manifest


def scheduled_allowed(project):
    config = settings(Path(project))
    if config.get("automatic_enabled") is not True:
        return False
    automation = config.get("automation_file")
    if not automation or not Path(automation).is_file():
        return False
    saved = tomllib.loads(Path(automation).read_text(encoding="utf-8"))
    return saved.get("id") == config.get("automation_id") and saved.get("status") == "ACTIVE"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--asof")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.scheduled and not scheduled_allowed(args.project):
            result = {"state": "automation_disabled"}
        else:
            result = run(args.project, asof=args.asof, output=args.output)
    except ValueError as exc:
        if str(exc) == "broker_state_already_open":
            result = {"state": "already_running"}
        else:
            result = {"state": "error", "error_type": type(exc).__name__, "reason": str(exc)[:120]}
    except (OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result = {"state": "error", "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return (
        2 if result.get("state") in ("error", "partial") or result.get("status") == "partial" else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
