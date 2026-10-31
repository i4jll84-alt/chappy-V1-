import json

import httpx
import pytest

from trading_system.free_market import PublicArchive, interpret, public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://www.jpx.co.jp/markets/equities/rights/a.csv",
        "https://www.jpx.co.jp.evil.example/markets/equities/rights/a.csv",
        "https://name:secret@www.taisyaku.jp/data/zandaka.csv",
        "https://www.taisyaku.jp/app/stock",
        "https://www.jpx.co.jp/markets/equities/rights/a.csv?token=x",
    ],
)
def test_only_public_source_paths(url):
    with pytest.raises(ValueError, match="public_url_not_allowed"):
        public_url(url)


def test_cp932_csv_preserves_blanks_markers_and_codes():
    result, links = interpret(
        "https://www.taisyaku.jp/data/shina.csv",
        "コード,料率,説明\n0123,*****,注記\n0124,,\n".encode("cp932"),
    )
    assert result["encoding"] == "cp932"
    assert result["rows"][1:] == [["0123", "*****", "注記"], ["0124", "", ""]]
    assert links == []


def test_links_and_context_from_published_page_only():
    result, links = interpret(
        "https://www.jpx.co.jp/markets/equities/rights/index.html",
        b"""<h3>2026-11-05</h3><table><tr><th colspan="2">Code</th></tr>
        <tr><td>0123</td><td>2:1</td></tr></table>
        <option value="archives-01.html">Archive</option>
        <script>$.ajax({url: '/markets/equities/rights/data.csv'});</script>
        <a href="https://evil.example/x.csv">Bad</a>
        <a href="/other/file.csv">Outside scope</a>""",
    )
    assert result["headings"][0]["text"] == "2026-11-05"
    assert result["tables"][0][0][0]["colspan"] == "2"
    assert set(links) == {
        "https://www.jpx.co.jp/markets/equities/rights/archives-01.html",
        "https://www.jpx.co.jp/markets/equities/rights/data.csv",
    }


def test_versions_failures_and_tamper_are_not_hidden(tmp_path):
    archive = PublicArchive(tmp_path)
    url = "https://www.taisyaku.jp/data/zandaka.csv"
    try:
        for run, body in (
            ("one", b"code,qty\n0123,1\n"),
            ("two", b"code,qty\n0123,2\n"),
            ("three", b"code,qty\n0123,2\n"),
        ):
            archive.record(run, url, None, httpx.Response(200, content=body))
        rows = list(archive.db.execute("SELECT * FROM observations"))
        assert rows[0]["sha256"] != rows[1]["sha256"] == rows[2]["sha256"]
        assert rows[0]["raw_path"] != rows[1]["raw_path"] == rows[2]["raw_path"]
        assert rows[0]["source_published_at"] is None
        assert rows[0]["available_at"] > rows[0]["observed_at"]
        assert archive.audit("one")["state"] == "checked"
        raw = tmp_path / rows[0]["raw_path"]
        raw.write_bytes(b"corrupted")
        assert archive.audit("one")["state"] == "issues_found"
        assert raw.read_bytes() == b"corrupted"
        archive.failure("failed", url, None, "HTTPStatusError", 403)
        assert archive.audit("failed")["issues"][0]["http_status"] == 403
    finally:
        archive.close()


def test_collect_does_not_follow_redirects_or_fetch_third_party(tmp_path):
    seed = "https://www.jpx.co.jp/markets/equities/rights/index.html"
    redirect = "https://www.jpx.co.jp/markets/equities/rights/redirect.csv"
    calls = []

    def serve(request):
        calls.append(str(request.url))
        assert "x-api-key" not in request.headers
        if str(request.url) == seed:
            return httpx.Response(200, text=f'<a href="{redirect}">Download</a>')
        return httpx.Response(302, headers={"location": "https://evil.example/file"})

    archive = PublicArchive(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(serve), follow_redirects=False) as client:
            report = archive.collect(client, interval=0, seeds=[seed])
        assert calls == [seed, redirect]
        assert report["state"] == "issues_found"
        assert report["documents"] == 2
        assert json.loads((tmp_path / "status.json").read_text())["issues"][0]["http_status"] == 302
    finally:
        archive.close()


def test_conditional_refresh_retains_raw_and_checks_integrity(tmp_path):
    url = "https://www.taisyaku.jp/data/zandaka.csv"
    archive = PublicArchive(tmp_path)
    modified = "Fri, 30 Oct 2026 01:52:09 GMT"
    try:
        archive.record(
            "first",
            url,
            None,
            httpx.Response(200, content=b"code,qty\n0123,2\n", headers={"last-modified": modified}),
        )

        def serve(request):
            assert request.headers["if-modified-since"] == modified
            return httpx.Response(304)

        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            report = archive.collect(client, interval=0, seeds=[url])
        assert report["state"] == "checked"
        assert report["unchanged"] == 1 and report["added_or_changed"] == 0
        row = archive.db.execute("SELECT * FROM observations ORDER BY id DESC").fetchone()
        assert row["http_status"] == 304
        (tmp_path / row["raw_path"]).write_bytes(b"corrupt")
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            assert archive.collect(client, interval=0, seeds=[url])["state"] == "issues_found"
    finally:
        archive.close()


