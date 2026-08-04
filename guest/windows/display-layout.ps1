<#
    Leave the guest with exactly one display -- the Virtual Display Driver's --
    so Looking Glass shows a desktop instead of an empty wall.

    WHY: the guest comes up with two active monitors, the emulated QXL one and
    VDD's. Windows keeps QXL primary because it existed first, so the taskbar and
    every new window land on the display Looking Glass does NOT capture, and the
    mouse can wander onto a screen nobody can see. Measured: `virsh screenshot`
    (which reads QXL) showed the full desktop while the LG client showed a bare
    2560x1440 wallpaper.

    WHY DisplaySwitch AND NOT ChangeDisplaySettingsEx: the legacy API cannot do
    this on Windows 11 25H2. Both CDS_SET_PRIMARY on the VDD and a plain detach
    of the QXL display return -1 (DISP_CHANGE_FAILED), with or without an
    elevated token -- topology now belongs to the CCD API (SetDisplayConfig).
    DisplaySwitch.exe is Windows' own CCD front end and does the job in one call.
    Measured 2026-08-04; do not spend another hour on the CDS path.

    WHY /internal AND /external ARE TRIED IN TURN: which of the two Windows
    considers "internal" is not something to guess. On this guest the INDIRECT
    display (VDD) is the internal one and the emulated QXL is external, which is
    the opposite of what the names suggest. So the script switches, verifies, and
    if it got the wrong one switches the other way. Nothing is hardcoded.

    WHY IT NEEDS A SCHEDULED TASK: the qemu guest agent runs in session 0.
    Display configuration is per-session and session 0 has no display devices at
    all. The script re-launches itself into the console session through a
    one-shot task with /IT (interactive token, needs no stored password) and
    /RL HIGHEST.

    Run it as SYSTEM through the agent, like the other guest scripts. NOTE the
    target path: it must be somewhere the console user can READ, and
    C:\Windows\Temp is not (the task silently produced nothing until this moved).

        push.sh guest/windows/display-layout.ps1 C:\Users\Public\6-misafir-ekran.ps1
        powershell -ExecutionPolicy Bypass -File C:\Users\Public\6-misafir-ekran.ps1

    The way back, if VDD ever fails to come up and the guest is left blind:

        powershell -File C:\Users\Public\6-misafir-ekran.ps1 -Reattach

    It is idempotent: if the target is already the only attached display it says
    so and changes nothing.
#>
[CmdletBinding()]
param(
    # Which adapter must survive. Matched against the display device's
    # DeviceString; the VDD registers as "Virtual Display Driver".
    [string]$AdapterMatch = 'Virtual Display Driver',
    # Restore the multi-display desktop instead (DisplaySwitch /extend).
    [switch]$Reattach,
    [string]$TaskName = 'qemu-vfio-display-topology',
    # C:\Users\Public, not C:\Windows\Temp: the task runs as the console user,
    # who can neither read scripts from nor write logs under C:\Windows.
    [string]$LogPath = 'C:\Users\Public\qemu-vfio-display.log',
    # Set when the script is re-entered inside the console session.
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$argFile = 'C:\Users\Public\qemu-vfio-display.json'

# ---------------------------------------------------------------- bootstrap half
if (-not $Apply) {
    if (Test-Path $LogPath) { Remove-Item $LogPath -Force }

    # `query session` is the only thing that knows who is at the console, and a
    # hardcoded user name is exactly the machine-specific value K9 forbids.
    $consoleUser = (query session 2>$null |
        Where-Object { $_ -match '^\s*>?console\s+(\S+)\s+\d+\s+Active' } |
        ForEach-Object { $Matches[1] }) | Select-Object -First 1
    if (-not $consoleUser) { throw 'no active console session -- log into the guest first' }
    Write-Host "== console session user: $consoleUser"

    # Arguments travel in a sidecar file, not on the task's command line:
    # schtasks /tr mangles inner quoting, and -AdapterMatch "Virtual Display
    # Driver" came back as ERROR: Invalid argument/option - 'Display'.
    @{ AdapterMatch = $AdapterMatch; Reattach = [bool]$Reattach; LogPath = $LogPath } |
        ConvertTo-Json | Set-Content -Path $argFile -Encoding UTF8

    $cmd = "powershell.exe -ExecutionPolicy Bypass -NoProfile -File $PSCommandPath -Apply"
    & schtasks.exe /create /tn $TaskName /tr $cmd /sc ONCE /st 00:00 /ru $consoleUser /it /rl HIGHEST /f | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks /create exit code $LASTEXITCODE" }
    try {
        & schtasks.exe /run /tn $TaskName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "schtasks /run exit code $LASTEXITCODE" }
        Write-Host '== launched in the console session, waiting for its log'
        for ($i = 0; $i -lt 45; $i++) {
            Start-Sleep -Seconds 2
            if ((Test-Path $LogPath) -and (Get-Content $LogPath -Raw) -match 'DONE|FAILED') { break }
        }
    }
    finally { & schtasks.exe /delete /tn $TaskName /f 2>&1 | Out-Null }

    if (Test-Path $LogPath) { Get-Content $LogPath } else { Write-Host 'no log -- the task produced nothing' }
    return
}

