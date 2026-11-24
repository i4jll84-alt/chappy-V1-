# Read an already-open research workbook. Never create Excel, run macros, or place orders.
param([Parameter(Mandatory=$true)][string]$WorkbookPath,
      [Parameter(Mandatory=$true)][string]$Symbols,
      [ValidateSet(12,420)][int]$Rows=12,
      [ValidateRange(-1,1)][int]$Batch=-1)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$expected=@($Symbols.Split(','))
$fields=@('現在日付','現在値','現在値詳細時刻','最良買気配値','最良売気配値','最良買気配数量','最良売気配数量','最良買気配詳細時刻','最良売気配詳細時刻','出来高')
$headers=@('日付','時刻','始値','高値','安値','終値','出来高')
function Test-Formula($actual,$expectedText) {
    if ($actual -isnot [string]) {return $false}
    return (($actual -creplace '^=@?(?:_xll\.)?','=') -ceq $expectedText)
}
function Get-ReadFailure($exception, $stage, $affectedSymbol) {
    $reason = 'excel_unavailable'
    $code = $null
    for ($cause = $exception; $null -ne $cause; $cause = $cause.InnerException) {
        if ($cause -is [Runtime.InteropServices.COMException]) {
            $code = '0x{0:X8}' -f $cause.HResult
        }
    }
    if ($exception.Message -in @('panel','busy','workbook_not_found','sheet_count',
            'symbol','header','formula','chart','chart_header','chart_range_changed',
            'excel_property_unavailable','excel_range_shape')) {
        $reason = $exception.Message
    } elseif ($code -in @('0x80010001','0x8001010B')) {
        $reason = 'excel_call_rejected'
    } elseif ($code -eq '0x8001010A') {
        $reason = 'excel_retry_later'
    } elseif ($stage -eq 'attach' -and $code -eq '0x800401E3') {
        $reason = 'excel_not_registered'
    }
    return @{state='unavailable'; reason=$reason; stage=$stage; symbol=$affectedSymbol; hresult=$code}
}
# Cached dynamic COM call sites propagate HRESULTs. Reflection on every cell-range
# property is slow enough to delay the next market observation.
Add-Type -ReferencedAssemblies Microsoft.CSharp,System.Core -TypeDefinition @"
public static class RakutenExcelRead {
    public static void Clear(dynamic target) { target.ClearContents(); }
    public static void Formula(dynamic target, string value) { target.Formula2 = value; }

