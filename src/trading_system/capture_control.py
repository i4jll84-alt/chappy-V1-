"""Cooperative collector restarts, separate from operator STOP markers."""

import json
from datetime import datetime, timedelta

from trading_system.storage.store import atomic_write


def request_restart(root, moment):
    request = {
        "requested_at": moment.isoformat(),
        "expires_at": (moment + timedelta(minutes=2)).isoformat(),
    }
    atomic_write(root / "health-restart.json", json.dumps(request).encode(), sharing_retries=20)


def restart_requested(root, launched_ns, moment):
    try:
        value = json.loads((root / "health-restart.json").read_bytes())
        requested = datetime.fromisoformat(value["requested_at"])
        expires = datetime.fromisoformat(value["expires_at"])
        return (
            requested.utcoffset() is not None
            and expires.utcoffset() is not None
            and requested <= moment < expires <= requested + timedelta(minutes=2)
            and launched_ns < int(requested.timestamp() * 1_000_000_000)
        )
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return False
