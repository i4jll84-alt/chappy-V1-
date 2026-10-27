"""Paper lifecycle coordinator: one bounded data step between management ticks."""

import sqlite3
from collections import Counter
from datetime import datetime, time, timedelta
from uuid import uuid4

from trading_system.brokers.router import BrokerRouter
from trading_system.execution.risk import TERMINAL, check_risk
from trading_system.execution.store import digest
from trading_system.runtime.handlers import FixedHandlers
from trading_system.runtime.models import DATASETS, Settings, daytime_deadline
from trading_system.runtime.store import JobStore


class Lifecycle:
    def __init__(self, root, settings, router, fixtures, clock):
        if type(router) is not BrokerRouter:
            raise ValueError("stage_A_requires_paper_router")
        self.settings = Settings.model_validate(settings)
        self.clock, self.router = clock, router
        self.handlers = FixedHandlers(fixtures)
        self.now = self.settings.now(clock())
        self.anchor = self.now.date()
        self.scope = router.active.model_dump(mode="json")
        self.run_id = uuid4().hex
        self.purpose = "needs_review"
        self.phase = "restore_and_reconcile"
        self.normal_exit = False
        self.exit_requested = False
        self.save_error = None
        self.reconciliation = False
        self.reconciliation_reason = "not_checked"
        self.account_snapshot = {}
        self.store = JobStore(root, self.now)
        self.candidate_key = None
        try:
            binding = dict(first_day=str(self.settings.first_day), account=self.scope)
            old = self.store.meta("binding")
            if old is not None and old != binding:
                raise ValueError("immutable_lifecycle_account_or_origin")
            # Corrections need a new version; inputs for each definition stay immutable.
            definition = dict(
                settings=self.settings.model_dump(mode="json"),
                fixtures=self.handlers.fixtures.model_dump(mode="json"),
            )
            key = "definition:" + self.settings.definition_version
            prior = self.store.meta(key)
            if prior is not None and prior != digest(definition):
                raise ValueError("changed_definition_requires_new_version")
            with self.store.transaction():
                self.store.set_meta("binding", binding)
                self.store.set_meta(key, digest(definition))
            self._attach_guard()
            self.reconcile()
            self.reconstruct()
            day = self.settings.calendar.at(self.anchor)
            if not day.business or self.now.time() >= time(16):
                self.purpose = "postprocessing"
            elif self.now.time() >= day.closes:
                self.purpose = "preparing_stop"
            else:
                self.purpose = "daytime"
            self.phase = "current_management" if self.purpose == "daytime" else "backlog"
            self.persist()
        except BaseException:
            # Keep the guard closed if initialization failed after attaching it.
            self.purpose = "needs_review"
            self.store.close()
            raise

    def _attach_guard(self):
        if getattr(self.router, "operation_guard", None) is not None:
            raise ValueError("router_already_managed")
        self.router.operation_guard = self.guard
        self.router.adapter.operation_guard = self.guard

    def guard(self, operation, request=None):
        if operation == "switch":
            raise ValueError("close_lifecycle_before_account_switch")
        now = self.settings.now(self.clock())
        day = self.settings.calendar.at(now.date())
        if (
            self.exit_requested
            or self.save_error
            or self.purpose != "daytime"
            or now.date() != self.anchor
            or not day.business
            or now.time() >= day.closes
        ):
            raise ValueError("lifecycle_order_operations_blocked")
        if operation == "submit" and request.action in {"cash_buy", "margin_short"}:
            if not self.reconciliation or not self.data_ready():
                raise ValueError("current_data_or_reconciliation_missing")
        # Existing manager still checks permission, session, resources and uncertainty.
        # Missing short inventory alone never blocks a cover here.

    def reconcile(self):
        try:
            results = self.router.reconcile(self.now)
            self.router._require_known_routes(self.router.adapter, self.now)
            snapshot = self.router.adapter.manager.snapshot()
            unknown = [
                o["client_id"]
                for o in snapshot["orders"]
                if o["state"]
                in {"DISPATCHING", "UNKNOWN_SUBMIT", "CANCEL_REQUESTED", "UNKNOWN_CANCEL"}
            ]
            self.reconciliation = bool(results) and all(results.values()) and not unknown
            self.reconciliation_reason = (
                None if self.reconciliation else "account_or_order_unresolved"
            )
        except Exception as exc:
            # Preserve durable local orders/reservations, not invented remote answers.
            snapshot = self.router.adapter.manager.snapshot()
            unknown = [
                o["client_id"]
                for o in snapshot["orders"]
                if o["state"] not in {"FILLED", "CANCELLED", "REJECTED"}
            ]
            self.reconciliation = False
            self.reconciliation_reason = "reconcile_" + type(exc).__name__
        reserved = [
            dict(
                internal_id=o["client_id"],
                state=o["state"],
                remaining_quantity=o["intent"]["quantity"] - o["filled_quantity"],
                intent=o["intent"],
            )
            for o in snapshot["orders"]
            if o["state"] not in TERMINAL
        ]
        try:
            resources = check_risk(
                self.router.adapter.risk, snapshot["account"], snapshot["orders"], self.now
            )
            resource_reason = None
        except ValueError:
            resources, resource_reason = None, "resource_valuation_unverified_reservations_retained"
        self.account_snapshot = dict(
            reservation_basis=reserved,
            resources=resources,
            resource_reason=resource_reason,
            snapshot=snapshot,
            unresolved_orders=unknown,
            reconciliation_complete=self.reconciliation,
            reason=self.reconciliation_reason,
        )

    def reconstruct(self):
        """Run at startup and every tick, independent of successful shutdown registration."""
        version = self.settings.definition_version
        base = dict(
            model_version=self.settings.model_version,
            definition_hash=self.store.meta("definition:" + version),
            input_period_start=str(self.settings.first_day),
        )
        closed = self.settings.closed_days(self.now)
        with self.store.transaction():
            features = {}
            for day in closed:
                keys = [
                    self.store.ensure(k, day, self.scope, version, [], dict(base)) for k in DATASETS
                ]
                quality = self.store.ensure("quality", day, self.scope, version, keys, dict(base))
                # Chronological feature dependencies prevent gaps across several missing days.
                previous = list(features.values())[-1:]
                feature = self.store.ensure(
                    "features", day, self.scope, version, [quality, *previous], dict(base)
                )
                features[day] = feature
                self.store.ensure("report", day, self.scope, version, [], dict(base))
            today = self.settings.calendar.at(self.now.date())
            if today.business and self.now.time() < today.closes:
                target = today.day
            else:
                target = self.settings.calendar.next_business(today.day)
            try:
                needed = self.settings.calendar.previous_business(target)
            except ValueError:
                needed = None
            self.candidate_key = None
            if needed in features:
                self.candidate_key = self.store.ensure(
                    "candidates", target, self.scope, version, [features[needed]], dict(base)
                )
            self.store.set_meta("current_target", str(target))
        for job in self.store.jobs():
            if (
                job["kind"] == "candidates"
                and job["state"] != "completed"
                and job["key"] != self.candidate_key
            ):
                job.update(state="needs_review", reason="expired_candidate_not_replayed")
                with self.store.transaction():
                    self.store.save(job)
        # Verify every completed publication before it can unblock dependants.
        for job in self.store.jobs():
            if job["state"] == "completed":
                self.store.output(job["key"])

    def data_ready(self):
        if not self.candidate_key:
            return False
        job = self.store.get(self.candidate_key)
        return bool(
            job
            and job["state"] == "completed"
            and job["day"] == str(self.now.date())
            and self.store.output(job["key"])["payload"]["body"]["internal_mock"]
        )

    def step_job(self, *, daytime=False):
        jobs = self.store.jobs()
        current_version = self.settings.definition_version
        # Current preparation precedes reports. Historical candidates are never replayed.
        rank = {
            k: i for i, k in enumerate((*DATASETS, "quality", "features", "candidates", "report"))
        }
        jobs.sort(key=lambda j: (j["kind"] == "report", j["day"], rank[j["kind"]]))
        for job in jobs:
            if job["version"] != current_version or job["state"] in {"completed", "needs_review"}:
                continue
            if job["kind"] == "candidates" and job["key"] != self.candidate_key:
                continue
            if daytime and job["kind"] == "report":
                continue
            if job["next_at"] and datetime.fromisoformat(job["next_at"]) > self.now:
                continue
            if any(self.store.get(k)["state"] != "completed" for k in job["dependencies"]):
                job.update(state="dependency_wait", reason="required_inputs_not_complete")
                with self.store.transaction():
                    self.store.save(job)
                continue
            if job["attempts"] >= self.settings.max_attempts:
                job.update(state="needs_review", reason="retry_limit_reached")
                with self.store.transaction():
                    self.store.save(job)
                continue
            self.store.claim(job, self.now)
            try:
                result = self.handlers.execute(job, self.now, self.store, self.account_snapshot)
            except (OSError, sqlite3.Error):
                raise  # Mandatory persistence failure cannot become an ordinary retry.
            except Exception as exc:
                self.store.finish(
                    job,
                    self.now,
                    "retry_wait",
                    "handler_" + type(exc).__name__,
                    next_at=self.now + timedelta(seconds=self.settings.retry_seconds),
                )
            else:
                next_at = (
                    self.now
                    if result.immediate
                    else self.now + timedelta(seconds=self.settings.retry_seconds)
                )
                self.store.finish(
                    job,
                    self.now,
                    result.state,
                    result.reason,
                    result.output,
                    next_at=next_at if result.state.endswith("_wait") else None,
                )
            return True
        return False

    def status(self):
        try:
            ready = self.data_ready()
        except Exception:
            ready = False
        jobs = self.store.jobs()
        counts = dict(Counter(j["state"] for j in jobs))
        return dict(
            run_id=self.run_id,
            at=self.now.isoformat(),
            purpose=self.purpose,
            phase=self.phase,
            mode="paper",
            evidence="simulated",
            real_execution_enabled=False,
            target_day=self.store.meta("current_target"),
            last_normal_exit=self.store.meta("last_normal_exit"),
            normal_exit=self.normal_exit,
            exit_requested=self.exit_requested,
            save_error=self.save_error,
            jobs=counts,
            unfinished=[
                dict(
                    key=j["key"],
                    kind=j["kind"],
                    day=j["day"],
                    state=j["state"],
                    reason=j["reason"],
                    next_at=j["next_at"],
                )
                for j in jobs
                if j["state"] != "completed"
            ],
            current_data_ready=ready,
            reconciliation_complete=self.reconciliation,
            account=self.account_snapshot,
            restart_conditions=[
                "restore_local_state",
                "reconcile_paper_account",
                "verify_calendar_and_current_inputs",
            ],
            stop_reason=getattr(self, "stop_reason", None),
            stop_delayed=(
                not self.normal_exit and self.exit_requested and self.now.time() >= time(16)
            ),
            delay_reason=(self.save_error or "mandatory_local_save_in_progress")
            if self.exit_requested and not self.normal_exit
            else None,
        )

    def persist(self, *, normal=False):
        self.store.record_status(self.status(), normal=normal)

    def stop(self, reason="daytime_1600"):
        self.exit_requested = True
        self.purpose = "preparing_stop"
        self.phase = "saving"
        self.stop_reason = reason
        self.persist()
        self.reconcile()
        self.reconstruct()
        self.normal_exit = True
        try:
            self.persist(normal=True)
        except BaseException:
            self.normal_exit = False
            raise

    def refresh_clock(self):
        now = self.settings.now(self.clock())
        if now < self.now:
            raise ValueError("clock_moved_backwards")
        self.now = now

    def stop_due(self):
        return self.now.date() != self.anchor or self.now >= daytime_deadline(self.now)

    def tick(self):
        if self.normal_exit or self.save_error:
            return self.report()
        try:
            self.refresh_clock()
            if self.purpose != "postprocessing" and self.stop_due():
                self.stop()
            elif self.purpose == "postprocessing":
                self.phase = "backlog"
                self.reconstruct()
                exhausted = True
                for _ in range(self.settings.postprocess_budget):
                    self.refresh_clock()
                    self.reconstruct()
                    if not self.step_job():
                        exhausted = False
                        break
                self.stop_reason = "postprocess_budget_reached" if exhausted else "no_runnable_jobs"
                self.exit_requested = True
                self.normal_exit = True
                self.persist(normal=True)
            else:
                self.reconcile()  # Always before one bounded data page/calculation.
                self.refresh_clock()
                if self.stop_due():
                    self.stop()
                    return self.report()
                self.reconstruct()
                day = self.settings.calendar.at(self.now.date())
                if self.now.time() >= day.closes:
                    self.purpose, self.phase = "preparing_stop", "saving"
                    self.persist()
                else:
                    self.phase = "current_management"
                    self.step_job(daytime=True)
                    self.refresh_clock()
                    if self.stop_due():
                        self.stop()
                    else:
                        self.persist()
        except Exception as exc:
            self.normal_exit = False
            self.exit_requested = True
            self.purpose = "needs_review"
            self.save_error = "lifecycle_" + type(exc).__name__
            # No recursive save retry, reset, or automatic restart after a local failure.
        return self.report()

    def report(self):
        try:
            return self.status()
        except Exception as exc:
            self.normal_exit = False
            self.exit_requested = True
            self.purpose = "needs_review"
            self.save_error = "status_unavailable_" + type(exc).__name__
            return dict(
                mode="paper",
                evidence="simulated",
                real_execution_enabled=False,
                purpose=self.purpose,
                normal_exit=False,
                exit_requested=True,
                save_error=self.save_error,
                account=self.account_snapshot,
                current_data_ready=False,
                reconciliation_complete=False,
                at=self.now.isoformat(),
                jobs=None,
                restart_conditions=["repair_local_persistence", "reconcile_paper_account"],
            )

    def close(self):
        # Closing this coordinator never removes its gate from a still-open router.
        self.exit_requested = True
        self.store.close()


def run(runtime, sleep, interval=1):
    """No autostart registration; caller starts this paper-only process explicitly."""
    if not 0 < interval <= 5:
        raise ValueError("poll_interval_out_of_bounds")
    while True:
        status = runtime.tick()
        if status["normal_exit"] or status["save_error"]:
            return status
        sleep(interval)
