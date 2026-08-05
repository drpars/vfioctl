<#
    Virtual Display Driver (VDD), guest side.

    WHY IT EXISTS: the dGPU has no physical display attached (measured:
    nvidia-smi display_attached=No), so Windows never builds a desktop on it and
    Looking Glass would have nothing to capture. VDD is the software substitute
    for a dummy plug.

    HOW IT IS RUN: pushed into the guest with the qemu guest agent and executed
    as NT AUTHORITY\SYSTEM -- no SSH, no network share, no interactive session.
    The channel is the guest agent; commands land as SYSTEM in session 0.

        ./vfioctl guest --name <domain> setup

    which pushes it to C:\Users\Public\vfioctl\ and runs it there, then reads
    the guest's monitor inventory back rather than trusting this script's word.

    NOTHING MACHINE-SPECIFIC IS BAKED IN (K9): every URL, hash, the GPU name and
    the resolution are parameters, and the last two are measured in the guest and
    on the host rather than defaulted to. The defaults are what was measured to
    work on this host, and they are what a caller that measured nothing gets.

    Upstream reference: the project's own Community Scripts/silent-install.ps1.
    This script deviates on purpose in three places:
      * nefcon is pinned to 1.17.40, not 1.14.0, and driven with its modern
        flags (--create-device-node / --install-driver). The positional
        `nefconw install <inf> <hwid>` form the upstream script uses is only
        devcon-compatibility shim.
      * every download is checked against a sha256 the caller passes in.
      * vdd_settings.xml is written BEFORE the driver is installed, and it names
        the dGPU explicitly -- otherwise the indirect display would render on
        whichever adapter Windows picks, and the QXL one is present too.
