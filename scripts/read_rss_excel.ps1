# Application-side read-only RSS probe. Requires Windows PowerShell/.NET Framework.
# Only attaches to a registered running Excel instance. Never opens a workbook,
# writes properties, evaluates formulas, runs macros, saves, closes or quits Excel.
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$WorkbookPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Read-Column {
    param($Matrix)
    if ($Matrix -isnot [System.Array] -or $Matrix.Rank -ne 2 -or
        $Matrix.GetLength(0) -ne 3 -or $Matrix.GetLength(1) -ne 1) {
        throw 'Unexpected range shape'
    }
    $firstRow = $Matrix.GetLowerBound(0)
    $firstColumn = $Matrix.GetLowerBound(1)
    return ,@(
        $Matrix.GetValue($firstRow, $firstColumn)
        $Matrix.GetValue($firstRow + 1, $firstColumn)
        $Matrix.GetValue($firstRow + 2, $firstColumn)
    )
}

$rssResult = [ordered]@{ schema_version = 1; status = 'error'; reason = 'invalid_target' }
$rssExcel = $null
$rssBook = $null
$rssSheet = $null
$rssRange = $null
$rssBooks = $null
$rssCandidate = $null
try {
    if ($WorkbookPath -notmatch '^[A-Za-z]:[\\/]' -or
        [System.IO.Path]::GetExtension($WorkbookPath) -ine '.xlsx') {
        throw 'Invalid local target'
    }
    $rssTarget = [System.IO.Path]::GetFullPath($WorkbookPath)
    $rssResult.reason = 'excel_not_registered'
    $rssExcel = [System.Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application')
    $rssResult.reason = 'excel_not_ready'
    if (-not $rssExcel.Ready) { throw 'Excel not ready' }
    $rssResult.reason = 'target_not_in_attached_instance'
    $rssBooks = $rssExcel.Workbooks
    if ($rssBooks.Count -gt 32) { throw 'Too many workbooks' }
    for ($rssIndex = 1; $rssIndex -le $rssBooks.Count; $rssIndex++) {
        $rssCandidate = $rssBooks.Item($rssIndex)
        if ([string]::Equals([string]$rssCandidate.FullName, $rssTarget,
                [System.StringComparison]::OrdinalIgnoreCase)) {
            $rssBook = $rssCandidate
            $rssCandidate = $null
            break
        }
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($rssCandidate)
        $rssCandidate = $null
    }
    if ($null -eq $rssBook) { throw 'Target not found' }
    $rssResult.reason = 'unexpected_sheet_layout'
    if ($rssBook.Sheets.Count -ne 1 -or $rssBook.Worksheets.Count -ne 1) {
        throw 'Unexpected sheet count'
    }
    $rssSheet = $rssBook.Worksheets.Item('Sheet1')
    $rssRange = $rssSheet.Range('A1:A3')
    $rssResult.reason = 'excel_read_failed'
    $rssFormulasBefore = Read-Column $rssRange.Formula2
    $rssValuesBefore = Read-Column $rssRange.Value2
    $rssValuesAfter = Read-Column $rssRange.Value2
    $rssFormulasAfter = Read-Column $rssRange.Formula2
    $rssResult = [ordered]@{
        schema_version = 1
        status = 'read'
        formulas_before = $rssFormulasBefore
        values_before = $rssValuesBefore
        formulas_after = $rssFormulasAfter
        values_after = $rssValuesAfter
    }
} catch {
    # Do not expose exception text, account information or other workbook paths.
} finally {
    foreach ($rssObject in @($rssRange, $rssSheet, $rssCandidate, $rssBook, $rssBooks, $rssExcel)) {
        if ($null -ne $rssObject -and [System.Runtime.InteropServices.Marshal]::IsComObject($rssObject)) {
            try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($rssObject) } catch { }
        }
    }
}
ConvertTo-Json -InputObject $rssResult -Depth 6 -Compress
