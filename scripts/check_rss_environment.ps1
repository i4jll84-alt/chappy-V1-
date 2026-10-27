# Read-only Windows environment observations; does not activate Excel or RSS.
# Usage: powershell.exe -NoProfile -File scripts/check_rss_environment.ps1
# File names: https://marketspeed.jp/ms2_rss/onlinehelp/ohm_001/ohm_001_04.html
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-Observation {
    param([string]$Status, [string]$Reason, [string[]]$ErrorTypes = @())
    return [ordered]@{ status = $Status; reason = $Reason; error_types = @($ErrorTypes) }
}

function Get-LocalFileObservation {
    param([string]$Candidate)
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return New-Observation 'unknown' 'environment_or_registered_path_unavailable'
    }
    # Do not expand arbitrary registry variables, probe UNC paths, mapped drives,
    # or follow reparse points. These could cause remote filesystem access.
    if ($Candidate -notmatch '^[A-Za-z]:\\' -or $Candidate.Contains('%')) {
        return New-Observation 'unknown' 'non_local_or_unexpanded_path_not_probed'
    }
    try {
        $full = [System.IO.Path]::GetFullPath($Candidate)
        $root = [System.IO.Path]::GetPathRoot($full)
        $drive = New-Object System.IO.DriveInfo($root)
        if ($drive.DriveType -ne [System.IO.DriveType]::Fixed) {
            return New-Observation 'unknown' 'non_fixed_drive_not_probed'
        }
        $current = $root
        $parts = $full.Substring($root.Length).Split('\', [System.StringSplitOptions]::RemoveEmptyEntries)
        foreach ($part in $parts) {
            $current = [System.IO.Path]::Combine($current, $part)
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                return New-Observation 'unknown' 'reparse_point_not_followed'
            }
        }
        if ($item -is [System.IO.FileInfo]) {
            return New-Observation 'found' 'local_file_observed'
        }
        return New-Observation 'not_found' 'expected_file_is_not_a_file'
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return New-Observation 'not_found' 'local_path_not_found'
    }
    catch [System.IO.FileNotFoundException] {
        return New-Observation 'not_found' 'local_path_not_found'
    }
    catch [System.IO.DirectoryNotFoundException] {
        return New-Observation 'not_found' 'local_path_not_found'
    }
    catch {
        return New-Observation 'unknown' 'filesystem_probe_failed' @($_.Exception.GetType().Name)
    }
}

function Get-RegistryObservation {
    param([string]$Hive, [string]$View, [ValidateSet('excel', 'click_to_run')][string]$Kind)
    $base = $null
    $key = $null
    $result = [ordered]@{ hive = $Hive; view = $View }
    try {
        $hiveValue = if ($Hive -eq 'HKCU') {
            [Microsoft.Win32.RegistryHive]::CurrentUser
        } else {
            [Microsoft.Win32.RegistryHive]::LocalMachine
        }
        $viewValue = [Microsoft.Win32.RegistryView]::$View
        # Failure to open a hive is UNKNOWN, never evidence of absence.
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hiveValue, $viewValue)
        $subkey = if ($Kind -eq 'excel') {
            'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\excel.exe'
        } else {
            'SOFTWARE\Microsoft\Office\ClickToRun\Configuration'
        }
        $key = $base.OpenSubKey($subkey, $false)
        if ($null -eq $key) {
            $result.observation = New-Observation 'not_found' 'registry_subkey_not_found'
        } elseif ($Kind -eq 'excel') {
            $result.registration_status = 'found'
            $registered = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($registered -is [string] -and -not [string]::IsNullOrWhiteSpace($registered)) {
                $result.observation = Get-LocalFileObservation $registered.Trim().Trim('"')
            } else {
                $result.observation = New-Observation 'unknown' 'registered_executable_value_unavailable'
            }
        } else {
            $platform = $key.GetValue('Platform', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $result.observation = New-Observation 'found' 'click_to_run_configuration_observed'
            $result.architecture = if ($platform -in @('x86', 'x64', 'arm64')) { $platform } else { 'unknown' }
        }
    }
    catch {
        $result.observation = New-Observation 'unknown' 'registry_probe_failed' @($_.Exception.GetType().Name)
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
        if ($null -ne $base) { $base.Dispose() }
    }
    return $result
}

function Get-RelativeLocalFileObservation {
    param([string]$Root, [string]$Relative)
    try {
        if ([string]::IsNullOrWhiteSpace($Root)) {
            return New-Observation 'unknown' 'environment_or_registered_path_unavailable'
        }
        return Get-LocalFileObservation ([System.IO.Path]::Combine($Root, $Relative))
    }
    catch {
        return New-Observation 'unknown' 'filesystem_probe_failed' @($_.Exception.GetType().Name)
    }
}

$excelRegistrations = @()
$officeArchitectures = @()
foreach ($hive in @('HKCU', 'HKLM')) {
    foreach ($view in @('Registry32', 'Registry64')) {
        $excelRegistrations += Get-RegistryObservation $hive $view 'excel'
        $officeArchitectures += Get-RegistryObservation $hive $view 'click_to_run'
    }
}

$conventionalExcel = @()
$programRoots = @(
    @{ label = 'ProgramFiles'; value = $env:ProgramFiles },
    @{ label = 'ProgramFiles(x86)'; value = ${env:ProgramFiles(x86)} }
)
foreach ($programRoot in $programRoots) {
    foreach ($relative in @('Microsoft Office\root\Office16\EXCEL.EXE', 'Microsoft Office\Office16\EXCEL.EXE')) {
        $conventionalExcel += [ordered]@{
            location = '%' + $programRoot.label + '%\' + $relative
            observation = Get-RelativeLocalFileObservation $programRoot.value $relative
        }
    }
}

$rssFiles = @()
foreach ($name in @('MarketSpeed2_RSS_32bit.xll', 'MarketSpeed2_RSS_64bit.xll', 'MarketSpeed2_RSS_VBA.xlam')) {
    $rssFiles += [ordered]@{
        file_name = $name
        location = '%LOCALAPPDATA%\MarketSpeed2\Bin\rss'
        observation = Get-RelativeLocalFileObservation $env:LOCALAPPDATA ('MarketSpeed2\Bin\rss\' + $name)
    }
}

[ordered]@{
    schema_version = 1
    mode = 'read_only_environment_diagnostic'
    real_execution_enabled = $false
    installation_observation_only = $true
    connection_proven = $false
    excel_app_paths = @($excelRegistrations)
    office_click_to_run = @($officeArchitectures)
    conventional_excel_files = @($conventionalExcel)
    rss_files = @($rssFiles)
    unverified = @('excel_runtime_bitness', 'addin_registration', 'login', 'rss_connection', 'data_freshness', 'order_capability')
    scope = 'Registry and fixed local file metadata only; absence is limited to these locations. Uses invoking process environment roots, which can differ in a sandbox.'
} | ConvertTo-Json -Depth 8
