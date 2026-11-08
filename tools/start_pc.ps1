# Windows PowerShell 5.1은 한글을 읽도록 UTF-8 BOM을 유지한다.
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Url = "http://127.0.0.1:$Port"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$Mutex = New-Object System.Threading.Mutex($false, "Local\DongguPC-127.0.0.1-$Port")
$Locked = $false
$Server = $null
$Ready = $false

function Test-PortOpen {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($pending)
        return $true
    } catch { return $false } finally { $client.Dispose() }
}

function Get-ExpectedVersion {
    $source = Get-Content -LiteralPath (Join-Path $Repo 'pc\dgpc\__init__.py') -Raw -Encoding UTF8
    $match = [regex]::Match($source, '(?m)^__version__\s*=\s*["''](?<version>[^"''\r\n]+)["'']\s*$')
    if (-not $match.Success) { throw '실행기의 버전 정보를 확인하지 못했습니다. 프로그램 파일을 확인하세요.' }
    return $match.Groups['version'].Value
}

function Test-AppReady {
    $script:AppHealth = 'unrecognized'
    try {
        $api = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/state" `
            -Headers @{'X-DG-CSRF' = '1'} -TimeoutSec 1 -MaximumRedirection 0
        if ($api.StatusCode -ne 200 -or $api.Headers['Server'].Trim() -ne 'dgpc') { return $false }
        $state = $api.Content | ConvertFrom-Json
        if ($state.setup_needed -isnot [bool] -or $state.logged_in -isnot [bool]) { return $false }
        if ([string]$api.Headers['X-DG-Version'] -cne $ExpectedVersion) {
            $script:AppHealth = 'version_mismatch'
            return $false
        }
        $script:AppHealth = 'assets_unavailable'
        $page = Invoke-WebRequest -UseBasicParsing -Uri "$Url/" -TimeoutSec 1 -MaximumRedirection 0
        if ($page.StatusCode -ne 200 -or -not $page.Content.Contains('<title>동구 대형폐기물 PC 업무</title>')) { return $false }
        $scriptPattern = @'
(?is)<!--.*?(?:-->|\z)|<script\b(?<attrs>(?:[^>'"]|"[^"]*"|'[^']*')*)>.*?(?:</script\s*>|\z)|<(?:[^>'"]|"[^"]*"|'[^']*')*>
'@
        $attributePattern = @'
(?is)(?<name>[^\s=/"'<>]+)\s*=\s*(?:"(?<double>[^"]*)"|'(?<single>[^']*)'|(?<bare>[^\s"'=<>]+))
'@
        $sources = New-Object 'System.Collections.Generic.List[string]'
        foreach ($tag in [regex]::Matches($page.Content, $scriptPattern)) {
            if (-not $tag.Groups['attrs'].Success) { continue }
            foreach ($attribute in [regex]::Matches($tag.Groups['attrs'].Value, $attributePattern)) {
                if ($attribute.Groups['name'].Value -ieq 'src') {
                    $source = if ($attribute.Groups['double'].Success) { $attribute.Groups['double'].Value } elseif ($attribute.Groups['single'].Success) { $attribute.Groups['single'].Value } else { $attribute.Groups['bare'].Value }
                    $source = [System.Net.WebUtility]::HtmlDecode($source).Trim()
                    if (-not $source) { return $false }
                    $sources.Add($source)
                    break
                }
            }
        }
        if ($sources.Count -eq 0) { return $false }
        $sources.Add('pc.js')
        $sources.Add('core.js')
        $baseUri = [Uri]$Url
        $resources = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($source in $sources) {
            $assetUri = [Uri]::new($baseUri, $source)
            if ($assetUri.Scheme -ne $baseUri.Scheme -or $assetUri.Host -ne $baseUri.Host -or $assetUri.Port -ne $baseUri.Port -or $assetUri.UserInfo) { return $false }
            [void]$resources.Add($assetUri.AbsoluteUri)
        }
        foreach ($resource in $resources) {
            $asset = Invoke-WebRequest -UseBasicParsing -Uri $resource -TimeoutSec 1 -MaximumRedirection 0
            $mime = ($asset.Headers['Content-Type'] -split ';', 2)[0].Trim().ToLowerInvariant()
            $body = if ($asset.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($asset.Content) } else { [string]$asset.Content }
            if ($asset.StatusCode -ne 200 -or $mime -notin @('text/javascript', 'application/javascript', 'text/ecmascript', 'application/ecmascript') -or [string]::IsNullOrWhiteSpace($body)) { return $false }
        }
        $script:AppHealth = 'ready'
        return $true
    } catch { return $false }
}

function Show-Ready([string]$Status) {
    $listener = Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort $Port |
        Select-Object -First 1
    Write-Host "동구 PC 업무 화면이 준비되었습니다: $Url"
    if ($Status -eq 'existing') { Write-Host '실행 중인 동구 PC 서버를 그대로 사용합니다.' }
    if ($NoBrowser) {
        $result = @{status = $Status; url = $Url; process_id = [int]$listener.OwningProcess}
        if ($Status -eq 'started') { $result.started_process_id = $Server.Id }
        Write-Output ('DGPC_READY ' + ($result | ConvertTo-Json -Compress))
    } else {
        Start-Process -FilePath $Url
    }
}

function Find-Python {
    $venv = Join-Path $Repo '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = & $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate) { return $candidate.Trim() }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notlike '*\WindowsApps\*') { return $command.Source }
    }
    throw 'Python 3.12 이상을 찾지 못했습니다. Python 설치 후 Donggu-PC.cmd -Install을 실행하세요.'
}

Push-Location -LiteralPath $Repo
try {
    try { $Locked = $Mutex.WaitOne(60000) } catch [System.Threading.AbandonedMutexException] { $Locked = $true }
    if (-not $Locked) { throw '다른 실행기가 PC를 준비 중입니다. 잠시 후 다시 실행하세요.' }
    $ExpectedVersion = Get-ExpectedVersion

    # 포트 충돌은 자료 초기화·의존성 설치보다 먼저 확인한다.
    if (Test-PortOpen) {
        if (-not (Test-AppReady)) {
            if ($script:AppHealth -eq 'version_mismatch') {
                throw "실행 중인 동구 PC 서버의 버전이 이 실행기($ExpectedVersion)와 다르거나 확인되지 않습니다. 기존 서버를 해당 실행 앱에서 종료한 뒤 다시 실행하세요. 기존 서버는 종료하지 않았고 새 서버도 시작하지 않았습니다."
            }
            throw "포트 $Port 를 다른 프로그램이 사용 중이거나 PC 서버가 아직 준비되지 않았습니다. 해당 프로그램을 종료하지 않았습니다. 잠시 후 재실행하거나 Donggu-PC.cmd -Port 8766을 사용하세요."
        }
        Show-Ready 'existing'
        exit 0
    }

    $Python = Find-Python
    & $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 이상이 필요합니다. .venv의 Python 버전을 확인하세요.' }
    if ($Install) {
        $venv = Join-Path $Repo '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venv)) {
            Write-Host '이 저장소의 .venv를 준비합니다.'
            & $Python -m venv (Join-Path $Repo '.venv')
            if ($LASTEXITCODE -ne 0) { throw '.venv 생성 실패. Python 설치 상태와 폴더 쓰기 권한을 확인하세요.' }
        }
        $Python = $venv
        Write-Host '고정된 PC 의존성을 설치합니다. 최초 준비에는 인터넷이 필요합니다.'
        & $Python -m pip install -r 'pc/requirements.txt'
        if ($LASTEXITCODE -ne 0) { throw 'PC 의존성 설치 실패. 위 설치 오류를 확인한 뒤 Donggu-PC.cmd -Install을 다시 실행하세요.' }
    }
    $dependencyCheck = @'
import sys
try:
    import cryptography, qrcode, PIL, keyring, ortools
    from ortools.constraint_solver import pywrapcp
    import sqlcipher3
    connection = sqlcipher3.connect(':memory:')
    assert connection.execute('PRAGMA cipher_version').fetchone()
    connection.close()
except Exception as error:
    print(type(error).__name__)
    sys.exit(1)
'@
    $dependencyResult = & $Python -c $dependencyCheck
    if ($LASTEXITCODE -ne 0) {
        throw 'SQLCipher 등 PC 필수 모듈이 준비되지 않았습니다. 최초 한 번 터미널에서 .\Donggu-PC.cmd -Install을 실행하세요. 고정 설치 목록: pc/requirements.txt (Node.js는 필요하지 않습니다).'
    }

    $State = if ($env:DG_STATE) { $env:DG_STATE } else { Join-Path $Repo 'pc\state' }
    if (-not [System.IO.Path]::IsPathRooted($State)) { $State = Join-Path $Repo $State }
    $State = [System.IO.Path]::GetFullPath($State)
    $env:DG_STATE = $State
    if (-not (Test-Path -LiteralPath (Join-Path $State 'pc.db'))) {
        Write-Host '최초 실행: 암호화 DB와 PC 키를 준비합니다. 비밀번호 안내가 나오면 이 창에서 입력하세요.'
        & $Python 'pc/pc_tool.py' init
        if ($LASTEXITCODE -ne 0) { throw '초기화하지 못했습니다. 위 안내를 확인하세요. 기존 자료를 삭제하지 마세요.' }
    }

    # 키 값은 출력·파일 저장하지 않는다. 콘솔 입력이 필요한 실행은 창을 유지한다.
    $keyCheck = @'
import os, sys
sys.path.insert(0, 'pc')
from dgpc import store
print(1 if os.environ.get('DG_DB_KEY') or store._keyring_get() else 0)
'@
    $keyAvailable = & $Python -c $keyCheck
    if ($LASTEXITCODE -ne 0) { throw 'DB 키 저장소 확인 실패. Python 환경을 확인하세요.' }
    $Interactive = $keyAvailable.Trim() -ne '1'
    $Logs = Join-Path $State 'logs'
    New-Item -ItemType Directory -Path $Logs -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outLog = Join-Path $Logs "launcher-$stamp.out.log"
    $errLog = Join-Path $Logs "launcher-$stamp.err.log"
    $arguments = @('pc/pc_tool.py', 'serve', '--port', "$Port")
    if ($Interactive) {
        Write-Host 'OS 키 저장소에 DB 키가 없어 비밀번호를 입력해야 합니다. 업무를 마칠 때까지 이 창을 열어 두세요.'
        $Server = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Repo -NoNewWindow -PassThru
    } else {
        Write-Host '동구 PC 서버를 시작합니다. 화면이 준비될 때까지 기다려 주세요.'
        $Server = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Repo `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    }
    $deadline = (Get-Date).AddSeconds($(if ($Interactive) { 180 } else { 45 }))
    while ((Get-Date) -lt $deadline) {
        $Server.Refresh()
        if ($Server.HasExited) { throw "PC 서버가 시작되지 않았습니다. DB 비밀번호·실행 오류를 확인하세요. 로그 폴더: $Logs" }
        if (Test-AppReady) { $Ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $Ready) { throw "PC 화면 준비 시간이 초과되었습니다. 로그 폴더: $Logs" }
    Show-Ready 'started'
    $Mutex.ReleaseMutex()
    $Locked = $false
    if ($Interactive) { $Server.WaitForExit() }
    exit 0
} catch {
    if ($Server -and -not $Ready -and -not $Server.HasExited) {
        # 이 실행기가 만든 프로세스 트리만 정리한다. 포트의 다른 프로세스는 종료하지 않는다.
        & taskkill.exe /PID $Server.Id /T /F 2>$null | Out-Null
    }
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($Locked) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
    Pop-Location
}
