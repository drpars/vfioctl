@echo off
rem ---------------------------------------------------------------------------
rem  Staged onto the helper ISO as \qemu-vfio\setupcomplete.cmd, copied by the
rem  specialize pass to C:\Windows\Setup\Scripts\SetupComplete.cmd, and run by
rem  Setup as SYSTEM right before the first logon.
rem
rem  WHY THIS FILE EXISTS AT ALL. Everything the host does to the guest after
rem  installation goes over guest-exec, and guest-exec does not exist until
rem  qemu-ga is running. So this script is the bridge, and the bridge has to be
rem  built from inside the guest -- there is no channel to build it from outside.
rem  It deliberately does NOT try to be the whole post-install: the three PS1
rem  scripts (VDD, Looking Glass, display layout) are driven from the host once
rem  the agent answers.
rem
rem  WHY NOT msiexec IN THE specialize PASS. Windows Installer is not reliably
rem  serviced that early; SetupComplete.cmd runs in a finished OS.
rem
rem  IT LOGS BECAUSE IT CANNOT REPORT. Setup ignores this script's exit code and
rem  discards its console, so a failure here looks exactly like "the agent never
rem  came up" -- the same silence as a guest that never booted. The log is the
rem  only way to tell those apart, so it is written twice: C:\Windows\Temp for
rem  habit, and C:\Users\Public because that is where the console user can read
rem  it without elevation (same reasoning as guest/windows/display-layout.ps1).
rem ---------------------------------------------------------------------------

set LOG=C:\Windows\Temp\qemu-vfio-setupcomplete.log

echo ==== qemu-vfio setupcomplete %DATE% %TIME% ==== >> "%LOG%" 2>&1

rem --- qemu-ga: the keystone -------------------------------------------------
rem Drive letters are unstable, so probe instead of assuming.
set GA=
for %%i in (D E F G H I) do @if exist %%i:\guest-agent\qemu-ga-x86_64.msi set GA=%%i:\guest-agent\qemu-ga-x86_64.msi

if not defined GA (
	echo FAIL: qemu-ga-x86_64.msi not found on any of D: E: F: G: H: I: >> "%LOG%" 2>&1
) else (
	echo installing %GA% >> "%LOG%" 2>&1
	msiexec /i "%GA%" /qn /norestart /L*v C:\Windows\Temp\qemu-vfio-qemuga.log
	echo msiexec exit=%ERRORLEVEL% >> "%LOG%" 2>&1
	sc query QEMU-GA >> "%LOG%" 2>&1
)

rem --- autologon is NOT set here, and that is measured ------------------------
rem The first round tried, and the log proved it cannot work from this script:
rem SetupComplete.cmd ran at 00:51:20 and OOBE's CloudExperienceHostBroker
rem cleared the autologon values at 00:52:51, a minute and a half afterwards.
rem "SetupComplete.cmd runs after Setup finishes" is true and still too early.
rem The host sets autologon over guest-exec once the agent answers.

rem --- no screen/sleep timeouts ----------------------------------------------
rem A sleeping guest screen shows up on the host as "Looking Glass never gets a
rem frame", which reads as a broken capture rather than a dark monitor.
powercfg /change monitor-timeout-ac 0 >> "%LOG%" 2>&1
powercfg /change standby-timeout-ac 0 >> "%LOG%" 2>&1

rem --- leave the evidence where it can be read --------------------------------
copy /y "%LOG%" C:\Users\Public\qemu-vfio-setupcomplete.log >nul 2>&1
echo ==== done %DATE% %TIME% ==== >> "%LOG%" 2>&1
copy /y "%LOG%" C:\Users\Public\qemu-vfio-setupcomplete.log >nul 2>&1

exit /b 0
