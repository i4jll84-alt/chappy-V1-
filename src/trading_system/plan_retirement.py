"""Preserve the first C experiment when its registered PEAD successor is enrolled."""

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
    old = root / "accounts/C"
    folder = root / "retired" / ("C-" + OLD_VERSION)
    archive = folder / "account"
    manifest = folder / "retirement.json"
    if old.exists() and (old / "enrollment.json").is_file():
        enrollment = json.loads((old / "enrollment.json").read_bytes())
        if enrollment["spec"] == spec:
            return
        if enrollment["spec"] != OLD_SPEC or archive.exists():
            raise ValueError("account_terms_changed")
        if not manifest.exists():
            summary_file = root / "summary.json"
            summary = (
                json.loads(summary_file.read_bytes())["plans"].get("C", {})
                if summary_file.exists()
                else {}
            )
            if summary and summary.get("version") != OLD_VERSION:
                raise ValueError("account_terms_changed")
            folder.mkdir(parents=True, exist_ok=True)
            record = dict(
                retired_at=now.isoformat(),
                retired_spec=OLD_SPEC,
                replacement=spec,
                final_report=summary,
                files_sha256=_inventory(old),
            )
            atomic_write(manifest, canonical(record).encode())
        record = json.loads(manifest.read_bytes())
        if record["replacement"] != spec or _inventory(old) != record["files_sha256"]:
            raise ValueError("retirement_inventory_mismatch")
        if (
            old.resolve() != root.resolve() / "accounts" / "C"
            or folder.resolve() != root.resolve() / "retired" / ("C-" + OLD_VERSION)
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
            or record["retired_spec"] != OLD_SPEC
            or _inventory(archive) != record["files_sha256"]
        ):
            raise ValueError("retirement_inventory_mismatch")
