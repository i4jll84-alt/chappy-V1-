import copy
from datetime import date, timedelta

import httpx
import pytest

from trading_system.research import irbank_first5 as first5
from trading_system.research import irbank_jsf as jsf
from trading_system.research import irbank_priority as m


def technical(root, days=None, pools=None):
    days = days or ["2016-11-17", "2016-11-18", "2021-11-19", "2023-10-31"]
    value = {"sessions": days, "days": pools or {"2016-11-18": [{"code": "72030"}]}}
    jsf.save(root / "technical-candidates.json", value)
    jsf.save(
        root / "acquisition-policy.json",
        {
            "priority_plan": {
                "mode": m.MODE,
                "technical_sha256": m.sha(root / "technical-candidates.json"),
                "first5_targets_sha256": m.signature(m.target_dates(value)),
                "history_targets_sha256": m.signature(m.target_dates(value, "history")),
            }
        },
    )
    return value


def response(code, days, balance=100, status=200):
    return httpx.Response(
        status,
        json={
            "security_code": code,
            "period": "1y",
            "exchange": "XTKS",
            "fields": [{"key": key, "unit": "shares"} for key in jsf.BALANCES],
            "days": [
                dict(
                    date=d,
                    loan_balance=balance,
                    stock_loan_balance=0,
                    net_balance=balance,
                    loan_new=0,
                    loan_repay=0,
                    stock_loan_new=0,
                    stock_loan_repay=0,
                )
                for d in days
            ],
        },
    )


def page(root, code, end, days, **kwargs):
    archive = jsf.Archive(root)
    receipt, rows = archive.write(code, end, response(code, days, **kwargs))
    archive.merge(code, receipt, rows)
    return archive


def test_all_fixed_candidates_and_previous_day_included(tmp_path):
    pools = {
        "2016-11-18": [{"code": f"{i}0"} for i in range(1000, 1008)],
        "2021-11-19": [{"code": "99990"}],
    }
    data = technical(tmp_path, pools=pools)
    assert list(m.target_dates(data)) == [str(i) for i in range(1000, 1008)]
    assert all(v == ["2016-11-17", "2016-11-18"] for v in m.target_dates(data).values())
    assert m.target_dates(data, "history")["9999"] == ["2016-11-18", "2021-11-19"]
    bad = copy.deepcopy(data)
    bad["sessions"].reverse()
    with pytest.raises(jsf.Halt, match="calendar"):
        m.target_dates(bad)
    bad = copy.deepcopy(data)
    bad["days"] = {"2016-11-17": [{"code": "72030"}], "2016-11-19": [{"code": "72030"}]}
    with pytest.raises(jsf.Halt, match="previous_session"):
        m.target_dates(bad)


def test_manifest_drift_is_rejected(tmp_path):
    data = technical(tmp_path)
    assert m.enabled(tmp_path)
    m.check_plan(tmp_path, data)
    data["days"]["2016-11-18"].append({"code": "99990"})
    with pytest.raises(jsf.Halt, match="priority_plan_changed"):
        m.check_plan(tmp_path, data)


def test_coverage_distinguishes_missing_requests_from_unobserved_rows(tmp_path):
    data = technical(tmp_path)
    archive = page(tmp_path, "7203", "2021-11-19", ["2016-11-18"])
    result = m.coverage(archive, "7203", ["2016-11-17", "2016-11-18"])
    assert result["unrequested_dates"] == ["2016-11-17"]
    assert result["observed_dates"] == 1
    assert m.ready_states(tmp_path, data)[1]["state"] == "waiting"
    page(tmp_path, "7203", "2016-11-17", [], status=404)
    states, result = m.ready_states(tmp_path, data)
    assert result["state"] == "ready" and result["unobserved_dates"] == 1
    ranks, evidence = first5.approximate_ranks(tmp_path, data["sessions"], data["days"], states)
    assert ranks["2016-11-18"] == []
    assert evidence["unobserved_tested"][0]["code"] == "72030"


