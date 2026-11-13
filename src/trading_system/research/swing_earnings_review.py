"""Score independently annotated earnings risks against matched machine fills."""

from collections import Counter
from decimal import Decimal as D


class EarningsReview:
    def __init__(self, accounts):
        self.arms = {}
        for key in accounts:
            if key.startswith("machine_"):
                self.arms[key] = dict(
                    open={},
                    avoided_loss=D(0),
                    missed_gain=D(0),
                    closed=0,
                    flat=0,
                    flagged_buys=0,
                    unfilled=0,
                    notes=Counter(),
                    agent_observations=Counter(),
                    reasons=Counter(),
                    shadow=None,
                    baseline=None,
                    observed_days=0,
                )

    def update(self, close, pre, agent):
        annotation = (pre or {}).get("earnings", {}).get("annotations", {})
        for key, book in self.arms.items():
            arm = key.split("_")[1]
            group = (agent or {}).get("answer", {}).get("groups", {}).get(arm, {}) or {}
            ratings = group.get("earnings_review", {})
            if not isinstance(ratings, dict):
                ratings = {}
            issues = (agent or {}).get("earnings_issues", {}).get(arm, {})
            for code in (pre or {}).get("machine", {}).get(key, {}).get("buy", []):
                book["notes"][annotation.get(code, {}).get("state", "unavailable")] += 1
                assessment = issues.get(code)
                if assessment is None:
                    assessment = ratings.get(code, {}).get("state", "no_agent_annotation")
                book["agent_observations"][assessment] += 1
            book["reasons"][close["coverage"][key]] += 1
            if pre:
                for reason in pre.get("machine", {}).get(key, {}).get("skips", {}).values():
                    book["reasons"][reason] += 1
                if pre.get("signal", {}).get("credit_complete") is False and arm == "plus3":
                    book["reasons"]["credit_unavailable_for_selection"] += 1
            for trade in close["fills"][key]:
                code = trade["code"]
                if trade["status"] != "simulated":
                    book["reasons"][trade["status"]] += 1
                    if (
                        trade["action"] == "buy"
                        and annotation.get(code, {}).get("state") == "during_hold"
                    ):
                        book["unfilled"] += 1
                    continue
                costs = D(trade["fee"]) + D(trade["slippage"])
                notional = D(trade["open"]) * trade["quantity"]
                if (
                    trade["action"] == "buy"
                    and annotation.get(code, {}).get("state") == "during_hold"
                ):
                    book["open"][code] = notional + costs
                    book["flagged_buys"] += 1
                if trade["action"] == "sell" and code in book["open"]:
                    net = notional - costs - book["open"].pop(code)
                    book["closed"] += 1
                    book["flat"] += int(net == 0)
                    book["missed_gain"] += max(net, D(0))
                    book["avoided_loss"] += max(-net, D(0))
            alt = close.get("earnings_counterfactual", {}).get(key)
            book["shadow"] = alt["metrics"] if alt else None
            book["baseline"] = close["metrics"][key]
            if alt:
                book["observed_days"] += 1

    def report(self):
        result = {}
        for key, b in self.arms.items():
            shadow, base = b["shadow"], b["baseline"]
            closed = b["closed"]
            result[key] = dict(
                states=dict(b["notes"]),
                agent_states=dict(b["agent_observations"]),
                counts_by_reason=dict(b["reasons"]),
                flagged_filled=b["flagged_buys"],
                not_filled=b["unfilled"],
                pending=len(b["open"]),
                closed=closed,
                flat=b["flat"],
                avoided_loss_yen=str(b["avoided_loss"]) if closed else None,
                missed_gain_yen=str(b["missed_gain"]) if closed else None,
                matched_omission_net_yen=str(b["avoided_loss"] - b["missed_gain"])
                if closed
                else None,
                shadow_days=b["observed_days"],
                shadow_net_yen=shadow["net"] if shadow else None,
                shadow_max_dd_pct=shadow["max_dd_pct"] if shadow else None,
                baseline_max_dd_pct=base["max_dd_pct"] if base else None,
                portfolio_difference_yen=str(D(shadow["net"]) - D(base["net"]))
                if shadow and base
                else None,
            )
        return result
