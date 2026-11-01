"""A Start can acknowledge only the RSS stop marker seen by that operator action."""

import os
from hashlib import file_digest
from time import sleep
from uuid import uuid4


def publish_stop(root):
    """Retry brief Windows sharing conflicts; report a persistent failure to the caller."""
    root.mkdir(parents=True, exist_ok=True)
    stop = root / "STOP"
    token = uuid4().hex
    for attempt in range(51):
        try:
            stop.write_text(token, encoding="ascii")
            return
        except PermissionError:
            if attempt == 50:
                raise
            sleep(0.02)


def stop_ticket(path, before_ns=None):
    try:
        with path.open("rb") as stream:
            stamp = os.fstat(stream.fileno()).st_mtime_ns
            if before_ns is not None and stamp >= before_ns:
                return "none"
            digest = file_digest(stream, "sha256").hexdigest()
            if os.fstat(stream.fileno()).st_mtime_ns != stamp:
                return "none"
    except FileNotFoundError:
        return "none"
    # Windows file timestamps have 100 ns resolution, also used by PowerShell.
    return f"{stamp // 100}:{digest}"


def _restore_stop(claim, stop):
    # Publish without replacing a newer STOP. On failure retain the durable claim.
    try:
        if os.name == "nt":
            os.rename(claim, stop)  # Windows refuses to replace an existing destination.
            return
        os.link(claim, stop)
    except FileExistsError:
        pass
    claim.unlink()


def can_start(root, ticket):
    """Called with the collector lock held, before the first daytime read."""
    stop = root / "STOP"
    claim = root / "STOP.resuming"
    if claim.exists():
        _restore_stop(claim, stop)
    if not stop.exists():
        return True
    if not ticket or ticket == "none" or stop_ticket(stop) != ticket:
        return False

    # After the rename all subsequent writers target STOP, never this claim.
    stop.rename(claim)
    if stop_ticket(claim) != ticket:
        _restore_stop(claim, stop)
        return False
    claim.unlink()
    return not stop.exists()
