# Windows: finds Python 3.9+, offers to install it (with the user's consent) if it is missing, then starts xui-fleet.
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Test-Python($exe, $pre) {
    try {
        $out = & $exe @pre -c "import sys, venv, ensurepip; print(sys.version_info >= (3, 9))" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq 'True')
    } catch { return $false }
}

function Find-Python {
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $cands += @{ exe = 'py'; pre = @('-3') } }
    foreach ($n in 'python', 'python3') {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        # The WindowsApps "python" is only a Store shortcut, not Python.
        if ($c -and $c.Source -notlike '*WindowsApps*') { $cands += @{ exe = $c.Source; pre = @() } }
    }
    $dirs = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")
    foreach ($d in $dirs) {
        if ($d -and (Test-Path $d)) {
            Get-ChildItem -Path $d -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending |
                ForEach-Object { $p = Join-Path $_.FullName 'python.exe'; if (Test-Path $p) { $cands += @{ exe = $p; pre = @() } } }
        }
    }
    foreach ($c in $cands) { if (Test-Python $c.exe $c.pre) { return $c } }
    return $null
}

function Install-Python {
    Write-Host ""
    Write-Host "Для работы xui-fleet нужен Python (бесплатный, с официального сайта python.org)."
    $answer = Read-Host "Установить его сейчас? Это займёт 1-3 минуты. [Д/н]"
    if ($answer -match '^\s*(н|n|no|нет)') { throw "Установка отменена. Без Python приложение не запустится." }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Устанавливаю Python через winget (только для этого пользователя)..."
        winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        if (Find-Python) { return }
    }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $url = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-$arch.exe"
    $tmp = Join-Path $env:TEMP "python-3.12.8-$arch.exe"
    Write-Host "Скачиваю установщик Python с python.org..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    $sig = Get-AuthenticodeSignature $tmp
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        throw "Подпись установщика не подтверждена — установка остановлена."
    }
    Write-Host "Устанавливаю Python (только для этого пользователя, без прав администратора)..."
    $p = Start-Process -FilePath $tmp -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'InstallLauncherAllUsers=0', 'Include_test=0' -Wait -PassThru
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "Установщик Python завершился с кодом $($p.ExitCode)." }
}

try {
    $py = Find-Python
    if (-not $py) {
        Install-Python
        $py = Find-Python
        if (-not $py) { throw "Python установлен, но не найден. Закройте это окно и запустите файл ещё раз." }
    }
    & $py.exe @($py.pre) (Join-Path $Root 'launcher.py')
    if ($LASTEXITCODE -ne 0) { throw "Запуск завершился с ошибкой (код $LASTEXITCODE)." }
} catch {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host "Нажмите Enter, чтобы закрыть"
    exit 1
}
