param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$rssProject = Split-Path -Parent $PSScriptRoot
$rssPython = Join-Path $rssProject '.venv\Scripts\python.exe'
$rssWorkbook = Join-Path $env:USERPROFILE 'Documents\RakutenRSS\RSS接続確認.xlsx'

Write-Host 'RSS株価取得（読取・保存・照合のみ）' -ForegroundColor Cyan
Write-Host '事前準備：マーケットスピード IIにログインし、RSS接続確認.xlsxをExcelで開いてください。'
Write-Host 'Excelの表示は「接続中・発注不可」のままにしてください。'
Write-Host '対象：7203.T / 20回取得 / 読取間の待機2秒。常時取得ではありません。'
Write-Host ''

if (-not (Test-Path -LiteralPath $rssPython -PathType Leaf)) {
    Write-Host 'Pythonが見つかりません。プロジェクトの保存場所を確認してください。' -ForegroundColor Red
    return
}
if (-not (Test-Path -LiteralPath $rssWorkbook -PathType Leaf)) {
    Write-Host 'RSS接続確認.xlsxが所定の保存場所に見つかりません。' -ForegroundColor Red
    return
}

Push-Location -LiteralPath $rssProject
try {
    if ($CheckOnly) {
        & $rssPython -m trading_system.rss_workflow --help
        if ($LASTEXITCODE -ne 0) { throw 'Startup check failed' }
        Write-Host '起動確認OK。Excel接続・データ取得は実行していません。' -ForegroundColor Green
        return
    }
    Write-Host '取得を開始します。完了までパソコンをスリープさせないでください。'
    & $rssPython -m trading_system.rss_workflow --workbook $rssWorkbook --code 7203.T --samples 20 --interval 2
    $rssExitCode = $LASTEXITCODE
    switch ($rssExitCode) {
        0 { Write-Host '取得・保存・照合が完了しました。' -ForegroundColor Green }
        3 { Write-Host '注意項目があります。上のreportに示された実行結果を確認してください。' -ForegroundColor Yellow }
        default { Write-Host '処理に失敗しました。上のstageとreportを確認してください。' -ForegroundColor Red }
    }
    Write-Host '結果の保存先：data\rss-workflows（プロジェクト内）'
    Write-Host 'この結果はRSSの鮮度や模擬売買の成功を保証するものではありません。'
} catch {
    Write-Host '起動または実行に失敗しました。設定の確認が必要です。' -ForegroundColor Red
} finally {
    Pop-Location
}
Write-Host ''
Write-Host '処理は終了しました。このウィンドウは閉じてかまいません。'
