param([ValidateSet('setup','check','run','inspect','test','benchmark')][string]$Action = 'check')
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$env:PYTHONUTF8 = '1'
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:LOCAL_MODEL = 'qwen3:1.7b'
$env:AI_MODE = 'ollama'
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$TokenPath = Join-Path $ProjectRoot 'private\bot-token.dpapi'

function Read-BotToken {
    if (!(Test-Path -LiteralPath $TokenPath)) { throw 'Run setup first.' }
    $SecureToken = Get-Content -Raw -LiteralPath $TokenPath | ConvertTo-SecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try { $env:BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
}

if ($Action -eq 'setup') {
    & py -3 -c 'import sys; assert sys.version_info >= (3,12)'
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12+ from python.org.' }
    if (!(Test-Path -LiteralPath $PythonExe)) {
        & py -3 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment.' }
    }
    if (!(Test-Path -LiteralPath 'config.json')) {
        Copy-Item -LiteralPath 'examples\config.example.json' -Destination 'config.json'
    }
    New-Item -ItemType Directory -Force -Path 'private' | Out-Null
    if (Test-Path -LiteralPath $TokenPath) {
        Write-Host 'Existing protected token preserved. Rotate explicitly if needed.'
    } else {
        $SecureToken = Read-Host 'Paste BotFather token (hidden)' -AsSecureString
        $SecureToken | ConvertFrom-SecureString | Set-Content -LiteralPath $TokenPath -Encoding ASCII
    }
    Write-Host 'Setup created files only. Configure IDs/FAQ, install local Ollama and follow README.'
    exit 0
}
if (!(Test-Path -LiteralPath $PythonExe)) { throw 'Run setup first.' }
try {
    switch ($Action) {
        'check' { & $PythonExe -m assistant_core.main --config config.json --check }
        'test' { & $PythonExe -m unittest discover -s tests -v }
        'benchmark' { & $PythonExe tools\check_model.py }
        'inspect' { Read-BotToken; & $PythonExe tools\inspect_telegram.py }
        'run' { Read-BotToken; & $PythonExe -m assistant_core.main --config config.json }
    }
    $ResultCode = $LASTEXITCODE
} finally { Remove-Item Env:BOT_TOKEN -ErrorAction SilentlyContinue }
exit $ResultCode
