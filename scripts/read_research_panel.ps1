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
try {
    if($expected.Count -ne 31 -or @($expected|Where-Object {$_ -cnotmatch '^[0-9][A-Z0-9]{3}\.T$'}).Count){throw 'panel'}
    $app=[Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application')
    if(-not $app.Ready){throw 'busy'}
    $book=$null
    foreach($candidate in $app.Workbooks){
        if([string]::Equals($candidate.FullName,$WorkbookPath,[StringComparison]::OrdinalIgnoreCase)){$book=$candidate;break}
    }
    if($null -eq $book -or $book.Sheets.Count -ne 32){throw 'workbook'}
    $quotes=$book.Worksheets.Item('気配').Range('A1:K32')
    $formulas=$quotes.Formula2
    $vals=$quotes.Value2
    $after=$quotes.Formula2
    for($k=0;$k -lt $fields.Count;$k++){if($vals.GetValue(1,$k+2) -cne $fields[$k]){throw 'header'}}
    $result=@()
    for($i=0;$i -lt $expected.Count;$i++){
        $symbol=$expected[$i]
        $row=$i+2
        if($vals.GetValue($row,1) -cne $symbol){throw 'symbol'}
        $q=@()
        for($j=0;$j -lt $fields.Count;$j++){
            $wanted='=RssMarket("'+$symbol+'","'+$fields[$j]+'")'
            if(-not (Test-Formula ($formulas.GetValue($row,$j+2)) $wanted) -or
               -not (Test-Formula ($after.GetValue($row,$j+2)) $wanted)){throw 'formula'}
            $q+=,($vals.GetValue($row,$j+2))
        }
        $ws=$book.Worksheets.Item($symbol)
        $wanted='=RssChart(A2:G2,"'+$symbol+'","1M",400)'
        if(-not (Test-Formula $ws.Range('A1').Formula2 $wanted)){throw 'chart'}
        $header=$ws.Range('A2:G2').Value2
        for($j=0;$j -lt 7;$j++){if($header.GetValue(1,$j+1) -cne $headers[$j]){throw 'chart_header'}}
        $last=$ws.Cells.Item($ws.Rows.Count,1).End(-4162).Row
        $bars=@()
        if($last -ge 3){
            $first=[Math]::Max(3,$last-$Rows+1)
            $matrix=$ws.Range("A${first}:G${last}").Value2
            $bars=@(for($n=1;$n -le $matrix.GetLength(0);$n++){
                $bar=@(for($j=1;$j -le 7;$j++){,($matrix.GetValue($n,$j))})
                ,$bar
            })
        }
        if(-not (Test-Formula $ws.Range('A1').Formula2 $wanted)){throw 'chart'}
        $result+=@{symbol=$symbol;quote=$q;bars=$bars}
    }
    $indexes=@()
    $ix=$book.Worksheets.Item('気配').Range('A36:D37')
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
    # Never emit Excel exception text, other workbook names or credentials.
    $reason=if($_.Exception.Message -in @('panel','busy','workbook','symbol','header','formula','chart','chart_header')){$_.Exception.Message}else{'excel_unavailable'}
    $data=@{state='unavailable';reason=$reason}
}
ConvertTo-Json -InputObject $data -Depth 7 -Compress
