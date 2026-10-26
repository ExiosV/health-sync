#Requires -RunAsAdministrator
# Registers the Task Scheduler task that runs the health-sync Bluetooth scale reader on devicex2.
#
# The reader listens for the Mi Body Composition Scale 2 and posts to the health-sync ingest endpoint.
# It used to run as a visible cmd window in the owner's logon session (task \health-sync-scale, LogonType
# Interactive, AtLogOn, action run.cmd). This script replaces that with an S4U task: it runs in session 0
# (no console window, nothing to close by accident) and starts at boot without anyone logging on.
# A BLE scan from session 0 was verified on devicex2 on 2026-10-26 (25 devices seen vs 27 interactive).
#
#   install-scale-reader-task.ps1                    # register only (backs up the original task first)
#   install-scale-reader-task.ps1 -StartNow          # register, stop the old instance, start the task now
#   install-scale-reader-task.ps1 -Deploy -SourceDir C:\path\to\health-sync
#                                                    # stop, copy reader.py/miscale.py/run-scale-reader.cmd,
#                                                    # register, start (-Deploy implies -StartNow)
#
# Re-running is safe: the task is re-registered with the same definition and the ORIGINAL backup is kept.
#
# Order: back up the task -> stop the task and every reader process -> back up and copy the files ->
# register -> start -> wait up to 60 s for python reader.py in session 0.
#
# Deploy copies only those three files into the install dir. It never touches token.txt, pending.jsonl
# or venv. Before copying it saves reader.py, miscale.py, run-scale-reader.cmd and run.cmd (if present)
# as <name>.bak-<ts>.
#
# Task backup: the first run exports the existing task to <install dir>\health-sync-scale.bak-<ts>.xml.
# If a backup whose principal is Interactive (the original logon task) already exists, no new backup is
# made; that file stays the undo point.
#
# Rollback: if any step fails, the task and reader processes are stopped, the .bak files of this run are
# restored, the original backup XML is re-registered and started. Without a backup the old interactive
# run.cmd is started instead.
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
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = $null
$fileBackups = @()   # @{ Bak = ...; To = ... } made in this run
$script:rc = 0
if ($Deploy) { $StartNow = $true }

function Get-ReaderProcesses {
    # Only the reader itself and its wrappers, never this PowerShell or its parent.
    $self = $PID
    $parent = $null
    try {
        $parent = (Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $PID)).ParentProcessId
    } catch { }
    Get-CimInstance Win32_Process | Where-Object {
        $cl = $_.CommandLine
        $_.ProcessId -ne $self -and $_.ProcessId -ne $parent -and $cl -and (
            ($_.Name -like 'python*.exe' -and $cl -like '*reader.py*' -and $cl -like '*health-sync-scale*') -or
            ($_.Name -eq 'cmd.exe' -and (
                $cl -like '*run-scale-reader.cmd*' -or
                ($cl -like '*\run.cmd*' -and $cl -like '*health-sync-scale*')))
        )
    }
}

function Stop-Reader {
    # Never throws: python first (so the wrapper does not restart it in between), then the wrappers.
    try { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue } catch { }
    $procs = @()
    try { $procs = @(Get-ReaderProcesses) } catch { 'could not list processes: ' + $_.Exception.Message }
    foreach ($p in @($procs | Where-Object { $_.Name -like 'python*' }) + @($procs | Where-Object { $_.Name -eq 'cmd.exe' })) {
        'stopping pid {0} (session {1}): {2}' -f $p.ProcessId, $p.SessionId, $p.Name
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
}

function Show-ReaderState {
    $procs = @()
    try { $procs = @(Get-ReaderProcesses) } catch { }
    if ($procs.Count -eq 0) { 'reader processes: none'; return }
    foreach ($p in $procs) {
        'pid {0}, session {1}, {2}' -f $p.ProcessId, $p.SessionId, $p.Name
    }
}

function Test-InteractiveXml([string]$path) {
    try {
        [xml]$x = Get-Content $path -Raw
        foreach ($pr in @($x.Task.Principals.Principal)) {
            if ($pr.LogonType -eq 'InteractiveToken') { return $true }
        }
    } catch { }
    return $false
}

function Invoke-Rollback([string]$reason) {
    # Must never throw: every step is guarded so the backup is always re-registered.
    'FAILED: ' + $reason
    $script:rc = 1
    Stop-Reader
    foreach ($f in $fileBackups) {
        try {
            Copy-Item -Path $f.Bak -Destination $f.To -Force -ErrorAction Stop
            'restored {0} from {1}' -f $f.To, $f.Bak
        } catch {
            'rollback: restore of {0} failed: {1}' -f $f.To, $_.Exception.Message
        }
    }
    if ($backup) {
        try {
            Register-ScheduledTask -Xml (Get-Content $backup -Raw) -TaskName $taskName -Force -ErrorAction Stop | Out-Null
            're-registered {0} from {1}' -f $taskName, $backup
            Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
            'started the restored task'
        } catch {
            'rollback: re-register/start from {0} failed: {1}' -f $backup, $_.Exception.Message
        }
    } else {
        'no backup of the previous task exists - manual recovery (elevated PowerShell):'
        '  Unregister-ScheduledTask -TaskName {0} -Confirm:$false; & {1}' -f $taskName, $FallbackCmd
        try {
            if (Test-Path $FallbackCmd) {
                'starting the old interactive wrapper: ' + $FallbackCmd
                Start-Process -FilePath $FallbackCmd -ErrorAction Stop
            }
        } catch {
            'rollback: start of {0} failed: {1}' -f $FallbackCmd, $_.Exception.Message
        }
    }
    'ROLLED BACK'
    Show-ReaderState
}

# 0. Pre-checks that need no rollback (nothing has been changed yet).
$copies = @()
if ($Deploy) {
    if (-not $SourceDir) {
        throw '-Deploy needs -SourceDir (the health-sync repo root, e.g. C:\work\health-sync)'
    }
    $copies = @(
        @{ From = Join-Path $SourceDir 'health_sync\scale\reader.py';          To = Join-Path $InstallDir 'reader.py' },
        @{ From = Join-Path $SourceDir 'health_sync\scale\miscale.py';         To = Join-Path $InstallDir 'miscale.py' },
        @{ From = Join-Path $SourceDir 'scripts\windows\run-scale-reader.cmd'; To = Join-Path $InstallDir 'run-scale-reader.cmd' }
    )
    foreach ($c in $copies) {
        if (-not (Test-Path $c.From)) { throw ('source file missing: ' + $c.From) }
    }
}
$cmdPath = Join-Path $InstallDir 'run-scale-reader.cmd'
if (-not $Deploy -and -not (Test-Path $cmdPath)) {
    throw ('wrapper not found: {0} (pass -Deploy -SourceDir <repo root>, or copy run-scale-reader.cmd there first)' -f $cmdPath)
}
if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir | Out-Null }

