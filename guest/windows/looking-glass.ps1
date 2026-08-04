<#
    Looking Glass host, guest side.

    THE VERSION MUST MATCH THE HOST'S CLIENT (K2). The client here is the AUR
    package looking-glass B7-7, so the guest gets the B7 host binary. A mismatch
    does not degrade gracefully: the client refuses the shared memory segment.
    Check the host side with `looking-glass-client --version` before changing
    $Version.

    Since B6 the Windows installer CARRIES THE IVSHMEM DRIVER -- there is no
    separate download, and the old "grab virtio-win-08032022.zip" instruction
    from B5-era guides is obsolete. Verified: the 08032022 archive does not even
    contain an ivshmem driver any more.

    Run it the same way as guest/windows/vdd.ps1, through the agent as SYSTEM:

        push.sh guest/windows/looking-glass.ps1 C:\Windows\Temp\vfioctl-lg.ps1
        powershell -ExecutionPolicy Bypass -File C:\Windows\Temp\vfioctl-lg.ps1

    PRECONDITION: the ivshmem device must already be in the domain, otherwise
    the installer stages a driver with nothing to bind to and the host service
    starts and immediately fails. The domain block lives in libvirt's own store
    (K10) -- `virsh -c qemu:///system edit win11`, qemu:commandline with
    mem-path=/dev/kvmfr0, size matching the host's static_size_mb.
#>
[CmdletBinding()]
param(
    [string]$Version = 'B7',
    [string]$Url = 'https://looking-glass.io/artifact/B7/host',
    [string]$Sha256 = 'c2415a5a0c405f1d6aa936986bdd4b806c50574b4521747e113c3be2be047b1b',
    [string]$LogPath = 'C:\Windows\Temp\lg-install.log'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Start-Transcript -Path $LogPath -Force | Out-Null
function Say { param([string]$m) Write-Host ("== {0}" -f $m) }

try {
    $tmp = Join-Path $env:TEMP ('lg-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null

    # Fail early and loudly if the ivshmem device is absent -- installing on top
    # of a missing device produces a service that looks installed and never works.
    $ivshmem = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'PCI\VEN_1AF4&DEV_1110*' }
    if (-not $ivshmem) { throw 'no ivshmem device (PCI\VEN_1AF4&DEV_1110) in this guest -- fix the domain XML first' }
    Say "ivshmem device present: $($ivshmem.FriendlyName) [$($ivshmem.Status)]"

    $zip = Join-Path $tmp 'lg-host.zip'
    Say "download $Url"
    & curl.exe -sSL --fail --retry 3 -o $zip $Url
    if ($LASTEXITCODE -ne 0) { throw "download failed ($LASTEXITCODE)" }
    $got = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
    if ($got -ne $Sha256.ToLower()) { throw "sha256 mismatch`n  want $Sha256`n  got  $got" }
    Say 'sha256 ok'

    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $setup = Get-ChildItem -Path $tmp -Filter 'looking-glass-host-setup.exe' -Recurse | Select-Object -First 1
    if (-not $setup) { throw 'looking-glass-host-setup.exe not found in the archive' }

    # /S = silent, default options. The installer needs administrator rights,
    # which SYSTEM has; it installs the IVSHMEM driver, the host binary and the
    # "Looking Glass (host)" service in one pass.
    Say "installing $Version host silently"
    $p = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -Wait -PassThru
    Say "installer exit code $($p.ExitCode)"

    Start-Sleep -Seconds 10

    Say 'ivshmem device after install'
    Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'PCI\VEN_1AF4&DEV_1110*' } |
        Select-Object Status, Problem, FriendlyName, InstanceId | Format-List

    Say 'service'
    Get-Service | Where-Object { $_.DisplayName -like '*Looking Glass*' -or $_.Name -like '*looking*' } |
        Select-Object Name, DisplayName, Status, StartType | Format-List

    Say 'install log tail (the host writes its own)'
    $hostLog = 'C:\Program Files\Looking Glass (host)\looking-glass-host.txt'
    if (Test-Path $hostLog) { Get-Content $hostLog -Tail 25 } else { Say "no $hostLog yet" }
    Say 'done'
}
catch {
    Write-Host "FAILED: $_"
    Write-Host $_.ScriptStackTrace
}
finally {
    if ($tmp -and (Test-Path $tmp)) { Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
}
