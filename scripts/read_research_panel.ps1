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
function Get-ChartLastRow($dateColumn) {
    # RssChart starts in row 3 and may leave older cells below its first end marker.
    $lastDataRow = 2
    foreach ($dateValue in $dateColumn) {
        if ('--------' -ceq $dateValue) { break }
        $lastDataRow++
    }
    return $lastDataRow
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
            'symbol','header','formula','chart','chart_header','chart_range_changed')) {
        $reason = $exception.Message
    } elseif ($code -in @('0x80010001','0x8001010B')) {
        $reason = 'excel_call_rejected'
    } elseif ($code -eq '0x8001010A') {
        $reason = 'excel_retry_later'
    } elseif ($stage -eq 'attach' -and $code -eq '0x800401E3') {
        $reason = 'excel_not_registered'
    }
    # Only controlled labels, a configured symbol and the numeric COM code leave the reader.
    return @{state='unavailable'; reason=$reason; stage=$stage; symbol=$affectedSymbol; hresult=$code}
}
$stage='panel'
$affectedSymbol=$null
try {
    if($expected.Count -ne 31 -or @($expected|Where-Object {$_ -cnotmatch '^[0-9][A-Z0-9]{3}\.T$'}).Count){throw 'panel'}
    $stage='attach'
    $app=[Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application')
    $stage='ready'
    if(-not $app.Ready){throw 'busy'}
    $stage='find_workbook'
    $book=$null
    foreach($candidate in $app.Workbooks){
        if([string]::Equals($candidate.FullName,$WorkbookPath,[StringComparison]::OrdinalIgnoreCase)){$book=$candidate;break}
    }
    if($null -eq $book){throw 'workbook_not_found'}
    $stage='sheet_count'
    if($book.Sheets.Count -ne 32){throw 'sheet_count'}
    $stage='quotes'
    $worksheets=$book.Worksheets
    $quotes=$worksheets.Item('気配').Range('A1:K32')
    $formulas=$quotes.Formula2
    $vals=$quotes.Value2
    $after=$quotes.Formula2
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
        $ws=$worksheets.Item($symbol)
        $anchor=$ws.Range('A1')
        $wanted='=RssChart(A2:G2,"'+$symbol+'","1M",400)'
        if(-not (Test-Formula $anchor.Formula2 $wanted)){throw 'chart'}
        $stage='chart_header'
        $header=$ws.Range('A2:G2').Value2
        for($j=0;$j -lt 7;$j++){if($header.GetValue(1,$j+1) -cne $headers[$j]){throw 'chart_header'}}
        $stage='chart_boundary'
        $bottom=$ws.Cells.Item($ws.Rows.Count,1).End(-4162).Row
        $last=2
        if($bottom -ge 3){
            $last=Get-ChartLastRow ($ws.Range("A3:A${bottom}").Value2)
        }
        $bars=@()
        if($last -ge 3){
            $first=[Math]::Max(3,$last-$Rows+1)
            $stage='chart_values'
            $matrix=$ws.Range("A${first}:G${last}").Value2
            # If the live output shrank during the read, discard this mixed snapshot.
            $stage='chart_boundary_after'
            if((Get-ChartLastRow ($ws.Range("A3:A${last}").Value2)) -lt $last){throw 'chart_range_changed'}
            $bars=@(for($n=1;$n -le $matrix.GetLength(0);$n++){
                $bar=@(for($j=1;$j -le 7;$j++){,($matrix.GetValue($n,$j))})
                ,$bar
            })
        }
        $stage='chart_formula_after'
        if(-not (Test-Formula $anchor.Formula2 $wanted)){throw 'chart'}
        $result+=@{symbol=$symbol;quote=$q;bars=$bars}
    }
    $stage='indexes'
    $affectedSymbol=$null
    $indexes=@()
    $ix=$worksheets.Item('気配').Range('A36:D37')
    $ifm=$ix.Formula2;$iv=$ix.Value2;$ia=$ix.Formula2
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
ConvertTo-Json -InputObject $data -Depth 7 -Compress
