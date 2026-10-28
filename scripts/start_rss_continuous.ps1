param(
    [ValidateSet('Start','Stop','Status','Check')][string]$Action = 'Start',
    [string]$DataDirectory = 'data\rss-continuous',
    [int]$Cycles = 0
)
$ErrorActionPreference = 'Stop'
$rssProject = Split-Path -Parent $PSScriptRoot
$rssPython = Join-Path $rssProject '.venv\Scripts\python.exe'
$rssWorkbook = Join-Path $env:USERPROFILE 'Documents\RakutenRSS\RSS接続確認.xlsx'
$rssData = [System.IO.Path]::GetFullPath((Join-Path $rssProject $DataDirectory))
$rssAllowed = [System.IO.Path]::GetFullPath((Join-Path $rssProject 'data')) + '\'
if (-not $rssData.StartsWith($rssAllowed, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '保存先はプロジェクトのdata内を指定してください。'
}
$rssStop = Join-Path $rssData 'STOP'
$rssStatus = Join-Path $rssData 'status.json'
if ($Action -eq 'Status') {
    if (Test-Path -LiteralPath $rssStatus) { Get-Content -LiteralPath $rssStatus -Encoding UTF8 }
    else { Write-Host 'まだ起動記録がありません。' }
    return
}
if ($Action -eq 'Stop') {
    if (Test-Path -LiteralPath $rssData -PathType Container) {
        [System.IO.File]::WriteAllText($rssStop, 'stop requested')
        Write-Host '停止を要求しました。現在の読取を終え、保存・照合して停止します。完了までお待ちください。'
        Write-Host '開始側の画面で終了表示を確認してください。'
    } else { Write-Host '取得用フォルダーはまだありません。' }
    return
}
if (-not (Test-Path -LiteralPath $rssPython -PathType Leaf)) { throw 'Pythonが見つかりません。' }
Push-Location -LiteralPath $rssProject
try {
    if ($Action -eq 'Check') {
        & $rssPython -m trading_system.rss_continuous --help
        return
    }
    $active = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'trading_system\.rss_continuous'
    })
    if ($active.Count -gt 0) {
        Write-Host 'すでにRSS取得が稼働中です。二重起動しません。' -ForegroundColor Yellow
        return
    }
    function Test-RssLateStart {
        $tokyo = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::Now, 'Tokyo Standard Time')
        # Reserve at least one minute for the three bounded readiness reads.
        return $tokyo.TimeOfDay -ge [TimeSpan]::FromMinutes(15 * 60 + 59)
    }
    function Invoke-RssPostprocessing {
        Write-Host '本日の取得終了時刻です。保存済みデータの取り込み・書き出し・照合を行って終了します。' -ForegroundColor Cyan
        & $rssPython -m trading_system.rss_continuous --workbook $rssWorkbook --code 7203.T --root $rssData --paper --recover-only
        if ($LASTEXITCODE -ne 0) {
            Write-Host '保存・照合が完了していません。エラーとstatus.jsonを確認してください。' -ForegroundColor Red
        } else {
            Write-Host '保存・照合を終えて停止しました。過去の注意履歴は記録に残しています。' -ForegroundColor Green
        }
    }
    if (Test-RssLateStart) { Invoke-RssPostprocessing; return }
    if (-not (Test-Path -LiteralPath $rssWorkbook -PathType Leaf)) { throw 'RSS接続確認.xlsxが見つかりません。' }
    $checkStarted = [datetime]::UtcNow
    $confirmed = & (Join-Path $PSScriptRoot 'confirm_rss_start.ps1') -WorkbookPath $rssWorkbook
    if ($confirmed -ne $true) { Write-Host '開始を取り消しました。'; return }
    if (Test-RssLateStart) { Invoke-RssPostprocessing; return }
    if (Test-Path -LiteralPath $rssStop) {
        if ((Get-Item -LiteralPath $rssStop).LastWriteTimeUtc -ge $checkStarted) {
            Write-Host '確認中に新しい停止要求が届いたため開始しません。' -ForegroundColor Yellow
            return
        }
        Remove-Item -LiteralPath $rssStop
    }
    Write-Host 'RSS常時取得・仮想取引の動作確認（実発注なし）' -ForegroundColor Cyan
    Write-Host 'マーケットスピードIIにログインし、Excelを「接続中・発注不可」にしてください。'
    Write-Host '対象7203.T。約2秒の待機を挟んで読み取ります。実際の間隔には読取時間が加わります。'
    Write-Host '開始前に3件を検査します。日付が前日以前の場合は30秒ごとに再確認し、当日データを待ちます。'
    Write-Host '当日の日付で株価0・時刻の数字が空欄の場合も、注意記録を残して有効な値を待ちます。'
    Write-Host '当日データが3件続けて検査を通ると自動で取得へ進みます。待機中も停止ボタンは有効です。'
    Write-Host 'その他の読取異常や未来の日付では開始を中止します。待機は接続状態や厳密な鮮度の保証ではありません。'
    Write-Host '日本時間16:00に取得を止め、保存・照合して終了します。保存中は画面を閉じずにお待ちください。'
    Write-Host 'PCのスリープ中は処理できません。16時を過ぎて復帰した場合は、取得を再開せず保存して停止します。'
    Write-Host '仮想取引は固定シナリオ1往復だけ。完了後も株価取得を継続します。'
    Write-Host '停止はデスクトップの「RSS常時取得を停止」、またはCtrl+Cを使用してください。'
    Write-Host ('状態・保存先: ' + $rssData)
    & $rssPython -m trading_system.rss_continuous --workbook $rssWorkbook --code 7203.T --root $rssData --paper --cycles $Cycles --wait-for-current-date
    if ($LASTEXITCODE -ne 0) { Write-Host '終了状態を確認してください。二重起動時は既存の取得が優先されます。' -ForegroundColor Yellow }
    else { Write-Host '保存・照合を終えて停止しました。' -ForegroundColor Green }
} finally { Pop-Location }