#>
[CmdletBinding()]
param(
    [string]$VddUrl = 'https://github.com/VirtualDrivers/Virtual-Display-Driver/releases/download/25.7.23/VirtualDisplayDriver-x86.Driver.Only.zip',
    # "x86" in the asset name means "not ARM64"; the payload is x64.
    [string]$VddSha256 = 'e24210692b442b39af763536330ce78b423f19342b7a7792c26de3944e418b3a',

    [string]$NefConUrl = 'https://github.com/nefarius/nefcon/releases/download/v1.17.40/nefcon_v1.17.40.zip',
    [string]$NefConSha256 = '812bae7ed7dfb7d6d2284bc7de2f8ccebc92ed2a0b1ae893c53b337096e50c1a',

    # VC++ runtime: VDD fails with "vcruntime140.dll not found" without it.
    # aka.ms is an evergreen redirect, so no hash is pinned here.
    [string]$VcRedistUrl = 'https://aka.ms/vs/17/release/vc_redist.x64.exe',

    # The adapter the indirect display renders on. Must match Windows' friendly
    # name exactly; "default" lets Windows choose, which is what we do not want.
    [string]$GpuName = 'NVIDIA GeForce RTX 3060 Laptop GPU',

    # A FALLBACK, NOT THE ANSWER: setup measures the host's connected displays
    # and the installed kvmfr window and passes -Width/-Height (build.py's
    # vdd_mode). These stand only when there is nothing to measure -- a host
    # with no display connected -- and are this machine's internal panel.
    [int]$Width = 2560,
    [int]$Height = 1440,
    [int]$RefreshRate = 60,

    [string]$SettingsDir = 'C:\VirtualDisplayDriver',
    [string]$LogPath = 'C:\Windows\Temp\vdd-install.log'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is glacial without this

Start-Transcript -Path $LogPath -Force | Out-Null

function Say { param([string]$m) Write-Host ("== {0}" -f $m) }

function Get-Checked {
    param([string]$Url, [string]$OutFile, [string]$Sha256)
    Say "download $Url"
    & curl.exe -sSL --fail --retry 3 -o $OutFile $Url
    if ($LASTEXITCODE -ne 0) { throw "download failed ($LASTEXITCODE): $Url" }
    if ($Sha256) {
        $got = (Get-FileHash -Algorithm SHA256 -Path $OutFile).Hash.ToLower()
        if ($got -ne $Sha256.ToLower()) { throw "sha256 mismatch for $Url`n  want $Sha256`n  got  $got" }
        Say "sha256 ok"
    }
}

try {
    $tmp = Join-Path $env:TEMP ('vdd-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null

    # ---------------------------------------------------------------- VC++ runtime
    if (Test-Path "$env:SystemRoot\System32\vcruntime140.dll") {
        Say 'vcruntime140.dll already present, skipping VC++ redist'
    }
    else {
        $vc = Join-Path $tmp 'vc_redist.x64.exe'
        Get-Checked -Url $VcRedistUrl -OutFile $vc
        Say 'installing VC++ redistributable'
        $p = Start-Process -FilePath $vc -ArgumentList '/install', '/quiet', '/norestart' -Wait -PassThru
        # 3010 = success, reboot required. Not an error for us: the driver install
        # below does not need the reboot, only the DLLs on disk.
        if ($p.ExitCode -notin 0, 1638, 3010) { throw "vc_redist exit code $($p.ExitCode)" }
        Say "vc_redist exit code $($p.ExitCode)"
    }

    # ------------------------------------------------------------------- settings
    # Written before the driver comes up: the driver reads this file when it
    # starts, and a driver that starts without it falls back to its own defaults
    # (1920x1080 on an adapter of Windows' choosing).
    Say "writing $SettingsDir\vdd_settings.xml (gpu='$GpuName', ${Width}x${Height}@${RefreshRate})"
    New-Item -ItemType Directory -Path $SettingsDir -Force | Out-Null
    $settings = @"
<?xml version='1.0' encoding='utf-8'?>
<!-- Written by vfioctl guest/windows/vdd.ps1. Edit there, not here. -->
<vdd_settings>
    <monitors>
        <count>1</count>
    </monitors>
    <gpu>
        <friendlyname>$GpuName</friendlyname>
    </gpu>
    <global>
        <g_refresh_rate>60</g_refresh_rate>
        <g_refresh_rate>120</g_refresh_rate>
    </global>
    <resolutions>
        <resolution>
            <width>$Width</width>
            <height>$Height</height>
            <refresh_rate>$RefreshRate</refresh_rate>
        </resolution>
        <resolution>
            <width>1920</width>
            <height>1080</height>
            <refresh_rate>60</refresh_rate>
        </resolution>
    </resolutions>
    <logging>
        <SendLogsThroughPipe>true</SendLogsThroughPipe>
        <logging>false</logging>
        <debuglogging>false</debuglogging>
    </logging>
    <colour>
        <SDR10bit>false</SDR10bit>
        <HDRPlus>false</HDRPlus>
        <ColourFormat>RGB</ColourFormat>
    </colour>
    <cursor>
        <HardwareCursor>true</HardwareCursor>
        <CursorMaxX>128</CursorMaxX>
        <CursorMaxY>128</CursorMaxY>
        <AlphaCursorSupport>true</AlphaCursorSupport>
    </cursor>
    <edid>
        <CustomEdid>false</CustomEdid>
        <PreventSpoof>false</PreventSpoof>
        <EdidCeaOverride>false</EdidCeaOverride>
    </edid>
</vdd_settings>
"@
    Set-Content -Path (Join-Path $SettingsDir 'vdd_settings.xml') -Value $settings -Encoding UTF8

    # --------------------------------------------------------------------- driver
    $nefZip = Join-Path $tmp 'nefcon.zip'
    $vddZip = Join-Path $tmp 'vdd.zip'
    Get-Checked -Url $NefConUrl -OutFile $nefZip -Sha256 $NefConSha256
    Get-Checked -Url $VddUrl    -OutFile $vddZip -Sha256 $VddSha256
    Expand-Archive -Path $nefZip -DestinationPath $tmp -Force
    Expand-Archive -Path $vddZip -DestinationPath $tmp -Force

    $nefconw = Join-Path $tmp 'x64\nefconw.exe'
    if (-not (Test-Path $nefconw)) { throw "nefconw.exe not where expected: $nefconw" }
    $inf = Get-ChildItem -Path $tmp -Filter 'MttVDD.inf' -Recurse | Select-Object -First 1
    if (-not $inf) { throw 'MttVDD.inf not found in the VDD archive' }
    Say "inf: $($inf.FullName)"

    # The driver is signed by SignPath, whose root is not in the machine store.
    # Trusting the publisher up front is what makes the install silent instead of
    # popping a "would you like to install this device software?" dialog that
    # nobody is there to click.
    Say 'importing the driver''s own certificates into TrustedPublisher'
    $cat = Join-Path $inf.DirectoryName 'mttvdd.cat'
    # Import-Certificate is NOT usable here: as SYSTEM it fails with
    # E_ACCESSDENIED (0x80070005) on Cert:\LocalMachine\*, while the plain
    # X509Store API opens the very same store ReadWrite without complaint
    # (measured, 2026-08-04). Upstream's silent-install.ps1 uses the cmdlet --
    # it presumably only ever runs from an interactive admin shell.
    $certs = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2Collection
    $certs.Import([System.IO.File]::ReadAllBytes($cat))
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('TrustedPublisher', 'LocalMachine')
    $store.Open('ReadWrite')
    try {
        foreach ($c in $certs) {
            $store.Add($c)
            Say "  trusted: $($c.Subject) [$($c.Thumbprint)]"
        }
    }
    finally { $store.Close() }

    # Root-enumerated device: there is no hardware to trigger PnP, so the node
    # has to be created by hand before the INF has anything to bind to.
    # --no-duplicates makes a re-run of this script a no-op instead of a second
    # virtual monitor.
    Say 'creating device node Root\MttVDD'
    & $nefconw --create-device-node --hardware-id 'Root\MttVDD' `
        --class-name Display --class-guid '4d36e968-e325-11ce-bfc1-08002be10318' --no-duplicates
    if ($LASTEXITCODE -ne 0) { throw "nefconw --create-device-node exit code $LASTEXITCODE" }

    Say 'installing the driver'
    & $nefconw --install-driver --inf-path $inf.FullName
    if ($LASTEXITCODE -ne 0) { throw "nefconw --install-driver exit code $LASTEXITCODE" }

    Start-Sleep -Seconds 10

    # ----------------------------------------------------------------- verdict
    Say 'result'
    Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'ROOT\DISPLAY*' -or $_.FriendlyName -like '*Virtual Display*' } |
        Select-Object Status, Problem, FriendlyName, InstanceId | Format-List
    Say 'monitors'
    Get-PnpDevice -Class Monitor -PresentOnly | Select-Object Status, FriendlyName, InstanceId | Format-List
    Say 'done'
}
catch {
    # THE EXIT CODE HAS TO MOVE TOO. build.py drives this through guest-exec,
    # and a zero from a script that just printed FAILED is a round that carries
    # on to the next step standing on a broken one. Stop-Transcript has to run
    # first, so the exit lives in `finally` and this only records the verdict.
    Write-Host "FAILED: $_"
    Write-Host $_.ScriptStackTrace
    $script:Failed = $true
}
finally {
    if ($tmp -and (Test-Path $tmp)) { Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
    if ($script:Failed) { exit 1 }
}
