$ErrorActionPreference = 'Stop'
$serviceDir = $PSScriptRoot
$baseUrl = 'http://127.0.0.1:8765'
try {
    $listeners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        $state = Invoke-RestMethod "$baseUrl/api/status" -TimeoutSec 5
        if ($null -eq $state.running -or $null -eq $state.pending) {
            throw 'Cannot verify service state. No process was stopped.'
        }
        if ($state.running -ne 0 -or $state.pending -ne 0) {
            throw 'Tasks are running or queued. Wait until idle, then retry.'
        }
        $processIds = @($listeners.OwningProcess | Sort-Object -Unique)
        if ($processIds.Count -ne 1) { throw 'Unexpected listeners. Restart cancelled.' }
        $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($processIds[0])"
        if ($serviceProcess.Name -notmatch '^python(w)?\.exe$' -or $serviceProcess.CommandLine -notmatch '(?:\s|["''])app\.py(?:["'']|\s|$)') {
            throw 'Port owner is not the expected Python app.py service. Restart cancelled.'
        }
        $pythonPath = $serviceProcess.ExecutablePath
    } else {
        $pythonPath = (& py -3.12 -c 'import sys; print(sys.executable)' | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 was not found.' }
    }
    if (-not $pythonPath -or -not (Test-Path -LiteralPath $pythonPath)) { throw 'Python executable was not found.' }
    if (-not (Test-Path -LiteralPath (Join-Path $serviceDir 'app.py'))) { throw 'app.py was not found.' }
    $logDir = Join-Path $serviceDir '..\_local\logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outLog = Join-Path $logDir "restart-$stamp.out.log"
    $errLog = Join-Path $logDir "restart-$stamp.err.log"
    if ($listeners.Count -gt 0) {
        $freshState = Invoke-RestMethod "$baseUrl/api/status" -TimeoutSec 5
        if ($freshState.running -ne 0 -or $freshState.pending -ne 0) { throw 'Service became busy. Restart cancelled.' }
        $freshListener = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
        if (@($freshListener | Where-Object OwningProcess -ne $serviceProcess.ProcessId).Count -gt 0) { throw 'Port owner changed. Restart cancelled.' }
        Write-Host 'Stopping idle knowledge service...'
        Stop-Process -Id $serviceProcess.ProcessId -ErrorAction Stop
        Wait-Process -Id $serviceProcess.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    Write-Host 'Starting knowledge service in background...'
    $started = Start-Process -FilePath $pythonPath -ArgumentList '-X','utf8','app.py' -WorkingDirectory $serviceDir -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        $started.Refresh()
        if ($started.HasExited) { throw "Service exited. Check: $errLog" }
        try {
            $health = Invoke-RestMethod "$baseUrl/api/status" -TimeoutSec 2
            $schema = Invoke-RestMethod "$baseUrl/openapi.json" -TimeoutSec 2
            $capabilities = Invoke-RestMethod "$baseUrl/api/capabilities" -TimeoutSec 2
            $live = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
            if ($health.worker -eq 'running' -and $capabilities.taxonomy -eq 'scopes-tags-v1' -and $schema.paths.PSObject.Properties.Name -contains '/api/reviews' -and $live.OwningProcess -contains $started.Id) {
                $ready = $true
                break
            }
        } catch { }
    }
    if (-not $ready) { throw "Health or confirmation API check failed. Check: $errLog" }
    Write-Host "SUCCESS: new service is running. PID=$($started.Id)" -ForegroundColor Green
    Write-Host 'Confirmation API: available. You can close this window.'
    Write-Host "Logs: $logDir"
    exit 0
} catch {
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'No automatic retry. Keep this message for troubleshooting.'
    exit 1
}