def test_throttling_stops_remaining_requests(tmp_path):
    seeds = ["https://www.taisyaku.jp/data/zandaka.csv", "https://www.taisyaku.jp/data/meigara.csv"]
    calls = []

    def serve(request):
        calls.append(str(request.url))
        return httpx.Response(429)

    archive = PublicArchive(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            report = archive.collect(client, interval=0, seeds=seeds)
        assert calls == [seeds[0]]
        assert report["state"] == "issues_found"
    finally:
        archive.close()


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("2026-11-02T07:59:59+09:00", "2026-10-30T08:00:00+09:00"),
        ("2026-11-02T08:00:00+09:00", "2026-11-02T08:00:00+09:00"),
        ("2026-11-02T21:10:00+09:00", "2026-11-02T08:00:00+09:00"),
        ("2026-11-03T02:10:00+09:00", "2026-11-02T08:00:00+09:00"),
        ("2026-10-31T21:10:00+09:00", "2026-10-30T08:00:00+09:00"),
        ("2026-11-01T08:10:00+09:00", "2026-10-30T08:00:00+09:00"),
    ],
)
def test_due_slot_includes_late_start_and_weekends(current, expected):
    from datetime import datetime

    from trading_system.free_market import due_slot

    assert due_slot(datetime.fromisoformat(current)).isoformat() == expected


def test_schedule_requires_successful_run_after_release(tmp_path):
    from datetime import datetime

    from trading_system.free_market import schedule_decision

    moment = datetime.fromisoformat("2026-11-02T21:00:00+09:00")
    path = tmp_path / "report-test.json"

    def save(run, state, completed):
        path.write_text(
            json.dumps({"run": run, "state": state, "checked_at": completed, "issues": []}),
            encoding="utf-8",
        )

    assert schedule_decision(tmp_path, moment)["state"] == "due"
    save("20261101T225900-abcd", "checked", "2026-11-01T23:10:00+00:00")
    assert schedule_decision(tmp_path, moment)["state"] == "due"
    save("20261101T233000-abcd", "issues_found", "2026-11-01T23:40:00+00:00")
    assert schedule_decision(tmp_path, moment)["state"] == "due"
    save("20261101T233000-abcd", "checked", "2026-11-01T23:40:00+00:00")
    assert schedule_decision(tmp_path, moment)["state"] == "already_acquired"
    (tmp_path / "STOP").touch()
    assert schedule_decision(tmp_path, moment)["state"] == "stopped"


def test_scheduled_cli_skips_when_another_writer_is_active(tmp_path, monkeypatch, capsys):
    from trading_system.brokers.locking import single_writer
    from trading_system.free_market import main

    monkeypatch.setattr("sys.argv", ["free_market", "--root", str(tmp_path), "--due"])
    with single_writer(tmp_path / "writer.lock"):
        assert main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "already_running"
    assert not (tmp_path / "public.sqlite").exists()


@pytest.mark.parametrize(
    "name",
    [
        "folder/18_kenri1610.pdf",
        "folder/17_idou2610.pdf",
        "folder/13_kabusu2609.pdf",
        "folder/17_kenri2609-correction.pdf",
        "index.html",
        "00-archives-10.html",
    ],
)
def test_monthly_required_sources(name):
    url = "https://www.jpx.co.jp/markets/statistics-equities/monthly/" + name
    assert public_url(url) == url


@pytest.mark.parametrize(
    "name",
    [
        "folder/18_kenri1609.pdf",
        "folder/18_kenri2613.pdf",
        "folder/01_sokatu2601.pdf",
        "folder/17_kenri2610.exe",
        "other.html",
        "../outside/18_kenri2610.pdf",
    ],
)
def test_monthly_does_not_expand_into_unrelated_or_earlier_data(name):
    url = "https://www.jpx.co.jp/markets/statistics-equities/monthly/" + name
    with pytest.raises(ValueError, match="public_url_not_allowed"):
        public_url(url)


def test_monthly_follows_published_selected_attachments_and_archives_only():
    root = "https://www.jpx.co.jp/markets/statistics-equities/monthly/"
    _, links = interpret(
        root + "index.html",
        b"""
      <option value="00-archives-10.html">2016</option>
      <a href="folder/17_kenri2609.pdf">rights</a>
      <a href="folder/16_idou2609.pdf">changes</a>
      <a href="folder/13_kabusu2609.pdf">shares</a>
      <a href="folder/01_sokatu2609.pdf">other</a>
      <a href="folder/18_kenri1609.pdf">before requested history</a>
    """,
    )
    assert set(links) == {
        root + x
        for x in (
            "00-archives-10.html",
            "folder/17_kenri2609.pdf",
            "folder/16_idou2609.pdf",
            "folder/13_kabusu2609.pdf",
        )
    }
