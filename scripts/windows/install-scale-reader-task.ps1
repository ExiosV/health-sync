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
# Deploy copies only those three files into the install dir. It never changes the content of token.txt,
# pending.jsonl or venv (only their ACLs, see "Install dir lock" below). Before copying it saves reader.py, miscale.py, run-scale-reader.cmd and run.cmd (if present)
# as <name>.bak-<ts>.
#
# Task backup: the first run exports the existing task to <install dir>\health-sync-scale.bak-<ts>.xml.
# If a backup whose principal is Interactive (the original logon task) already exists, no new backup is
# made; that file stays the undo point.
#
# Rollback: if any step fails, the task and reader processes are stopped, the .bak files of this run are
# restored, the original backup XML is re-registered and started. Without a backup the old interactive
# run.cmd is started instead.
#
# Install dir lock: the task runs elevated (RunLevel Highest), so a user who can write the install dir
# can run code as admin. The script creates the install dir (and each missing parent) and locks it:
# owner Administrators, no inheritance, SYSTEM + Administrators FullControl, Users ReadAndExecute;
# token.txt gets SYSTEM + Administrators only. Then it refuses to register the task if the install dir,
# the files the task runs, or any parent folder can be changed by a non-admin, or if a path component
# is a reparse point. Never install into C:\Tools: Authenticated Users can write there.
# Host inventory of devicex2 tasks: exiosv-lab-infra docs/k8s/devicex2-host-tasks.md.
param(
    [string]$InstallDir = 'C:\ProgramData\omb-tech-watchdog\bin\health-sync-scale',
    [string]$User = 'DEVICEX2\igorp',
    [string]$SourceDir = '',
    [string]$FallbackCmd = '',   # default: <InstallDir>\run.cmd
    [switch]$Deploy,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if ($InstallDir -notmatch '^[A-Za-z]:\\.+') {
    throw ('install dir must be a local absolute path below a drive root: ' + $InstallDir)
}
if (-not $FallbackCmd) { $FallbackCmd = Join-Path $InstallDir 'run.cmd' }
$icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
$cmdExe = $env:ComSpec
if (-not $cmdExe -or -not (Test-Path -LiteralPath $cmdExe)) {
    $cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
}
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

# --- Install dir lock -------------------------------------------------------------------------------
# SIDs, not names, so the script works on any Windows display language.
$sidSystem = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$sidAdmins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
$sidUsers = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-545')
$trustedSids = @(
    'S-1-5-18',       # SYSTEM
    'S-1-5-32-544',   # BUILTIN\Administrators
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'   # NT SERVICE\TrustedInstaller
)
# Rights that let a holder change an object: WriteData 2, AppendData 4, WriteExtendedAttributes 16,
# DeleteSubdirectoriesAndFiles 64, WriteAttributes 256, Delete 65536, ChangePermissions 262144,
# TakeOwnership 524288, GENERIC_ALL 0x10000000, GENERIC_WRITE 0x40000000.
$maskWrite = 2 -bor 4 -bor 16 -bor 64 -bor 256 -bor 65536 -bor 262144 -bor 524288 -bor 0x10000000 -bor 0x40000000
# For a parent folder only the rights that can remove, rename or re-ACL the existing child matter.
# (C:\ and C:\ProgramData let Users create new entries; that cannot replace an existing child.)
$maskParent = 64 -bor 65536 -bor 262144 -bor 524288 -bor 0x10000000

function Get-ReparsePoints([string]$root) {
    # Every junction or symlink below $root. The walk never descends into a reparse point.
    $found = @()
    $stack = New-Object System.Collections.Stack
    $stack.Push((New-Object System.IO.DirectoryInfo($root)))
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        foreach ($i in $dir.GetFileSystemInfos()) {
            if ($i.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { $found += $i.FullName; continue }
            if ($i -is [System.IO.DirectoryInfo]) { $stack.Push($i) }
        }
    }
    return $found
}

function Set-LockedDirAcl([string]$path, [switch]$Recurse) {
    # Owner Administrators, protected DACL: SYSTEM + Administrators FullControl, Users ReadAndExecute.
    $sec = New-Object System.Security.AccessControl.DirectorySecurity
    $sec.SetOwner($sidAdmins)
    $sec.SetAccessRuleProtection($true, $false)
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $none = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    foreach ($r in @(@($sidSystem, 'FullControl'), @($sidAdmins, 'FullControl'), @($sidUsers, 'ReadAndExecute'))) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $r[0], [System.Security.AccessControl.FileSystemRights]$r[1], $inherit, $none, $allow)
        $sec.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $path -AclObject $sec
    if (-not $Recurse) { return }
    # Children: owner Administrators, drop explicit ACEs, inherit only the locked DACL.
    # Refuse first if the tree holds a reparse point: a recursive icacls must never reach the ACL of a
    # target outside the install dir. /L also makes icacls act on a link itself, not on its target.
    $links = @(Get-ReparsePoints $path)
    if ($links.Count -gt 0) {
        foreach ($l in $links) { '  reparse point: ' + $l }
        throw ('install dir {0} holds {1} reparse point(s); remove them, then re-run' -f $path, $links.Count)
    }
    if (Get-ChildItem -LiteralPath $path -Force | Select-Object -First 1) {
        & $icacls (Join-Path $path '*') /reset /T /C /L /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ('icacls /reset failed on {0} (exit {1})' -f $path, $LASTEXITCODE) }
        & $icacls $path /setowner '*S-1-5-32-544' /T /C /L /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ('icacls /setowner failed on {0} (exit {1})' -f $path, $LASTEXITCODE) }
    }
}

