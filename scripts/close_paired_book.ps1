param([Parameter(Mandatory=$true)][string]$WorkbookPath)
$ErrorActionPreference='Stop'
if ([IO.Path]::GetFileName($WorkbookPath) -cnotmatch '^研究用分足-\d{4}-\d{2}-\d{2}\.xlsx$') {throw 'Unexpected workbook'}
$excel=[Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application')
foreach ($book in $excel.Workbooks) {
    if ([string]::Equals($book.FullName,$WorkbookPath,[StringComparison]::OrdinalIgnoreCase)) {
        # The dated workbook is a generated market-data display. Raw data is in SQLite.
        $book.Close($false)
        break
    }
}
