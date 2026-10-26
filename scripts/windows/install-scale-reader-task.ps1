#Requires -RunAsAdministrator
# Registers the Task Scheduler task that runs the health-sync Bluetooth scale reader on devicex2.
#
# The reader listens for the Mi Body Composition Scale 2 and posts to the health-sync ingest endpoint.
# It used to run as a visible cmd window in the owner's logon session (task \health-sync-scale, LogonType
# Interactive, AtLogOn, action run.cmd). This script replaces that with an S4U task: it runs in session 0
# (no console window, nothing to close by accident) and starts at boot without anyone logging on.
# A BLE scan from session 0 was verified on devicex2 on 2026-10-26 (25 devices seen vs 27 interactive).
#
#   install-scale-reader-task.ps1                    # register only (backs up the existing task first)
#   install-scale-reader-task.ps1 -StartNow          # register, stop the old instance, start the task now
#   install-scale-reader-task.ps1 -Deploy -StartNow  # also copy reader.py/miscale.py/run-scale-reader.cmd
#
# Deploy copies only those three files into the install dir. It never touches token.txt, pending.jsonl
# or venv.
#
# Rollback: before registering, an existing task is exported to <install dir>\health-sync-scale.bak-<ts>.xml.
# If the -StartNow check fails (no python reader.py in session 0 within 60 s), the backup XML is
# re-registered and started. Without a backup the old interactive run.cmd is started instead.
param(
    [string]$InstallDir = 'C:\Tools\health-sync-scale',
    [string]$User = 'DEVICEX2\igorp',
    [string]$SourceDir = '',
    [string]$FallbackCmd = 'C:\Tools\health-sync-scale\run.cmd',
    [switch]$Deploy,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$taskName = 'health-sync-scale'
$backup = $null
$script:rc = 0

function Get-ReaderProcesses {
    # Any process whose command line mentions the install dir: run-scale-reader.cmd, cmd.exe /c ..., the python reader.
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine -match 'health-sync-scale'
    }
}

function Show-ReaderState {
    $procs = @(Get-ReaderProcesses)
    if ($procs.Count -eq 0) { 'reader processes: none'; return }
    foreach ($p in $procs) {
        'pid {0}, session {1}, {2}' -f $p.ProcessId, $p.SessionId, $p.Name
    }
}

function Invoke-Rollback([string]$reason) {
    'FAILED: ' + $reason
    $script:rc = 1
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    foreach ($p in @(Get-ReaderProcesses)) {
        'killing pid {0}' -f $p.ProcessId
        & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null
    }
    if ($backup) {
        try {
            Register-ScheduledTask -Xml (Get-Content $backup -Raw) -TaskName $taskName -Force | Out-Null
            're-registered {0} from {1}' -f $taskName, $backup
            Start-ScheduledTask -TaskName $taskName
            'started the restored task'
        } catch {
            'rollback: re-register from {0} failed: {1}' -f $backup, $_.Exception.Message
        }
    } else {
        'no backup of the previous task exists - manual recovery (elevated PowerShell):'
        '  Unregister-ScheduledTask -TaskName {0} -Confirm:$false; & {1}' -f $taskName, $FallbackCmd
        if (Test-Path $FallbackCmd) {
            'starting the old interactive wrapper: ' + $FallbackCmd
            Start-Process -FilePath $FallbackCmd
        }
    }
    'ROLLED BACK'
    Show-ReaderState
}

# 0. Optionally deploy the reader files from the repo checkout (never touches token.txt/pending.jsonl/venv).
if ($Deploy) {
    if (-not $SourceDir) {
        throw '-Deploy needs -SourceDir (the health-sync repo root, e.g. C:\work\health-sync)'
    }
    $copies = @(
        @{ From = Join-Path $SourceDir 'health_sync\scale\reader.py';         To = Join-Path $InstallDir 'reader.py' },
        @{ From = Join-Path $SourceDir 'health_sync\scale\miscale.py';        To = Join-Path $InstallDir 'miscale.py' },
        @{ From = Join-Path $SourceDir 'scripts\windows\run-scale-reader.cmd'; To = Join-Path $InstallDir 'run-scale-reader.cmd' }
    )
    if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir | Out-Null }
    foreach ($c in $copies) {
        if (-not (Test-Path $c.From)) { throw ('source file missing: ' + $c.From) }
        Copy-Item -Path $c.From -Destination $c.To -Force
        'deployed {0} -> {1}' -f $c.From, $c.To
    }
}

$cmdPath = Join-Path $InstallDir 'run-scale-reader.cmd'
if (-not (Test-Path $cmdPath)) {
    throw ('wrapper not found: {0} (pass -Deploy -SourceDir <repo root>, or copy run-scale-reader.cmd there first)' -f $cmdPath)
}

# 1. Back up the existing task definition (rollback material).
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir | Out-Null }
    $backup = Join-Path $InstallDir ('{0}.bak-{1}.xml' -f $taskName, (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Export-ScheduledTask -TaskName $taskName | Set-Content -Path $backup -Encoding Unicode
    'backup of the existing task: ' + $backup
} else {
    'no existing task {0} - nothing to back up' -f $taskName
}

function Invoke-Main {
    # 2. Register: cmd.exe /c run-scale-reader.cmd, at startup +1 min, S4U as the owner.
    try {
        $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
            -Argument ('/c "{0}"' -f $cmdPath) -WorkingDirectory $InstallDir
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $trigger.Delay = 'PT1M'   # let Bluetooth and the network come up first
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
        'registered {0} (AtStartup +1 min, S4U as {1}) -> cmd.exe /c {2}' -f $taskName, $User, $cmdPath
    } catch {
        Invoke-Rollback ('register: ' + $_.Exception.Message)
        return
    }

    if (-not $StartNow) { return }

    # 3. Stop the old instance: the task itself and anything in another session (the interactive run.cmd).
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    foreach ($p in @(Get-ReaderProcesses)) {
        'stopping old pid {0} (session {1}): {2}' -f $p.ProcessId, $p.SessionId, $p.Name
        & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null
    }

    # 4. Start the task and wait up to 60 s for a python reader.py in session 0.
    Start-ScheduledTask -TaskName $taskName
    'started task {0}' -f $taskName

    $deadline = (Get-Date).AddSeconds(60)
    $found = $null
    while (-not $found -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $found = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match '^python' -and $_.CommandLine -match 'reader\.py' -and $_.SessionId -eq 0
        } | Select-Object -First 1
    }
    if (-not $found) {
        Invoke-Rollback ('no python reader.py in session 0 within 60 s (task state: {0})' -f `
            (Get-ScheduledTask -TaskName $taskName).State)
        return
    }
    'OK: reader.py runs in session 0 (pid {0}): {1}' -f $found.ProcessId, $found.CommandLine
}

try {
    Invoke-Main
} catch {
    Invoke-Rollback ('unexpected error: ' + $_.Exception.Message)
}
exit $script:rc