    public static object Property(dynamic target, string name) {
        switch (name) {
            case "Ready": return target.Ready;
            case "Workbooks": return target.Workbooks;
            case "Count": return target.Count;
            case "FullName": return target.FullName;
            case "Sheets": return target.Sheets;
            case "Worksheets": return target.Worksheets;
            case "Row": return target.Row;
            case "Formula2": return target.Formula2;
            case "Value2": return target.Value2;
            default: throw new System.ArgumentException("unknown Excel property");
        }
    }
}
"@
function Get-ExcelProperty($target, [string]$name, [object[]]$arguments) {
    if ($null -eq $target) {throw 'excel_property_unavailable'}
    $attempt=0
    while ($true) {
        try {
            # PowerShell's ordinary property getter can turn a COM exception into $null.
            if ($arguments) {
                $value = $target.$name($arguments[0])
            } else {
                $value = [RakutenExcelRead]::Property($target,$name)
            }
            if ($null -eq $value) {throw 'excel_property_unavailable'}
            return ,$value
        } catch {
            $issue = Get-ReadFailure $_.Exception $stage $affectedSymbol
            # Back off per property, with a shared 1.8 s sleep budget per snapshot.
            # A previous property's recovered error must not consume all retries.
            $delay=[Math]::Min(600,200 * ($attempt+1))
            if ($issue.reason -notin @('excel_call_rejected','excel_retry_later') -or
                $retryState.delay_ms + $delay -gt 1800) {throw}
            $attempt++
            $retryState.delay_ms += $delay
            $retryLog.Add(@{stage=$stage; property=$name; symbol=$affectedSymbol;
                hresult=$issue.hresult; attempt=$attempt; delay_ms=$delay})
            Start-Sleep -Milliseconds $delay
        }
    }
}
function Invoke-ExcelMutation($target, [string]$operation, [string]$value) {
    # Only idempotent operations on a prevalidated research chart are permitted.
    if ($operation -notin @('ClearContents','Formula2')) {throw 'chart'}
    $attempt=0
    while ($true) {
        try {
            if ($operation -eq 'ClearContents') {[RakutenExcelRead]::Clear($target)}
            else {[RakutenExcelRead]::Formula($target,$value)}
            return
        } catch {
            $issue=Get-ReadFailure $_.Exception $stage $affectedSymbol
            $delay=[Math]::Min(600,200 * ($attempt+1))
            if ($issue.reason -notin @('excel_call_rejected','excel_retry_later') -or
                $retryState.delay_ms + $delay -gt 1800) {throw}
            $attempt++
            $retryState.delay_ms += $delay
            $retryLog.Add(@{stage=$stage; property=$operation; symbol=$affectedSymbol;
                hresult=$issue.hresult; attempt=$attempt; delay_ms=$delay})
            Start-Sleep -Milliseconds $delay
        }
    }
}
function Read-Matrix($range, $property, $height, $width) {
    $matrix = Get-ExcelProperty $range $property
    if ($matrix -isnot [Array] -or $matrix.Rank -ne 2 -or
        $matrix.GetLowerBound(0) -ne 1 -or $matrix.GetLowerBound(1) -ne 1 -or
        $matrix.GetLength(0) -ne $height -or $matrix.GetLength(1) -ne $width) {
        throw 'excel_range_shape'
    }
    return ,$matrix
}
function Get-ChartStatus($value, $formula) {
    if ($value -isnot [string]) {return 'unknown'}
    $parts = $value -split '=>',2
    if ($parts.Count -ne 2 -or -not (Test-Formula $parts[0].Trim() $formula)) {return 'unknown'}
    switch -CaseSensitive ($parts[1].Trim()) {
        '配信中' {return 'streaming'}
        'マーケットスピード II にログインして「接続中」の状態に変更してから利用してください。' {return 'connection_required'}
        default {return 'unknown'}
    }
}
function Get-ChartBars($matrix, $limit, $hasGuardRow) {
    for ($col=1; $col -le 7; $col++) {
        if ($matrix.GetValue(2,$col) -cne $headers[$col-1]) {throw 'chart_header'}
    }
    $last = 2
    for ($row=3; $row -le $matrix.GetLength(0); $row++) {
        $day = $matrix.GetValue($row,1)
        if ('--------' -ceq $day) {break}
        if ($null -ne $day -and '' -cne [string]$day) {$last=$row}
    }
    if ($hasGuardRow -and $last -eq $matrix.GetLength(0)) {throw 'chart_range_changed'}
    for ($row=[Math]::Max(3,$last-$limit+1); $row -le $last; $row++) {
        $bar = @(for ($col=1; $col -le 7; $col++) {,($matrix.GetValue($row,$col))})
        ,$bar
    }
}
function Add-IndexSnapshot($data, $quoteSheet, $last) {
    $stage='indexes'
    $affectedSymbol=$null
    $data.indexes=@()
    try {
        $indexes=@()
        $ix=Get-ExcelProperty $quoteSheet 'Range' @("A$($last+4):D$($last+5)")
        $ifm=Read-Matrix $ix 'Formula2' 2 4
        $iv=Read-Matrix $ix 'Value2' 2 4
        $ia=Read-Matrix $ix 'Formula2' 2 4
        $ixCodes=@('N225','TOPX');$ixFields=@('現在日付','更新時刻','現在値')
        for($i=0;$i -lt 2;$i++){
            $key=$ixCodes[$i];$ok=$iv.GetValue($i+1,1) -ceq $key
            $snapshot=@()
            for($j=0;$j -lt 3;$j++){
                $wanted='=RssIndexMarket("'+$key+'","'+$ixFields[$j]+'")'
                $ok=$ok -and (Test-Formula ($ifm.GetValue($i+1,$j+2)) $wanted) -and (Test-Formula ($ia.GetValue($i+1,$j+2)) $wanted)
                $snapshot+=,($iv.GetValue($i+1,$j+2))
            }
            if($ok){$indexes+=@{code=$key;fields=$snapshot}}
        }
        $data.indexes=$indexes
    } catch {
        # A failed optional index read must not discard validated stock quotes/bars.
        $data.index_issue=Get-ReadFailure $_.Exception $stage $affectedSymbol
    }
}
$stage='panel'
$affectedSymbol=$null
$retryLog=[Collections.Generic.List[object]]::new()
$retryState=@{delay_ms=0}
try {
    $paired=$Batch -ge 0
    if(((-not $paired) -and $expected.Count -ne 31) -or $expected.Count -lt 31 -or $expected.Count -gt 61 -or
       @($expected|Where-Object {$_ -cnotmatch '^[0-9][A-Z0-9]{3}\.T$'}).Count -or
       @($expected | Sort-Object -Unique).Count -ne $expected.Count){throw 'panel'}
    $slots=$expected.Count
    if($paired -and $slots -gt 50){$slots=[int][Math]::Ceiling($slots/2)}
    $first=[Math]::Max(0,$Batch)*$slots
    if($first -ge $expected.Count){throw 'panel'}
    $last=$expected.Count+1
    $stage='attach'
    $app=[Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application')
    $stage='ready'
    if(-not (Get-ExcelProperty $app 'Ready')){throw 'busy'}
    $stage='find_workbook'
    $books=Get-ExcelProperty $app 'Workbooks'
    $count=Get-ExcelProperty $books 'Count'
    $book=$null
    for($i=1;$i -le $count;$i++){
        $candidate=Get-ExcelProperty $books 'Item' @($i)
        if([string]::Equals((Get-ExcelProperty $candidate 'FullName'),$WorkbookPath,[StringComparison]::OrdinalIgnoreCase)){$book=$candidate;break}
    }
    if($null -eq $book){throw 'workbook_not_found'}
    $stage='sheet_count'
    $sheets=Get-ExcelProperty $book 'Sheets'
    if((Get-ExcelProperty $sheets 'Count') -ne ($slots+1)){throw 'sheet_count'}
    $worksheets=Get-ExcelProperty $book 'Worksheets'
    $stage='quotes'
    $quoteSheet=Get-ExcelProperty $worksheets 'Item' @('気配')
    $quotes=Get-ExcelProperty $quoteSheet 'Range' @("A1:K$last")
    $stage='quote_formulas_before'
    $formulas=Read-Matrix $quotes 'Formula2' $last 11
    $stage='quote_values'
    $vals=Read-Matrix $quotes 'Value2' $last 11
    $stage='quote_formulas_after'
    $after=Read-Matrix $quotes 'Formula2' $last 11
    $stage='quote_identity'
    for($k=0;$k -lt $fields.Count;$k++){if($vals.GetValue(1,$k+2) -cne $fields[$k]){throw 'header'}}
    $result=@()
    for($i=0;$i -lt $expected.Count;$i++){
        $symbol=$expected[$i]
        $affectedSymbol=$symbol
        $stage='quote_identity'
        $row=$i+2
        if($vals.GetValue($row,1) -cne $symbol){throw 'symbol'}
        $q=@()
        for($j=0;$j -lt $fields.Count;$j++){
            $wanted='=RssMarket("'+$symbol+'","'+$fields[$j]+'")'
            if(-not (Test-Formula ($formulas.GetValue($row,$j+2)) $wanted) -or
               -not (Test-Formula ($after.GetValue($row,$j+2)) $wanted)){throw 'formula'}
            $q+=,($vals.GetValue($row,$j+2))
        }
        $result+=@{symbol=$symbol;quote=$q;bars=@();chart_status='not_polled'}
    }
    $chartIssue=$null
    try {
        $pending=@()
        for($i=$first;$i -lt [Math]::Min($expected.Count,$first+$slots);$i++){
            $symbol=$expected[$i]
            $affectedSymbol=$symbol
            $stage='chart_formula_before'
            $sheetName=if($paired){"分足$($i-$first+1)"}else{$symbol}
            $ws=Get-ExcelProperty $worksheets 'Item' @($sheetName)
            $anchor=Get-ExcelProperty $ws 'Range' @('A1')
            $wanted='=RssChart(A2:G2,"'+$symbol+'","1M",400)'
            $current=Get-ExcelProperty $anchor 'Formula2'
            # A collector restart can leave a slot between clearing and rebinding.
            if(-not (Test-Formula $current $wanted)){
                $otherIndex=if($first){$i-$first}else{$i+$slots}
                if(-not $paired){throw 'chart'}
                if($current -ne ''){
                    if($otherIndex -ge $expected.Count){throw 'chart'}
                    $prior='=RssChart(A2:G2,"'+$expected[$otherIndex]+'","1M",400)'
                    if(-not (Test-Formula $current $prior)){throw 'chart'}
                }
                # Clear the dedicated sheet's old output before rebinding. Only the
                # prevalidated chart formula is written. No broker/order calls.
                $stage='chart_release'
                Invoke-ExcelMutation $anchor 'ClearContents'
                $sheetEnd=Get-ExcelProperty $ws 'Range' @('A1048576')
                $bottom=Get-ExcelProperty (Get-ExcelProperty $sheetEnd 'End' @(-4162)) 'Row'
                if($bottom -gt 2){
                    $stage='chart_clear_output'
                    Invoke-ExcelMutation (Get-ExcelProperty $ws 'Range' @("A3:G$bottom")) 'ClearContents'
                }
                $stage='chart_subscribe'
                Invoke-ExcelMutation $anchor 'Formula2' $wanted
                if(-not (Test-Formula (Get-ExcelProperty $anchor 'Formula2') $wanted)){throw 'chart'}
                $result[$i].chart_status='subscribing'
                continue
            }
            $pending+=@{index=$i;sheet=$ws;anchor=$anchor;formula=$wanted}
        }
        foreach($p in $pending){
            $i=$p.index
            $affectedSymbol=$expected[$i]
            $ws=$p.sheet
            $wanted=$p.formula
            $anchor=$p.anchor
            $stage='chart_boundary'
            $sheetEnd=Get-ExcelProperty $ws 'Range' @('A1048576')
            $usedEnd=Get-ExcelProperty $sheetEnd 'End' @(-4162)
            $bottom=Get-ExcelProperty $usedEnd 'Row'
            # Include a guard row to detect growth after End(xlUp). Header, end marker and
            # OHLCV all come from one Value2 call, including when the live range shrinks.
            $endRow=[Math]::Min(1048576,[Math]::Max(2,$bottom)+1)
            $stage='chart_snapshot'
            $range=Get-ExcelProperty $ws 'Range' @("A1:G${endRow}")
            $matrix=Read-Matrix $range 'Value2' $endRow 7
            $chartStatus=Get-ChartStatus ($matrix.GetValue(1,1)) $wanted
            $bars=@(Get-ChartBars $matrix $Rows ($endRow -gt $bottom))
            $stage='chart_formula_after'
            if(-not (Test-Formula (Get-ExcelProperty $anchor 'Formula2') $wanted)){throw 'chart'}
            $result[$i].chart_status=$chartStatus
            if(-not $paired -or $chartStatus -eq 'streaming'){$result[$i].bars=$bars}
        }
    } catch {
        if(-not $paired){throw}
        # All quote identities, fields and values already passed validation.
        # Retain that snapshot even if an independent chart operation failed.
        $chartIssue=Get-ReadFailure $_.Exception $stage $affectedSymbol
    }
    $data=@{state='read';items=$result;indexes=@()}
    if($chartIssue){$data.chart_issue=$chartIssue}
    Add-IndexSnapshot $data $quoteSheet $last
} catch {
    $data=Get-ReadFailure $_.Exception $stage $affectedSymbol
}
if($retryLog.Count){$data.property_retries=$retryLog.ToArray()}
ConvertTo-Json -InputObject $data -Depth 7 -Compress
