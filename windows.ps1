param([ValidateSet('setup','check','run','inspect','test','benchmark','autostart-install','autostart-remove','harden','power','backup','health','status','coverage')][string]$Action = 'check')
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$env:PYTHONUTF8 = '1'
# ТЗ 5.0: локальная LLM не нужна -> режим фиксированных ответов, Ollama не используется.
$env:AI_MODE = 'faq'
$env:OLLAMA_NO_CLOUD = '1'
$TaskName = 'TelegramAssistant'
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$TokenPath = Join-Path $ProjectRoot 'private\bot-token.dpapi'


function Find-Python {
    # Подходит ЛЮБАЯ установленная версия >= 3.12 (3.12, 3.13, 3.14 ...).
    # Порядок: 3.12 (на ней прогнаны тесты), 3.13, 3.14, затем python из PATH.
    # Можно указать явно: $env:PYTHON_VERSION = '3.13'
    $versions = if ($env:PYTHON_VERSION) { @($env:PYTHON_VERSION) } else { @('3.12','3.13','3.14') }
    $check = 'import sys; assert sys.version_info >= (3,12)'
    foreach ($v in $versions) {
        try {
            & py "-$v" -c $check 2>$null
            if ($LASTEXITCODE -eq 0) { return @{ Exe = 'py'; Args = @("-$v") } }
        } catch {}
    }
    try {
        & python -c $check 2>$null
        if ($LASTEXITCODE -eq 0) { return @{ Exe = 'python'; Args = @() } }
    } catch {}
    return $null
}

function Read-BotToken {
    if (!(Test-Path -LiteralPath $TokenPath)) { throw 'Run setup first.' }
    $SecureToken = Get-Content -Raw -LiteralPath $TokenPath | ConvertTo-SecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try { $env:BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
}

if ($Action -eq 'setup') {
    $Py = Find-Python
    if (!$Py) { throw 'Python 3.12 or newer (64-bit) not found. Install from python.org (with the py launcher).' }
    if (!(Test-Path -LiteralPath $PythonExe)) {
        & $Py.Exe @($Py.Args) -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment.' }
    }
    & $PythonExe -c 'import sys; assert sys.version_info >= (3,12)'
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv uses Python older than 3.12. Delete .venv and run setup again.' }
    & $PythonExe --version
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
    Write-Host 'Setup created files only. Next: edit config.json, then: harden, power, check, test, inspect, run, autostart-install.'
    exit 0
}
if (!(Test-Path -LiteralPath $PythonExe)) { throw 'Run setup first.' }

function Assert-RealConfig {
    # Пример из examples\config.example.json содержит выдуманные ID. Реальные ID берутся при установке (inspect).
    if (!(Test-Path -LiteralPath 'config.json')) { throw 'config.json not found. Run setup first.' }
    $c = Get-Content -Raw -LiteralPath 'config.json' -Encoding UTF8 | ConvertFrom-Json
    if ($c.owner_id -eq 100000001 -or $c.business_owner_id -eq 100000001 -or ($c.contacts -contains 100000002)) {
        throw 'config.json still has placeholder IDs (100000001/100000002). Fill real IDs from inspect (TZ: do not invent IDs).'
    }
}

function Assert-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (!$p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run PowerShell as Administrator for this action.' }
}

switch ($Action) {
'autostart-install' {
    $Args1 = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $ProjectRoot 'windows.ps1') + '" run'
    $act  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Args1 -WorkingDirectory $ProjectRoot
    $trg  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $set  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    $prin = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set -Principal $prin -Force | Out-Null
    Write-Host "Task '$TaskName' registered (at logon of $env:USERNAME). After reboot the bot starts in STOP: owner sends /resume."
    exit 0
}
'autostart-remove' {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed."; exit 0
}
'harden' {
    # NTFS: only current user, SYSTEM, Administrators. Run once after setup.
    foreach ($d in 'private','data','examples\materials') {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        # SID вместо имён: работает на любой локализации Windows (S-1-5-18 = SYSTEM, S-1-5-32-544 = Administrators).
        & icacls $d /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls failed for $d" }
        Write-Host "ACL restricted: $d"
    }
    if (Test-Path config.json) { & icacls config.json /inheritance:r /grant:r "${env:USERNAME}:F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null }
    exit 0
}
'power' {
    Assert-Admin
    # Не уходить в сон от сети; экран можно гасить; закрытие крышки при питании от сети - ничего не делать.
    & powercfg /change standby-timeout-ac 0
    & powercfg /change hibernate-timeout-ac 0
    & powercfg /change monitor-timeout-ac 10
    & powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    & powercfg /setactive SCHEME_CURRENT
    Write-Host 'Power plan updated (AC: no sleep, no hibernate, lid close = do nothing).'
    exit 0
}
'backup' {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $dest = Join-Path (Split-Path $ProjectRoot -Parent) "TelegramAssistant-backups\backup-$stamp"
    New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
    & $PythonExe -m assistant_core.ops backup .\data $dest
    if ($LASTEXITCODE -ne 0) { throw 'Backup failed. Did you /stop and close the bot?' }
    # config.json (без секретов) и материалы - рядом с копией базы; токен DPAPI не копируется (на новом ПК вводится заново).
    $extra = "$dest-config"
    New-Item -ItemType Directory -Force -Path $extra | Out-Null
    if (Test-Path config.json) { Copy-Item -LiteralPath config.json -Destination $extra }
    Copy-Item -LiteralPath 'examples\materials' -Destination $extra -Recurse
    Write-Host "Backup created: $dest (+ config and materials: $extra)"; exit 0
}
'coverage' {
    & $PythonExe -c 'import coverage' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'coverage not installed: .\.venv\Scripts\python.exe -m pip install coverage' }
    & $PythonExe -m coverage run --source=src\assistant_core -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    & $PythonExe -m coverage report -m --fail-under=100
    exit $LASTEXITCODE
}
'health' { & $PythonExe -m assistant_core.ops health .\data; if ($LASTEXITCODE -eq 0) { Write-Host 'OK: heartbeat is fresh' } else { Write-Host 'FAIL: bot not running or stuck' }; exit $LASTEXITCODE }
'status' {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host ('Autostart task : ' + $(if ($t) { $t.State } else { 'NOT INSTALLED' }))
    Write-Host ('Token file     : ' + $(if (Test-Path $TokenPath) { 'present' } else { 'MISSING' }))
    Write-Host ('config.json    : ' + $(if (Test-Path config.json) { 'present' } else { 'MISSING' }))
    & $PythonExe --version
    & $PythonExe -m assistant_core.ops health .\data 2>$null
    Write-Host ('Heartbeat      : ' + $(if ($LASTEXITCODE -eq 0) { 'fresh (bot running)' } else { 'stale/none' }))
    exit 0
}
}
try {
    switch ($Action) {
        'check' { try { Assert-RealConfig } catch { Write-Warning $_.Exception.Message }; & $PythonExe -m assistant_core.main --config config.json --check }
        'test' { & $PythonExe -m unittest discover -s tests -v }
        'benchmark' { & $PythonExe tools\check_model.py }
        'inspect' { Read-BotToken; & $PythonExe tools\inspect_telegram.py }
        'run' { Assert-RealConfig; Read-BotToken; & $PythonExe -m assistant_core.main --config config.json }
    }
    $ResultCode = $LASTEXITCODE
} finally { Remove-Item Env:BOT_TOKEN -ErrorAction SilentlyContinue }
exit $ResultCode
