"""Archive retired paper accounts together with their final status and file hashes."""

import hashlib
import json
from pathlib import Path

from trading_system.storage.store import atomic_write, canonical

OLD_VERSION = "c1-pullback-guarded-v1"
NEW_VERSION = "pead-dip3-atr4-h60-v1"
OLD_SPEC = {
    "id": "C",
    "adapter": "plan_c_forward",
    "version": OLD_VERSION,
    "capital": 3_000_000,
    "start": "2026-11-24",
}


def _inventory(book):
    return {
        p.relative_to(book).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(book.rglob("*"))
        if p.is_file()
    }


def retire_previous_c(root: Path, spec, now):
    """Called under the forward writer lock. Never replace an unknown or archived book."""
    if (
        spec["id"] != "C"
        or spec["adapter"] != "plan_c_pead_forward"
        or spec["version"] != NEW_VERSION
    ):
        return
    _archive(root, OLD_SPEC, spec, now)


def _archive(root, old_spec, spec, now):
    key = old_spec["id"]
    old = root / "accounts" / key
    folder = root / "retired" / (key + "-" + old_spec["version"])
    archive = folder / "account"
    manifest = folder / "retirement.json"
    if old.exists() and (old / "enrollment.json").is_file():
        enrollment = json.loads((old / "enrollment.json").read_bytes())
        if enrollment["spec"] == spec:
            return
        if enrollment["spec"] != old_spec or archive.exists():
            raise ValueError("account_terms_changed")
        if not manifest.exists():
            summary_file = root / "summary.json"
            summary = (
                json.loads(summary_file.read_bytes())["plans"].get(key, {})
                if summary_file.exists()
                else {}
            )
            if summary and summary.get("version") != old_spec["version"]:
                raise ValueError("account_terms_changed")
            folder.mkdir(parents=True, exist_ok=True)
            record = dict(
                retired_at=now.isoformat(),
                retired_spec=old_spec,
                replacement=spec,
                final_report=summary,
                files_sha256=_inventory(old),
            )
            atomic_write(manifest, canonical(record).encode())
        record = json.loads(manifest.read_bytes())
        if record["replacement"] != spec or _inventory(old) != record["files_sha256"]:
            raise ValueError("retirement_inventory_mismatch")
        if (
            old.resolve() != root.resolve() / "accounts" / key
            or folder.resolve() != root.resolve() / "retired" / (key + "-" + old_spec["version"])
        ):
            raise ValueError("unexpected_retirement_path")
        old.rename(archive)
    elif old.is_dir() and any(old.iterdir()):
        raise ValueError("account_enrollment_missing")
    if archive.exists() or manifest.exists():
        if not archive.is_dir() or not manifest.is_file():
            raise ValueError("incomplete_account_retirement")
        record = json.loads(manifest.read_bytes())
        if (
            record["replacement"] != spec
            or record["retired_spec"] != old_spec
            or _inventory(archive) != record["files_sha256"]
        ):
            raise ValueError("retirement_inventory_mismatch")


def retire_fixed_comparisons(root, plans, now):
    for plan in plans:
        if plan["id"] in ("A9", "A10") and plan["stage"] == "retired":
            _archive(root, {key: plan[key] for key in OLD_SPEC}, None, now)


def retire_for_v21(project, root, plans, now):
    from trading_system.brokers.locking import single_writer
    from trading_system.research import a0_5

    if not any(
        p.get("id") == "B" and p.get("adapter") == "plan_b_v21" and p.get("stage") == "active"
        for p in plans
    ):
        return
    replacement = next({k: p[k] for k in OLD_SPEC} for p in plans if p["id"] == "B")
    old_b = dict(
        id="B",
        adapter="plan_b_forward",
        version="a0-5-guarded-v1",
        capital=3000000,
        start="2026-11-19",
    )
    with single_writer(a0_5.root_for(project)):
        path = a0_5.root_for(project, old_b["capital"]) / "RETIRED"
        if not path.exists():
            atomic_write(
                path, canonical(dict(version=old_b["version"], retired_at=now.isoformat())).encode()
            )
        _archive(root, old_b, replacement, now)
    for p in plans:
        if p.get("stage") == "retired" and p.get("id") == "C" and p.get("version") == NEW_VERSION:
            _archive(root, {k: p[k] for k in OLD_SPEC}, None, now)
