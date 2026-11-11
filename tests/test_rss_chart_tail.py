import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="The RSS reader runs under Windows PowerShell.")
def test_excel_property_reads_and_chart_snapshot():
    reader = Path(__file__).resolve().parents[1] / "scripts/read_research_panel.ps1"
    # Invalid panel returns before attaching to Excel and exposes the production functions.
    scenario = r"""
$ErrorActionPreference = 'Stop'
. $env:RSS_TEST_READER -WorkbookPath unused -Symbols fixture | Out-Null
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class RangeFixture {
    public int Failures, Calls, Code = unchecked((int)0x80010001);
    public object Data;
    public object Formula2 {
        get {
            Calls++;
            if (Failures-- > 0) throw new COMException("sensitive", Code);
            return Data;
        }
    }
}
"@
function Assert-Failure($action, $reason) {
    try { & $action | Out-Null } catch {
        $issue = Get-ReadFailure $_.Exception $stage $affectedSymbol
        if ($issue.reason -ne $reason) {throw "unexpected reason $($issue.reason)"}
        if (($issue | ConvertTo-Json) -match 'sensitive') {throw 'exception leaked'}
        return
    }
    throw "expected failure $reason"
}
$stage='quote_formulas_before'
$affectedSymbol=$null
$range=[RangeFixture]::new()
$range.Failures=1
# Ordinary PowerShell property access loses the COM exception, as in the old reader.
if ($null -ne $range.Formula2 -or $range.Calls -ne 1) {throw 'silent getter repro'}
$range.Failures=1
$range.Data=[Array]::CreateInstance([object], [int[]]@(32,11), [int[]]@(1,1))
$result=Read-Matrix $range 'Formula2' 32 11
if ($result.Rank -ne 2 -or $result.GetLength(0) -ne 32 -or $range.Calls -ne 3 -or
    $retryLog.Count -ne 1 -or $retryLog[0].hresult -ne '0x80010001' -or
    $retryLog[0].stage -ne $stage -or $retryLog[0].property -ne 'Formula2') {throw 'retry evidence'}
$range.Code=[int]0x8001010A
$range.Failures=9
Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_retry_later'
if ($range.Calls -ne 5 -or $retryLog.Count -ne 2) {throw 'per-frame retry limit'}
$retryLog.Clear()
$range.Code=[int]0x800A03EC
Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_unavailable'
if ($range.Calls -ne 6 -or $retryLog.Count) {throw 'permanent failure retried'}
$range.Failures=0
$range.Data=$null
Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_property_unavailable'
foreach($bad in @('scalar', (,[int[]]@(1,2)))) {
    $range.Data=$bad
    Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_range_shape'
}
$range.Data=[Array]::CreateInstance([object], [int[]]@(31,11), [int[]]@(1,1))
Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_range_shape'
$generic=Get-ReadFailure ([Exception]::new('private')) 'quotes' $null
if ($generic.reason -ne 'excel_unavailable' -or $null -ne $generic.hresult) {throw 'generic'}

# Status, header, date column and OHLCV share one Excel Value2 snapshot.
$matrix=[Array]::CreateInstance([object], [int[]]@(7,7), [int[]]@(1,1))
$f='=RssChart(A2:G2,"7203.T","1M",400)'
$shown=$f.Replace('=RssChart','=@RssChart')
$matrix.SetValue("$shown => 配信中",1,1)
if ((Get-ChartStatus ($matrix.GetValue(1,1)) $f) -ne 'streaming') {throw 'live status'}
$offline='マーケットスピード II にログインして「接続中」の状態に変更してから利用してください。'
if ((Get-ChartStatus "$shown => $offline" $f) -ne 'connection_required') {throw 'offline status'}
foreach ($unknown in @($null,'','配信中',"$shown => 配信中断",'=wrong => 配信中')) {
    if ((Get-ChartStatus $unknown $f) -ne 'unknown') {throw 'false status'}
}
for($col=1;$col -le 7;$col++){ $matrix.SetValue($headers[$col-1],2,$col) }
foreach($row in 3,4,6){$matrix.SetValue('2026/11/09',$row,1);$matrix.SetValue($row,$row,7)}
$matrix.SetValue('--------',5,1)
$bars=@(Get-ChartBars $matrix 12 $true)
if ($bars.Count -ne 2 -or $bars[0].Length -ne 7 -or $bars[1][6] -ne 4) {throw 'stale tail'}
if (@(Get-ChartBars $matrix 1 $true)[0][6] -ne 4) {throw 'latest row'}
$matrix.SetValue('--------',3,1)
if (@(Get-ChartBars $matrix 420 $true).Count -ne 0) {throw 'empty chart'}
$matrix.SetValue(46335,3,1)
$matrix.SetValue($null,5,1)
if (@(Get-ChartBars $matrix 420 $true).Count -ne 4) {throw 'no marker, trailing blank'}
$matrix.SetValue('2026/11/09',7,1)
Assert-Failure {Get-ChartBars $matrix 12 $true} 'chart_range_changed'
$matrix.SetValue('changed',2,2)
Assert-Failure {Get-ChartBars $matrix 12 $false} 'chart_header'
if (Test-Formula '=wrong' '=expected') {throw 'invalid formula accepted'}
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