def test_cached_conflict_and_hash_corruption_never_silently_pass(tmp_path):
    archive = page(tmp_path, "7203", "2016-11-18", ["2016-11-17"])
    archive.write("7203", "2021-11-19", response("7203", ["2016-11-17"], balance=101))
    with pytest.raises(jsf.Halt, match="overlap_conflict"):
        m.coverage(archive, "7203", ["2016-11-17"])
    raw, _ = archive.paths("7203", "2016-11-18")
    raw.write_bytes(b"corrupt")
    with pytest.raises(jsf.Halt, match="raw_hash_mismatch"):
        m.coverage(archive, "7203", ["2016-11-17"])


def install_mock(monkeypatch, data, responder):
    calls = []

    class Client:
        def __init__(self, *args):
            self.sent = 0

        def get(self, code, end):
            calls.append((code, end))
            result = responder(code, end, len(calls))
            self.sent += 1
            return result

        def close(self):
            pass

    monkeypatch.setattr(m, "prepare_candidates", lambda *a: data)
    monkeypatch.setattr(
        m,
        "prepare",
        lambda *a: {"codes": list(m.target_dates(data, "history")), "boundary_only_codes": []},
    )
    monkeypatch.setattr(m, "load_key", lambda *a: "fixture")
    monkeypatch.setattr(m, "Client", Client)
    return calls


def test_sparse_response_fallback_resume_and_cache_only_second_run(tmp_path, monkeypatch):
    data = technical(tmp_path)
    calls = install_mock(
        monkeypatch,
        data,
        lambda code, end, n: (
            response(code, ["2016-11-18"]) if n == 1 else response(code, [], status=404)
        ),
    )
    result = m.run(tmp_path, tmp_path)
    assert result["state"] == "complete"
    assert calls == [("7203", "2021-11-19"), ("7203", "2016-11-17")]
    assert m.run(tmp_path, tmp_path)["requests_this_run"] == 0
    assert len(calls) == 2


def test_quota_stop_resumes_without_refetching_saved_page(tmp_path, monkeypatch):
    days = [(date(2016, 11, 17) + timedelta(days=i)).isoformat() for i in range(600)]
    days += ["2021-11-19", "2023-10-31"]
    data = technical(tmp_path, days, {days[1]: [{"code": "72030"}], days[400]: [{"code": "72030"}]})

    def responder(code, end, count):
        if count == 2:
            raise jsf.Halt("run_request_limit")
        i = days.index(end)
        return response(code, days[max(0, i - 251) : i + 1])

    calls = install_mock(monkeypatch, data, responder)
    assert m.run(tmp_path, tmp_path)["reason"] == "run_request_limit"
    saved = calls[0]
    assert m.run(tmp_path, tmp_path)["state"] == "complete"
    assert calls.count(saved) == 1


def test_required_mode_has_identical_signals_to_full_window(tmp_path):
    data = technical(tmp_path, pools={"2016-11-18": [{"code": f"{i}0"} for i in range(1000, 1008)]})
    old = {}
    for i in range(1000, 1008):
        days = ["2016-11-17", "2016-11-18"] if i != 1000 else ["2016-11-17"]
        page(tmp_path, str(i), "2021-11-19", days)
        old[str(i)] = {"pages": ["2021-11-19"]}
    states, ready = m.ready_states(tmp_path, data)
    assert ready["state"] == "ready" and ready["unobserved_dates"] == 1
    legacy = first5.approximate_ranks(tmp_path, data["sessions"], data["days"], old)
    required = first5.approximate_ranks(tmp_path, data["sessions"], data["days"], states)
    assert required == legacy
    assert required[0]["2016-11-18"] == [f"{i}0" for i in range(1001, 1006)]
    states["1000"]["required_dates"] = ["2016-11-18"]
    with pytest.raises(ValueError, match="required_dates_not_covered"):
        first5.approximate_ranks(tmp_path, data["sessions"], data["days"], states)
