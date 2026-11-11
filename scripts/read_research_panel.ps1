# Read an already-open research workbook. Never create Excel, run macros, or place orders.
param([Parameter(Mandatory=$true)][string]$WorkbookPath,
      [Parameter(Mandatory=$true)][string]$Symbols,
      [ValidateSet(12,420)][int]$Rows=12)
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
            if ($issue.reason -notin @('excel_call_rejected','excel_retry_later') -or
                $retryLog.Count -ge 2) {throw}
            $retryLog.Add(@{stage=$stage; property=$name; symbol=$affectedSymbol; hresult=$issue.hresult})
            Start-Sleep -Milliseconds (200 * $retryLog.Count)
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
$stage='panel'
$affectedSymbol=$null
$retryLog=[Collections.Generic.List[object]]::new()
try {
    if($expected.Count -ne 31 -or @($expected|Where-Object {$_ -cnotmatch '^[0-9][A-Z0-9]{3}\.T$'}).Count){throw 'panel'}
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
    if((Get-ExcelProperty $sheets 'Count') -ne 32){throw 'sheet_count'}
    $worksheets=Get-ExcelProperty $book 'Worksheets'
    $stage='quotes'
    $quoteSheet=Get-ExcelProperty $worksheets 'Item' @('気配')
    $quotes=Get-ExcelProperty $quoteSheet 'Range' @('A1:K32')
    $stage='quote_formulas_before'
    $formulas=Read-Matrix $quotes 'Formula2' 32 11
    $stage='quote_values'
    $vals=Read-Matrix $quotes 'Value2' 32 11
    $stage='quote_formulas_after'
    $after=Read-Matrix $quotes 'Formula2' 32 11
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
        $stage='chart_formula_before'
        $ws=Get-ExcelProperty $worksheets 'Item' @($symbol)
        $anchor=Get-ExcelProperty $ws 'Range' @('A1')
        $wanted='=RssChart(A2:G2,"'+$symbol+'","1M",400)'
        if(-not (Test-Formula (Get-ExcelProperty $anchor 'Formula2') $wanted)){throw 'chart'}
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
        $result+=@{symbol=$symbol;quote=$q;bars=$bars;chart_status=$chartStatus}
    }
    $stage='indexes'
    $affectedSymbol=$null
    $indexes=@()
    $ix=Get-ExcelProperty $quoteSheet 'Range' @('A36:D37')
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
    $data=@{state='read';items=$result;indexes=$indexes}
} catch {
    $data=Get-ReadFailure $_.Exception $stage $affectedSymbol
}
if($retryLog.Count){$data.property_retries=$retryLog.ToArray()}
ConvertTo-Json -InputObject $data -Depth 7 -Compress
