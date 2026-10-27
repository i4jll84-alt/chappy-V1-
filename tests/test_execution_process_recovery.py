"""Exercise persistent mock state across genuinely separate Python processes."""

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


def run_demo(state_root, output):
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_system.cli",
            "execution-mock",
            "--state-root",
            str(state_root),
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_mock_demo_restores_state_across_processes_without_duplicate_dispatch(tmp_path):
    state_root = tmp_path / "state"
    first = run_demo(state_root, tmp_path / "first.json")
    second = run_demo(state_root, tmp_path / "second.json")
    assert first["mode"] == second["mode"] == "mock"
    assert first["real_execution_enabled"] is False
    assert second["real_execution_enabled"] is False
    assert first["broker_orders"] == second["broker_orders"]
    assert first["orders"] == second["orders"]
    assert first["broker_orders"]
    assert all(order["send_attempt_count"] == 1 for order in second["broker_orders"])
    assert first["account"] == second["account"] == second["broker_account"]
    assert Decimal(second["account"]["cash"]) == Decimal("89995.2")
    assert second["account"]["positions"] == {"MOCK-A": 100}
    assert len(second["events"]) >= len(first["events"])
    assert second["events"][: len(first["events"])] == first["events"]
    assert (state_root / "manager.sqlite").is_file()
    assert (state_root / "broker.sqlite").is_file()


def test_process_exit_between_broker_acceptance_and_local_ack_never_resends(tmp_path):
    setup = """
import os, sys
from pathlib import Path
from trading_system.execution.demo import NOW, mock_config, mock_intent
from trading_system.execution.mock import MockGateway
from trading_system.execution.service import ExecutionManager
root = Path(sys.argv[1])
config = mock_config()
gateway = MockGateway(root / 'broker.sqlite', config)
manager = ExecutionManager(root / 'manager.sqlite', config, gateway)
manager.prepare(mock_intent('process-interrupted'), NOW)
original_submit = gateway.submit
def accept_then_exit(intent, **kwargs):
    original_submit(intent, **kwargs)
    os._exit(0)
gateway.submit = accept_then_exit
manager.dispatch('process-interrupted', NOW)
raise AssertionError('The process should have exited before local acknowledgement')
"""
    recovery = """
import sys
from pathlib import Path
from trading_system.execution.demo import NOW, mock_config
from trading_system.execution.mock import MockGateway
from trading_system.execution.service import ExecutionManager
root = Path(sys.argv[1])
config = mock_config()
gateway = MockGateway(root / 'broker.sqlite', config)
manager = ExecutionManager(root / 'manager.sqlite', config, gateway)
try:
    assert manager.store.order('process-interrupted')['state'] == 'UNKNOWN_SUBMIT'
    assert manager.snapshot()['halt']['uncertainty']
    try:
        manager.dispatch('process-interrupted', NOW)
    except ValueError:
        pass
    else:
        raise AssertionError('Interrupted submission was resent')
    assert manager.reconcile('process-interrupted', NOW)['state'] == 'ACKNOWLEDGED'
    assert gateway.snapshot('process-interrupted')['send_attempt_count'] == 1
finally:
    manager.close()
    gateway.close()
"""
    for script in (setup, recovery):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / "crash")],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
