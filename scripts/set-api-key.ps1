# Masked local prompt. Never put the key in a command, chat, transcript, or source file.
$ErrorActionPreference = 'Stop'
$secureKey = Read-Host 'J-Quants API key (input hidden)' -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    [Environment]::SetEnvironmentVariable('JQUANTS_API_KEY', $plainKey, 'User')
    Write-Host 'JQUANTS_API_KEY saved. The value has not been displayed.'
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    $plainKey = $null
    $secureKey.Dispose()
}
