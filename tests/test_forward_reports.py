import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from pypdf import PdfWriter

from trading_system.research import forward_reports as r


def setup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "desktop" / "reports"
    return project, output


def builder(project, asof, now=None):
    return {
        "asof": asof,
        "status": "complete",
        "dataset_sha256": "a" * 64,
        "generated_at": now.isoformat(),
    }


def renderer(packet, output):
    reports = {}
    for period in r.LABELS:
        path = output / f"{period}.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=200)
        writer.add_metadata({"/Subject": json.loads(packet.read_bytes())["dataset_sha256"]})
        with path.open("wb") as target:
            writer.write(target)
        reports[period] = {"path": str(path), "pages": 1, "charts": []}
    return {"reports": reports}


def invoke(project, output, **kwargs):
    return r.run(
        project,
        output=output,
        asof="2026-11-26",
        now=datetime.fromisoformat("2026-11-26T23:00:00+09:00"),
        builder=kwargs.pop("builder", builder),
        runner=kwargs.pop("runner", renderer),
        **kwargs,
    )


def test_three_reports_repeat_without_rendering_or_packet_rewrite(tmp_path):
    project, output = setup(tmp_path)
    result = invoke(project, output)
    assert result["state"] == "created"
    assert (output / "レポート一覧.html").is_file()
    before = Path(result["packet"]).read_bytes()
    repeat = invoke(project, output, runner=lambda *a: pytest.fail("duplicate rendering"))
    assert repeat["state"] == "unchanged"
    assert Path(result["packet"]).read_bytes() == before
    assert all(Path(row["path"]).is_file() for row in result["reports"].values())


def test_revision_preserves_prior_report_files(tmp_path):
    project, output = setup(tmp_path)
    before = invoke(project, output)
    old = {p: Path(row["path"]).read_bytes() for p, row in before["reports"].items()}

    def revised(*args, **kwargs):
        return {**builder(*args, **kwargs), "dataset_sha256": "b" * 64}

    result = invoke(project, output, builder=revised)
    assert result["dataset_sha256"] == "b" * 64
    for period, row in before["reports"].items():
        relative = Path(row["path"]).relative_to(output)
        saved = (
            output / "変更前のレポート" / hashlib.sha256(old[period]).hexdigest()[:16] / relative
        )
        assert saved.read_bytes() == old[period]


def test_failed_render_does_not_replace_successful_reports(tmp_path):
    project, output = setup(tmp_path)
    result = invoke(project, output)
    before = {row["path"]: Path(row["path"]).read_bytes() for row in result["reports"].values()}

    def changed(*args, **kwargs):
        return {**builder(*args, **kwargs), "dataset_sha256": "b" * 64}

    def bad(*args):
        raise ValueError("synthetic render failure")

    with pytest.raises(ValueError, match="synthetic render failure"):
        invoke(project, output, builder=changed, runner=bad)
    assert all(Path(path).read_bytes() == raw for path, raw in before.items())


def test_partial_packet_does_not_destroy_complete_same_day_report(tmp_path):
    project, output = setup(tmp_path)
    first = invoke(project, output)

    def partial(*args, **kwargs):
        return {**builder(*args, **kwargs), "status": "partial", "dataset_sha256": "b" * 64}

    result = invoke(
        project, output, builder=partial, runner=lambda *a: pytest.fail("render partial")
    )
    assert result["state"] == "partial"
    assert r.output_valid(first)


def test_tampered_pdf_is_regenerated_and_old_packet_retained(tmp_path):
    project, output = setup(tmp_path)
    result = invoke(project, output)
    packet = Path(result["packet"]).read_bytes()
    manifest_path = Path(result["packet"]).with_name("manifest.json")
    frozen = manifest_path.read_bytes()
    Path(result["reports"]["day"]["path"]).write_bytes(b"broken")
    repaired = invoke(project, output)
    assert repaired["state"] == "restored" and r.output_valid(repaired)
    assert Path(repaired["packet"]).read_bytes() == packet
    assert manifest_path.read_bytes() == frozen


def test_output_cannot_be_onedrive(tmp_path):
    with pytest.raises(ValueError, match="onedrive"):
        r.local_output(tmp_path / "OneDrive" / "reports")


def test_all_render_outputs_validated_before_any_publication(tmp_path):
    project, output = setup(tmp_path)

    def bad(packet, staging):
        result = renderer(packet, staging)
        result["reports"]["year"]["pages"] = 2
        return result

    with pytest.raises(ValueError, match="pdf_page_count"):
        invoke(project, output, runner=bad)
    assert not list(output.glob("**/*.pdf"))


def test_scheduled_pause_or_delete_disables_native_task(tmp_path):
    project, output = setup(tmp_path)
    automation = tmp_path / "automation.toml"
    r.write_json(
        project / "data/forward-reports/config.json",
        {
            "output": str(output),
            "automatic_enabled": True,
            "automation_id": "paper-reports",
            "automation_file": str(automation),
        },
    )
    assert not r.scheduled_allowed(project)
    automation.write_text(chr(10).join(['id="paper-reports"', 'status="PAUSED"', ""]))
    assert not r.scheduled_allowed(project)
    automation.write_text(chr(10).join(['id="paper-reports"', 'status="ACTIVE"', ""]))
    assert r.scheduled_allowed(project)


def test_second_report_writer_is_rejected(tmp_path):
    project, output = setup(tmp_path)
    with r.single_writer(project / "data/forward-reports/dispatch"):
        with pytest.raises(ValueError, match="broker_state_already_open"):
            invoke(project, output)


def test_renderer_change_creates_distinct_frozen_version(tmp_path, monkeypatch):
    project, output = setup(tmp_path)
    first = invoke(project, output)
    old_packet = Path(first["packet"]).read_bytes()
    monkeypatch.setattr(r, "rendering_signature", lambda config: "c" * 64)
    second = invoke(project, output)
    assert second["state"] == "created"
    assert first["packet"] != second["packet"]
    assert first["dataset_sha256"] == second["dataset_sha256"]
    assert Path(first["packet"]).read_bytes() == old_packet
