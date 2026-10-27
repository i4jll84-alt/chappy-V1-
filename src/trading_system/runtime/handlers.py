"""Bounded fixed-response handlers. No HTTP, credentials, Excel or model training.

Production collectors retain their existing stores. This Stage A harness only
consumes explicitly simulated responses and uses the existing quality validators.
Features/candidates are internal mocks, never strategy signals or sendable orders.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from trading_system.brokers.models import ContractInput
from trading_system.execution.store import digest
from trading_system.market_financial import validate_market_financial
from trading_system.point_in_time.policy import aware_utc
from trading_system.reference import validate_reference
from trading_system.runtime.models import DATASETS
from trading_system.validation.prices import validate_price


class Response(ContractInput):
    evidence: Literal["simulated"] = "simulated"
    available_at: datetime
    target_day: str
    complete: StrictBool = True
    empty_reason: Literal["no_disclosure"] | None = None
    published_at: datetime | None = None
    expected_keys: tuple[str, ...]
    pages: tuple[tuple[dict, ...], ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded(self):
        aware_utc(self.available_at)
        if self.published_at is not None:
            aware_utc(self.published_at)
        if sum(len(p) for p in self.pages) > 10000:
            raise ValueError("fixed_response_too_large")
        if len(self.expected_keys) != len(set(self.expected_keys)):
            raise ValueError("duplicate_expected_keys")
        return self


class Fixtures(ContractInput):
    evidence: Literal["simulated"] = "simulated"
    responses: dict[str, tuple[Response, ...]] = Field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    state: str
    reason: str | None = None
    output: dict | None = None
    immediate: bool = False


def row_key(kind, row):
    return str(
        row.get("DiscNo")
        if kind == "financials"
        else row.get("Date")
        if kind == "topix"
        else row.get("Code")
    )


class FixedHandlers:
    def __init__(self, fixtures):
        self.fixtures = Fixtures.model_validate(fixtures)

    def execute(self, job, now, store, account_snapshot):
        kind = job["kind"]
        if kind in DATASETS:
            return self.acquire(job, now, store)
        dependencies = [store.output(key)["payload"] for key in job["dependencies"]]
        if kind == "quality":
            issues = []
            for data in dependencies:
                dataset, day = data["dataset"], job["day"]
                for row in data["rows"]:
                    if dataset == "prices":
                        found = validate_price(row)
                    elif dataset == "master":
                        found = validate_reference(dataset, row, {"date": day})
                    else:
                        params = {"from": day, "to": day} if dataset == "topix" else {"date": day}
                        found = validate_market_financial(dataset, row, params, now)
                    issues.extend(found)
            if any(i["severity"] in {"error", "warning"} for i in issues):
                return Result("needs_review", "quality_validation_failed")
            body = dict(quality_passed=True, issues=issues)
        elif kind in {"features", "candidates"}:
            # Explicit mock work product: do not fit or change the existing model.
            body = dict(
                internal_mock=True,
                actionable=False,
                target_day=job["day"],
                model_version=job["inputs"]["model_version"],
                values=[],
                reason="fixed_offline_lifecycle_validation",
            )
        elif kind == "report":
            body = dict(
                account_snapshot=account_snapshot,
                aggregation="snapshot_not_additive",
                snapshot_at=now.isoformat(),
                historical_day_accounting_verified=False,
            )
        else:
            return Result("needs_review", "unsupported_job")
        return Result(
            "completed",
            output=dict(
                evidence="simulated",
                count=1,
                body=body,
                obtained_at=now.isoformat(),
                published_at=None,
                inputs=job["inputs"],
            ),
        )

    def acquire(self, job, now, store):
        responses = self.fixtures.responses.get(job["kind"] + ":" + job["day"], ())
        eligible = [r for r in responses if r.available_at <= now]
        if not eligible:
            return Result("publication_wait", "no_available_response")
        response = max(eligible, key=lambda r: r.available_at)
        response_hash = digest(response.model_dump(mode="json"))
        cp = job["checkpoint"]
        # Keep an in-flight snapshot consistent; a provider change is not a continuation page.
        if cp and cp["response_hash"] != response_hash:
            pinned = next(
                (r for r in responses if digest(r.model_dump(mode="json")) == cp["response_hash"]),
                None,
            )
            if pinned is None:
                return Result("needs_review", "checkpoint_input_missing")
            response = pinned
            response_hash = cp["response_hash"]
        if response.target_day != job["day"]:
            return Result("needs_review", "target_day_mismatch")
        if response.published_at is not None and response.published_at > now:
            return Result("publication_wait", "publication_time_in_future")
        if not response.complete:
            return Result("publication_wait", "incomplete_response")
        page = cp.get("page", 0)
        rows = list(cp.get("rows", []))
        observations = list(cp.get("page_observations", []))
        if page < len(response.pages):
            rows.extend(response.pages[page])
            observations.append(dict(page=page, obtained_at=now.isoformat()))
            page += 1
            store.checkpoint(
                job,
                dict(
                    response_hash=response_hash,
                    page=page,
                    rows=rows,
                    page_observations=observations,
                ),
            )
        if page < len(response.pages):
            return Result("retry_wait", "page_checkpoint_saved", immediate=True)
        if not rows:
            if not (
                job["kind"] == "financials"
                and response.empty_reason == "no_disclosure"
                and not response.expected_keys
            ):
                return Result("publication_wait", "empty_not_verified")
        keys = [row_key(job["kind"], row) for row in rows]
        if len(keys) != len(set(keys)) or set(keys) != set(response.expected_keys):
            return Result("needs_review", "range_count_or_duplicate_mismatch")
        date_field = "DiscDate" if job["kind"] == "financials" else "Date"
        if any(row.get(date_field) != job["day"] for row in rows):
            return Result("needs_review", "row_target_day_mismatch")
        return Result(
            "completed",
            output=dict(
                dataset=job["kind"],
                target_day=job["day"],
                evidence="simulated",
                rows=rows,
                count=len(rows),
                empty_reason=response.empty_reason,
                obtained_at=now.isoformat(),
                published_at=(response.published_at.isoformat() if response.published_at else None),
                content_hash=digest(rows),
                page_observations=observations,
                response_hash=response_hash,
                inputs=job["inputs"],
            ),
        )
