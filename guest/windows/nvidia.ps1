<#
    The NVIDIA display driver, guest side -- the step that makes a handed-over
    card usable rather than merely present.

    WHY IT EXISTS, MEASURED TWICE ON THIS HARDWARE. A passed-through GPU with
    no vendor driver is not a working adapter: Windows binds its inbox
    "Microsoft Basic Display Adapter" to it, that driver cannot drive a
    non-primary card, and the device sits at problem=10 (CM_PROB_DEVICE_FAILED,
    ProblemStatus 0xC01E02B8). Everything downstream then fails in ways that
    point somewhere else -- Looking Glass finds no D3D12/DXGI adapter, exits,
    and its service faithfully restarts it while reporting Running.

    AND THE NAME MATTERS AS MUCH AS THE DRIVER. Until this runs, the card's
    friendly name is "Microsoft Basic Display Adapter" -- the same string the
    emulated QXL adapter carries, so the name vdd.ps1 is given matches two
    adapters and the indirect display can end up rendering on the wrong one.
    With the driver installed the card becomes "NVIDIA GeForce RTX 3060 Laptop
    GPU" and the choice is unambiguous. Hence the order: this, then VDD.

    NOTHING IS DOWNLOADED TO THE HOST. The guest fetches the package over its
    own network, the same way the other guest scripts do.

    Run it as SYSTEM through the agent, like the others:

        ./vfioctl guest --name <domain> setup

    which runs it only when the domain actually claims a PCI function -- there
    is nothing to install on the cardless rehearsal.

    THE VERSION IS PINNED, AND TO THE ONE THIS MACHINE HAS ALREADY PROVEN.
    610.62 is what the working guest runs (device OK, no Code 43, guest
    nvidia-smi reporting the card) and it shares a branch with the host's
    kernel module. NVIDIA publishes no checksum, so $Sha256 is one measured
    here on first download rather than an upstream value; changing $Version
    means measuring a new one. The -notebook- and -desktop- URLs return the
    same bytes -- the package is unified -- so no product lookup is needed.

    NO GEFORCE EXPERIENCE / NVIDIA APP: the component list is deliberately
    Display.Driver + HDAudio.Driver and nothing else.
#>
[CmdletBinding()]
param(
    [string]$Version = '610.62',
    [string]$Url = 'https://us.download.nvidia.com/Windows/610.62/610.62-notebook-win10-win11-64bit-international-dch-whql.exe',
    # Measured here on 2026-08-05: 978 406 136 bytes, matching the CDN's own
    # Content-Length. NVIDIA publishes no checksum to compare against, so this
    # is a value this machine took once and every later run is compared to.
    [string]$Sha256 = 'dfe395bbc971825cf30884bc49990a7527b9da72f8df947fb9f196265712d5a5',
    # Kept rather than deleted: it is ~930 MB, and a re-run that has to fetch it
    # again turns an idempotent step into three minutes of download. Named per
    # version so a version change cannot silently reuse the old package.
    [string]$InstallerPath = "C:\Windows\Temp\nvidia-$Version.exe",
    [string]$LogPath = 'C:\Windows\Temp\nvidia-install.log'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Start-Transcript -Path $LogPath -Force | Out-Null
function Say { param([string]$m) Write-Host ("== {0}" -f $m) }

function Get-NvidiaDevice {
    Get-PnpDevice -Class Display -PresentOnly |
        Where-Object { $_.InstanceId -like 'PCI\VEN_10DE*' } |
        Select-Object -First 1
}

try {
    # Fail at the precondition rather than three steps later: with no NVIDIA
    # function in the guest there is nothing here to install a driver for, and
    # that is a domain problem, not a driver problem.
    $dev = Get-NvidiaDevice
    if (-not $dev) {
        throw 'no NVIDIA device (PCI\VEN_10DE) in this guest -- the domain has no hostdev, or the card did not enumerate'
    }
    Say "device: $($dev.FriendlyName) [$($dev.Status) problem=$($dev.Problem)]"

    # Idempotence, and it is read from the guest rather than from a marker file:
    # a card that is already problem=0 under a driver naming itself NVIDIA has
    # nothing to gain from a reinstall.
    if ($dev.Problem -eq 0 -and $dev.FriendlyName -match 'NVIDIA') {
        Say 'driver already installed and the device is running -- nothing to do'
        Say 'done'
        return
    }

    if ((Test-Path -LiteralPath $InstallerPath) -and $Sha256) {
        $have = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLower()
        if ($have -eq $Sha256.ToLower()) { Say 'installer already downloaded, sha256 ok' }
        else { Remove-Item -LiteralPath $InstallerPath -Force }
    }

    if (-not (Test-Path -LiteralPath $InstallerPath)) {
        Say "download $Url"
        & curl.exe -sSL --fail --retry 3 -o $InstallerPath $Url
        if ($LASTEXITCODE -ne 0) { throw "download failed ($LASTEXITCODE)" }
    }

    $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLower()
    if (-not $Sha256) {
        # An unpinned run is a measurement, not an install: the whole point of
        # the hash is that nobody downstream can tell a truncated or swapped
        # package from a good one, and NVIDIA publishes nothing to compare to.
        throw "no `$Sha256 pinned. Measured now: $got  (`$Version=$Version)"
    }
    if ($got -ne $Sha256.ToLower()) {
        throw "sha256 mismatch`n  want $Sha256`n  got  $got"
    }
    Say "sha256 ok ($((Get-Item -LiteralPath $InstallerPath).Length) bytes)"

    # -s silent, -noreboot because the driver binds in this session (measured),
    # -nofinish so nothing waits for a click nobody is there to give.
    Say "installing $Version (Display.Driver HDAudio.Driver)"
    $p = Start-Process -FilePath $InstallerPath -Wait -PassThru -ArgumentList @(
        '-s', '-noreboot', '-nofinish', 'Display.Driver', 'HDAudio.Driver')
    Say "installer exit code $($p.ExitCode)"

    Start-Sleep -Seconds 15

    # THE VERDICT IS THE DEVICE, NOT THE EXIT CODE. An installer that returns 0
    # having bound nothing leaves exactly the state this script was written for.
    $dev = Get-NvidiaDevice
    Say 'result'
    $dev | Select-Object Status, Problem, FriendlyName, InstanceId | Format-List
    Say 'adapters'
    Get-CimInstance Win32_VideoController |
        Select-Object Name, DriverVersion, PNPDeviceID | Format-List
    if (-not $dev -or $dev.Problem -ne 0) {
        throw "the card is still not running (problem=$($dev.Problem)) after the install"
    }
    Say 'done'
}
catch {
    # See vdd.ps1: the verdict has to reach the exit code, or the driver moves
    # on to the next step standing on a broken one.
    Write-Host "FAILED: $_"
    Write-Host $_.ScriptStackTrace
    $script:Failed = $true
}
finally {
    Stop-Transcript | Out-Null
    if ($script:Failed) { exit 1 }
}
