import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from trading_system.research import swing_daily
from trading_system.research.swing_daily import snapshot, verify_pages


def test_daily_cross_section_cannot_publish_duplicates_or_modified_raw(tmp_path):
    day = "2026-11-02"
    row = {
        "Date": day,
        "Code": "72030",
        **{
            f: 1
            for f in (
                "O",
                "H",
                "L",
                "C",
                "Vo",
                "Va",
                "AdjFactor",
                "AdjO",
                "AdjH",
                "AdjL",
                "AdjC",
                "AdjVo",
            )
        },
    }

    class Client:
        def get_page(self, params):
            body = {"data": [row]}
            self.last_response_bytes = json.dumps(body).encode()
            return body

    path, n = snapshot(tmp_path, day, Client())
    assert n == 1
    with pytest.raises(ValueError, match="daily_master_code_mismatch"):
        verify_pages(path.parent, json.loads(path.read_bytes()), day, {"11110"})

    class Offline:
        def get_page(self, *args):
            pytest.fail("cached snapshot fetched again")

    assert snapshot(tmp_path, day, Offline()) == (path, 1)
    m = json.loads(path.read_bytes())
    p = path.parent / m["pages"][0]["file"]
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash_mismatch"):
        verify_pages(path.parent, m, day)
    body = json.dumps({"data": [row, row]}).encode()
    p.write_bytes(body)
    m["pages"][0].update(sha256=hashlib.sha256(body).hexdigest(), rows=2)
    with pytest.raises(ValueError, match="invalid_daily_identity"):
        verify_pages(path.parent, m, day)


def test_wait_only_until_latest_page_can_be_used(tmp_path, monkeypatch):
    start = datetime(2026, 11, 13, 7, 58, tzinfo=UTC)

    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)
        now = staticmethod(lambda tz: start)

    monkeypatch.setattr(swing_daily, "datetime", Clock)
    delays = []
    monkeypatch.setattr(swing_daily, "sleep", delays.append)
    path = tmp_path / "manifest.json"

    def pages(*offsets):
        path.write_text(
            json.dumps(
                {
                    "pages": [
                        {"available_at": (start + timedelta(seconds=s)).isoformat()}
                        for s in offsets
                    ]
                }
            )
        )

    pages(-1, 0.885)
    swing_daily.wait_for_daily_pages(path)
    assert delays == [0.885]
    pages(-1, 0)
    swing_daily.wait_for_daily_pages(path)
    assert delays == [0.885]
    pages(3600)
    with pytest.raises(ValueError, match="prices_not_yet_available"):
        swing_daily.wait_for_daily_pages(path)