function Set-AdminOnlyFileAcl([string]$path) {
    # Owner Administrators, protected DACL: SYSTEM + Administrators FullControl, nobody else.
    $sec = New-Object System.Security.AccessControl.FileSecurity
    $sec.SetOwner($sidAdmins)
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($sidSystem, $sidAdmins)) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid, [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow)
        $sec.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $path -AclObject $sec
}

function Get-AclProblems([string]$path, [int]$mask, [switch]$AnyAccess) {
    # Returns one line per problem: a non-trusted owner, or a non-trusted Allow ACE that applies to the
    # object itself (not inherit-only) with a right in $mask (with -AnyAccess: with any right).
    $problems = @()
    $item = Get-Item -LiteralPath $path -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        $problems += ('{0}: is a reparse point (junction or symlink)' -f $path)
    }
    $acl = Get-Acl -LiteralPath $path
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($trustedSids -notcontains $owner) { $problems += ('{0}: owner {1} is not SYSTEM/Administrators' -f $path, $owner) }
    foreach ($ace in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        if ($ace.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { continue }
        if ($ace.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $sid = $ace.IdentityReference.Value
        if ($trustedSids -contains $sid) { continue }
        $rights = [int]$ace.FileSystemRights
        if ($AnyAccess -or (($rights -band $mask) -ne 0)) {
            $problems += ('{0}: {1} has {2}' -f $path, $sid, $ace.FileSystemRights)
        }
    }
    return $problems
}

function Assert-InstallDirLocked {
    # Refuse an elevated task in a folder that a non-admin can change.
    $problems = @()
    $problems += @(Get-AclProblems $InstallDir $maskWrite)
    if (-not ((Get-Item -LiteralPath $InstallDir -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        foreach ($l in @(Get-ReparsePoints $InstallDir)) {
            $problems += ('{0}: is a reparse point (junction or symlink) inside the install dir' -f $l)
        }
    }
    foreach ($name in @('run-scale-reader.cmd', 'reader.py', 'miscale.py', 'venv', 'venv\Scripts\python.exe')) {
        $p = Join-Path $InstallDir $name
        if (Test-Path -LiteralPath $p) { $problems += @(Get-AclProblems $p $maskWrite) }
    }
    $token = Join-Path $InstallDir 'token.txt'
    if (Test-Path -LiteralPath $token) { $problems += @(Get-AclProblems $token 0 -AnyAccess) }
    $parent = [System.IO.Directory]::GetParent($InstallDir)
    while ($parent) {
        $problems += @(Get-AclProblems $parent.FullName $maskParent)
        $parent = $parent.Parent
    }
    if ($problems.Count -gt 0) {
        'install dir is not admin-only; refusing to register an elevated task:'
        foreach ($p in $problems) { '  ' + $p }
        throw ('install dir {0} or a parent can be changed by a non-admin' -f $InstallDir)
    }
    'install dir lock verified: ' + $InstallDir
}

function Initialize-InstallDir {
    # Create each missing parent and the install dir; lock what this run created and the install dir.
    $missing = @()
    $d = [System.IO.DirectoryInfo]$InstallDir
    while ($d -and -not $d.Exists) { $missing = @($d.FullName) + $missing; $d = $d.Parent }
    foreach ($m in $missing) {
        New-Item -ItemType Directory -Path $m | Out-Null
        if ($m -ne $InstallDir) { Set-LockedDirAcl $m; 'created and locked parent ' + $m }
    }
    if ((Get-Item -LiteralPath $InstallDir -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw ('install dir is a reparse point (junction or symlink); refusing to lock its target: ' + $InstallDir)
    }
    Set-LockedDirAcl $InstallDir -Recurse
    'locked install dir (owner Administrators; SYSTEM + Administrators FullControl, Users ReadAndExecute): ' + $InstallDir
    $token = Join-Path $InstallDir 'token.txt'
    if (Test-Path -LiteralPath $token) {
        Set-AdminOnlyFileAcl $token
        'locked token.txt (SYSTEM + Administrators only)'
    } else {
        'WARNING: no token.txt in the install dir; copy it there and re-run to lock it'
    }
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
if ($InstallDir -like 'C:\Tools\*' -or $InstallDir -eq 'C:\Tools') {
    throw ('refusing install dir under C:\Tools (Authenticated Users can write there): ' + $InstallDir)
}
# Create and lock the install dir, then verify the lock before anything else changes.
Initialize-InstallDir
Assert-InstallDirLocked

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

    # 4. Register: cmd.exe /c set INSTALL_DIR=<dir>&& "<dir>\run-scale-reader.cmd", S4U as the owner.
    #    Full path to cmd.exe ($env:ComSpec). INSTALL_DIR is set so the wrapper never falls back to its
    #    built-in default. Re-check the lock first: the deploy step above has just written files.
    #    Trigger 1: at startup +1 min. Trigger 2: every 5 min, so the task comes back if the wrapper dies
    #    (MultipleInstances IgnoreNew makes the repetition a no-op while the wrapper runs).
    Assert-InstallDirLocked
    $taskArg = '/c set INSTALL_DIR={0}&& "{1}"' -f $InstallDir, $cmdPath
    $action = New-ScheduledTaskAction -Execute $cmdExe -Argument $taskArg -WorkingDirectory $InstallDir
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
    'registered {0} (AtStartup +1 min and every 5 min, S4U as {1}) -> {2} {3}' -f $taskName, $User, $cmdExe, $taskArg

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
