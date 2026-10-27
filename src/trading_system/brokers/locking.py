"""Cross-platform OS lock, released on normal close or process death."""

import os
from contextlib import contextmanager


@contextmanager
def single_writer(root):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "writer.lock").open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("broker_state_already_open") from exc
        yield
    finally:
        handle.close()