# 1. Task backup: keep the FIRST one (the original Interactive task) as the undo point.
$original = Get-ChildItem -Path $InstallDir -Filter ('{0}.bak-*.xml' -f $taskName) -ErrorAction SilentlyContinue |
    Sort-Object Name | Where-Object { Test-InteractiveXml $_.FullName } | Select-Object -First 1
if ($original) {
    $backup = $original.FullName
    'original task backup (Interactive, kept as the undo point, no new backup made): ' + $backup
} elseif (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    $backup = Join-Path $InstallDir ('{0}.bak-{1}.xml' -f $taskName, $ts)
    Export-ScheduledTask -TaskName $taskName | Set-Content -Path $backup -Encoding Unicode
    'backup of the existing task (this is the original undo point): ' + $backup
} else {
    'no existing task {0} - nothing to back up' -f $taskName
}

function Invoke-Main {
    # 2. Stop the task and every reader instance BEFORE touching files.
    if ($StartNow) { Stop-Reader }

    # 3. Back up the installed files, then copy the new ones.
    if ($Deploy) {
        foreach ($name in @('reader.py', 'miscale.py', 'run-scale-reader.cmd', 'run.cmd')) {
            $path = Join-Path $InstallDir $name
            if (Test-Path $path) {
                $bak = '{0}.bak-{1}' -f $path, $ts
                Copy-Item -Path $path -Destination $bak -Force
                $script:fileBackups += @{ Bak = $bak; To = $path }
                'file backup: ' + $bak
            }
        }
        foreach ($c in $copies) {
            Copy-Item -Path $c.From -Destination $c.To -Force
            'deployed {0} -> {1}' -f $c.From, $c.To
        }
    }

    # 4. Register: cmd.exe /c run-scale-reader.cmd, S4U as the owner.
    #    Trigger 1: at startup +1 min. Trigger 2: every 5 min, so the task comes back if the wrapper dies
    #    (MultipleInstances IgnoreNew makes the repetition a no-op while the wrapper runs).
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument ('/c "{0}"' -f $cmdPath) -WorkingDirectory $InstallDir
    $boot = New-ScheduledTaskTrigger -AtStartup
    $boot.Delay = 'PT1M'   # let Bluetooth and the network come up first
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($boot, $repeat) `
        -Settings $settings -Principal $principal -Force | Out-Null
    'registered {0} (AtStartup +1 min and every 5 min, S4U as {1}) -> cmd.exe /c {2}' -f $taskName, $User, $cmdPath

    if (-not $StartNow) { return }

    # 5. Start the task and wait up to 60 s for a python reader.py in session 0.
    Start-ScheduledTask -TaskName $taskName
    'started task {0}' -f $taskName

    $deadline = (Get-Date).AddSeconds(60)
    $found = $null
    while (-not $found -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $found = @(Get-ReaderProcesses) | Where-Object {
            $_.Name -like 'python*' -and $_.SessionId -eq 0
        } | Select-Object -First 1
    }
    if (-not $found) {
        $state = '?'
        try { $state = (Get-ScheduledTask -TaskName $taskName).State } catch { }
        Invoke-Rollback ('no python reader.py in session 0 within 60 s (task state: {0})' -f $state)
        return
    }
    'OK: reader.py runs in session 0 (pid {0}): {1}' -f $found.ProcessId, $found.CommandLine
    if ($backup) { 'undo point (original task): ' + $backup }
}

try {
    Invoke-Main
} catch {
    Invoke-Rollback ('unexpected error: ' + $_.Exception.Message)
}
exit $script:rc
