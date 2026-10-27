"""Chronological experiment boundaries and a local append-only trial registry.

Indexes refer to an ordered session sequence. Labels end inclusively; windows
are half-open. This module does not select parameters or fit a model.
"""

import hashlib
import json
from pathlib import Path


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def walk_forward_splits(
    session_count: int,
    train_size: int,
    test_size: int,
    *,
    final_size: int,
    gap: int = 0,
    label_end: list[int] | None = None,
):
    """Expanding training windows, disjoint tests and an untouched final tail.

    A gap excludes sessions immediately preceding each test/final window.
    Training labels reaching the test boundary are purged. No future training
    samples are used, so a post-test embargo is not applicable to these folds.
    """
    values = (session_count, train_size, test_size, final_size, gap)
    if any(type(v) is not int for v in values):
        raise ValueError("Session counts must be integers")
    if min(values[:4]) <= 0 or gap < 0:
        raise ValueError("Positive window sizes and a nonnegative gap are required")
    final_start = session_count - final_size
    if train_size + gap + test_size > final_start - gap:
        raise ValueError("Insufficient sessions for training, test, gap and final holdout")
    ends = list(range(session_count)) if label_end is None else label_end
    if len(ends) != session_count or any(
        type(end) is not int or end < i or end >= session_count for i, end in enumerate(ends)
    ):
        raise ValueError("Invalid inclusive label end indexes")
    folds = []
    purged_test = []
    start = train_size + gap
    development_end = final_start - gap
    while start + test_size <= development_end:
        train = [i for i in range(start - gap) if ends[i] < start]
        if not train:
            raise ValueError("Purging removed all training samples")
        test = [i for i in range(start, start + test_size) if ends[i] < development_end]
        purged_test.extend(i for i in range(start, start + test_size) if i not in test)
        if not test:
            raise ValueError("Purging removed all development test samples")
        folds.append({"train": train, "test": test})
        start += test_size
    final_train = [i for i in range(final_start - gap) if ends[i] < final_start]
    if not final_train:
        raise ValueError("Purging removed all final training samples")
    return {
        "policy": "expanding_chronological_purged_v1",
        "gap_sessions": gap,
        "folds": folds,
        "final_train": final_train,
        "final_test": list(range(final_start, session_count)),
        "purged_development_test": purged_test,
        "unused_development_tail": list(range(start, development_end)),
    }


class ExperimentRegistry:
    """Local single-writer registry; a consumed final evaluation is not retried.

    The caller freezes the complete candidate input (including strategy
    parameters) in the manifest. Files are immutable by convention and exclusive
    creation. Manual filesystem edits are outside this local integrity boundary.
    """

    def __init__(self, root: Path, manifest: dict):
        if not isinstance(manifest, dict) or not isinstance(manifest.get("final_candidate"), dict):
            raise ValueError("A manifest with an explicit final_candidate object is required")
        self.manifest = canonical_bytes(manifest)
        self.experiment_id = hashlib.sha256(self.manifest).hexdigest()
        self.root = Path(root) / self.experiment_id
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "manifest.json"
        try:
            with path.open("xb") as handle:
                handle.write(self.manifest)
        except FileExistsError:
            if path.read_bytes() != self.manifest:
                raise ValueError("Experiment manifest integrity mismatch") from None

    def record_trial(self, parameters: dict, result: dict):
        """Retain successful, rejected and failed development trials alike."""
        if (self.root / "final_claim.json").exists():
            raise ValueError("Development trials are frozen after final evaluation is claimed")
        payload = canonical_bytes({"parameters": parameters, "result": result})
        number = 1
        while True:
            path = self.root / f"trial-{number:06d}.json"
            try:
                with path.open("xb") as handle:
                    handle.write(payload)
                return number
            except FileExistsError:
                number += 1

    def evaluate_final(self, candidate: dict, evaluate):
        """Consume the final holdout before calling evaluate, including failures.

        Reusing this exact manifest never grants another final call. Creating a
        new manifest is not a statistical reset: cross-experiment reuse of the
        same market period still requires human governance.
        """
        if (
            not isinstance(candidate, dict)
            or candidate != json.loads(self.manifest)["final_candidate"]
        ):
            raise ValueError("Final candidate must match the frozen manifest")
        claim = canonical_bytes({"candidate": candidate, "status": "consumed"})
        try:
            with (self.root / "final_claim.json").open("xb") as handle:
                handle.write(claim)
        except FileExistsError:
            raise ValueError("Final holdout has already been consumed") from None
        try:
            result = evaluate()
            payload = canonical_bytes({"status": "complete", "result": result})
        except BaseException as exc:
            # Exception messages can contain data or secrets; retain only type.
            payload = canonical_bytes({"status": "failed", "error_type": type(exc).__name__})
            with (self.root / "final_result.json").open("xb") as handle:
                handle.write(payload)
            raise
        with (self.root / "final_result.json").open("xb") as handle:
            handle.write(payload)
        return result
