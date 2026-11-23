param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$BindAddress = "0.0.0.0"
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)
if (-not $env:VTOH_ASR_KEY) {
    $env:VTOH_ASR_KEY = if ($env:DASHSCOPE_API_KEY) { $env:DASHSCOPE_API_KEY } else { $env:AI_BAILIAN_API_KEY }
}
if (-not $env:VTOH_CHAT_KEY) {
    $env:VTOH_CHAT_KEY = if ($env:DASHSCOPE_API_KEY) { $env:DASHSCOPE_API_KEY } else { $env:AI_BAILIAN_API_KEY }
}
if (-not $env:VTOH_ASR_KEY -or -not $env:VTOH_CHAT_KEY) {
    throw 'Set VTOH_ASR_KEY and VTOH_CHAT_KEY before starting.'
}
if (-not $env:VTOH_ACCESS_KEY) {
    $env:VTOH_ACCESS_KEY = [guid]::NewGuid().ToString('N')
    Write-Host "Client access key: $env:VTOH_ACCESS_KEY"
}
& python -m uvicorn server.app:app --host $BindAddress --port $Port --ws-max-size 32768 --no-access-log --log-level warning
exit $LASTEXITCODE
