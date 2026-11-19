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
$testSleeps=[Collections.Generic.List[int]]::new()
function Start-Sleep([int]$Milliseconds) { $testSleeps.Add($Milliseconds) }
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
$range.Failures=2
$result=Read-Matrix $range 'Formula2' 32 11
if ($range.Calls -ne 6 -or $retryLog.Count -ne 3 -or $retryState.delay_ms -ne 800 -or
    $retryLog[1].attempt -ne 1 -or $retryLog[2].attempt -ne 2) {throw 'per-property backoff'}
$retryLog.Clear();$retryState.delay_ms=0;$testSleeps.Clear();$range.Calls=0
$range.Failures=99
Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_retry_later'
if ($range.Calls -ne 5 -or $retryLog.Count -ne 4 -or $retryState.delay_ms -ne 1800 -or
    ($testSleeps -join ',') -ne '200,400,600,600') {throw 'bounded snapshot sleep budget'}
Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_retry_later'
if ($range.Calls -ne 6 -or $retryLog.Count -ne 4) {throw 'exhausted budget retried'}
$retryLog.Clear();$retryState.delay_ms=0
$range.Code=[int]0x800A03EC
Assert-Failure {Read-Matrix $range 'Formula2' 32 11} 'excel_unavailable'
if ($range.Calls -ne 7 -or $retryLog.Count -or $retryState.delay_ms) {
    throw 'permanent failure retried'
}
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

# An optional index failure must retain validated quotes/bars and its own issue.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class IndexFixture {
    public int Calls, FailAt;
    public object Formulas, Values;
    public object Read(object value) {
        if (++Calls == FailAt) throw new COMException("sensitive", unchecked((int)0x800A03EC));
        return value;
    }
    public object Formula2 { get { return Read(Formulas); } }
    public object Value2 { get { return Read(Values); } }
}
public class IndexSheetFixture {
    public IndexFixture Index = new IndexFixture();
    public object Range(string address) {
        if (address != "A36:D37") throw new Exception("wrong index range");
        return Index;
    }
}
"@
$sheet=[IndexSheetFixture]::new()
$fm=[Array]::CreateInstance([object], [int[]]@(2,4), [int[]]@(1,1))
$vm=[Array]::CreateInstance([object], [int[]]@(2,4), [int[]]@(1,1))
$codes=@('N225','TOPX');$fields=@('現在日付','更新時刻','現在値')
for($i=0;$i -lt 2;$i++){
    $vm.SetValue($codes[$i],$i+1,1)
    for($j=0;$j -lt 3;$j++){
        $fm.SetValue(('=RssIndexMarket("'+$codes[$i]+'","'+$fields[$j]+'")'),$i+1,$j+2)
        $vm.SetValue(@('2026/11/19','13:30:00',12345)[$j],$i+1,$j+2)
    }
}
$sheet.Index.Formulas=$fm;$sheet.Index.Values=$vm
foreach($failAt in 0,1,2,3){
    $sheet.Index.Calls=0;$sheet.Index.FailAt=$failAt
    $data=@{state='read';items=@(@{symbol='9360.T';quote=@(2525);bars=@(@(2525,100))});
        chart_issue=@{reason='chart_range_changed'}}
    $before=$data.items | ConvertTo-Json -Depth 7 -Compress
    Add-IndexSnapshot $data $sheet 32
    $after=$data.items | ConvertTo-Json -Depth 7 -Compress
    if ($data.state -ne 'read' -or $before -cne $after -or
        $data.chart_issue.reason -ne 'chart_range_changed') {throw 'validated stock data discarded'}
    if ($failAt) {
        if ($data.indexes.Count -ne 0 -or $data.index_issue.stage -ne 'indexes' -or
            $data.index_issue.reason -ne 'excel_unavailable' -or
            ($data | ConvertTo-Json -Depth 7) -match 'sensitive') {
            throw 'index failure not isolated'
        }
    } elseif ($data.indexes.Count -ne 2 -or $data.index_issue -or
        $data.indexes[1].code -ne 'TOPX') {throw 'valid indexes lost'}
}
$sheet.Index.Calls=0;$sheet.Index.FailAt=0
$fm.SetValue('=wrong',1,2)
$data=@{state='read';items=@()}
Add-IndexSnapshot $data $sheet 32
if ($data.indexes.Count -ne 1 -or $data.indexes[0].code -ne 'TOPX') {throw 'invalid index accepted'}
$sheet.Index.Values='invalid range'
$data=@{state='read';items=@(@{symbol='9360.T'})}
Add-IndexSnapshot $data $sheet 32
if ($data.index_issue.reason -ne 'excel_range_shape' -or $data.items.Count -ne 1) {
    throw 'index shape'
}
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
