import pytest

from trading_system.research.swing_windows import make_features, outcome


def bar(o=100, c=100, factor=1):
    return {"O": o, "C": c, "AdjFactor": factor, "ExRT": None}


def test_daily_features_and_forward_prices_are_separate():
    past = [bar(c=100 + i) for i in range(21)]
    f, state = make_features(past)
    assert state == "ready" and f["return_20"] == pytest.approx(0.2)
    assert outcome([bar(), bar(o=105)], (100, 101))[0] == pytest.approx((0.05, 0.01))
    assert outcome([bar(), None], (100, 101))[1] == "missing_or_delisted_row"
    assert outcome([bar(), bar(factor=0.5)], (100, 101))[1] == "rights_during_holding"
    assert make_features(past)[0] == f
    past[-1]["ExRT"] = "1"
    assert make_features(past) == (None, "rights_in_feature_window")
