import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="The RSS reader runs under Windows PowerShell.")
def test_chart_end_marker_limits_the_live_range():
    reader = Path(__file__).resolve().parents[1] / "scripts/read_research_panel.ps1"
    # An invalid symbol list returns before touching Excel and leaves the real functions in scope.
    scenario = """
$ErrorActionPreference = 'Stop'
. $env:RSS_TEST_READER -WorkbookPath unused -Symbols fixture | Out-Null
$column = [Array]::CreateInstance([object], [int[]]@(5, 1), [int[]]@(1, 1))
$values = @('2026/11/06', '2026/11/06', '--------', '2026/11/05', '--------')
for ($i = 0; $i -lt $values.Count; $i++) { $column.SetValue($values[$i], $i + 1, 1) }
if ((Get-ChartLastRow $column) -ne 4) { throw 'stale tail' }
if ((Get-ChartLastRow @('--------', '2026/11/05')) -ne 2) { throw 'no active rows' }
if ((Get-ChartLastRow '2026/11/06') -ne 3) { throw 'one cell' }
if ((Get-ChartLastRow @(46332, 46333)) -ne 4) { throw 'no end marker' }
"""
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-Command",
            scenario,
        ],
        env={**os.environ, "RSS_TEST_READER": str(reader)},
        check=True,
        timeout=20,
    )