# -------------------------------------------------------------- the working half
# Runs in the console session. Its only way to report is the log file.
if (Test-Path $argFile) {
    $a = Get-Content $argFile -Raw | ConvertFrom-Json
    if ($a.AdapterMatch) { $AdapterMatch = $a.AdapterMatch }
    if ($a.LogPath) { $LogPath = $a.LogPath }
    $Reattach = [bool]$a.Reattach
}
function Log { param([string]$m) Add-Content -Path $LogPath -Value $m }

try {
    Add-Type @'
using System;
using System.Runtime.InteropServices;

public static class Disp
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DISPLAY_DEVICE
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]  public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    public const int ATTACHED_TO_DESKTOP = 0x00000001;
    public const int PRIMARY_DEVICE      = 0x00000004;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern bool EnumDisplayDevices(string device, uint num, ref DISPLAY_DEVICE dd, uint flags);
}
'@

    # Every display device, attached or not. An empty list means the API is not
    # reachable from here (session 0) and must not be read as "no displays".
    function Get-Displays {
        $out = @()
        $dd = New-Object Disp+DISPLAY_DEVICE
        for ($i = 0; $i -lt 16; $i++) {
            $dd.cb = [Runtime.InteropServices.Marshal]::SizeOf($dd)
            # [NullString]::Value, not $null: PowerShell marshals a bare $null to
            # an EMPTY string for [string] P/Invoke parameters, and
            # EnumDisplayDevices("") just returns FALSE -- which reads exactly
            # like "this session has no displays" (2026-08-04, cost an hour).
            if (-not [Disp]::EnumDisplayDevices([NullString]::Value, $i, [ref]$dd, 0)) { break }
            $out += [pscustomobject]@{
                Name     = $dd.DeviceName
                Desc     = $dd.DeviceString
                Attached = (($dd.StateFlags -band [Disp]::ATTACHED_TO_DESKTOP) -ne 0)
                Primary  = (($dd.StateFlags -band [Disp]::PRIMARY_DEVICE) -ne 0)
            }
        }
        return $out
    }

    function Show-Displays {
        param([string]$title, $displays)
        Log "== $title"
        if (-not $displays) { Log '   (none -- EnumDisplayDevices returned nothing)'; return }
        foreach ($d in $displays) {
            Log ("   {0,-14} attached={1,-5} primary={2,-5} {3}" -f $d.Name, $d.Attached, $d.Primary, $d.Desc)
        }
    }

    # The goal: the target is attached, and it is the only one.
    function Test-Goal {
        $att = @(Get-Displays | Where-Object { $_.Attached })
        return ($att.Count -eq 1 -and $att[0].Desc -like "*$AdapterMatch*")
    }

    function Invoke-Switch {
        param([string]$mode)
        Log "== DisplaySwitch.exe /$mode"
        & "$env:SystemRoot\System32\DisplaySwitch.exe" "/$mode"
        Start-Sleep -Seconds 6
    }

    Log ("== running as {0}\{1}, session {2}" -f $env:USERDOMAIN, $env:USERNAME, (Get-Process -Id $PID).SessionId)
    Show-Displays 'before' (Get-Displays)

    if ($Reattach) {
        Invoke-Switch 'extend'
        Show-Displays 'after' (Get-Displays)
        Log 'DONE'
        return
    }

    if (Test-Goal) {
        Log "== '$AdapterMatch' is already the only attached display, nothing to do"
        Log 'DONE'
        return
    }

    # Try both topologies rather than betting on the naming.
    $ok = $false
    foreach ($mode in 'internal', 'external') {
        Invoke-Switch $mode
        if (Test-Goal) { $ok = $true; break }
        Log "   /$mode did not leave '$AdapterMatch' as the only display"
    }

    if (-not $ok) {
        Log '== neither topology worked, restoring the extended desktop'
        Invoke-Switch 'extend'
        Show-Displays 'after' (Get-Displays)
        Log "FAILED: could not isolate '$AdapterMatch'"
        return
    }

    Show-Displays 'after' (Get-Displays)
    Log 'DONE'
}
catch {
    Log "FAILED: $_"
    Log $_.ScriptStackTrace
}
