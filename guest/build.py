#!/usr/bin/env python3
"""Build a Windows guest unattended, from a blank disk to a reachable qemu-ga.

    ./guest/build.py build           # the whole round, ~7-10 min, unattended
    ./guest/build.py setup           # push and drive the three guest scripts
    ./guest/build.py status          # where is it now
    ./guest/build.py screenshot      # what is on its screen
    ./guest/build.py autologon       # re-run just the console-session step
    ./guest/build.py clean           # undefine + delete disk, nvram, helper ISO

WHAT THIS REPLACES. Five stages used to be done by hand: defining the domain,
answering Setup, waiting for the agent, opening a console session, and running
the three post-install scripts. All five are here now. `build` stops at a guest
that can be driven; `setup` is the half that drives it, separate because a
guest is worth building for reasons that have nothing to do with the display.

WHAT `setup` MEASURES RATHER THAN CLAIMS. Each of the three scripts installs
something the next one needs, so between them the guest's own inventory is read
back: the monitor VDD created, the Looking Glass service's state, the display
topology's verdict. An installer's exit code says the installer finished, which
is a different sentence.

THE FINISH LINE IS A GUEST THAT CAN BE DRIVEN, AND THAT TAKES TWO THINGS.
An agent, because everything the host does afterwards goes over guest-exec --
a guest without qemu-ga cannot be automated at all, however well it booted.
And a logged-in console session, because guest-exec lands in session 0 where
there are no display devices, so every remaining step (VDD, Looking Glass, the
display topology) is unreachable without one. `build` proves both: it polls
guest-ping, then sets autologon, reboots, and only calls the round finished
when guest-get-users names the account. A successful `reg add` is a claim; a
user in that list is the evidence.

IT ONLY TOUCHES GUESTS IT BUILT. The destructive half of this script -- wipe
the disk, undefine, delete the nvram -- runs against a domain only when that
domain carries the mark `build` wrote into it, and against an image only when
no other domain has it attached. An unrecognised guest is protected rather than
named, so the rule holds on a machine whose working guest is not called win11.

NOTHING PERSONAL REACHES THE REPOSITORY. The account name, the password, the
locale and the ISO path are asked at run time or passed as flags; the rendered
autounattend.xml and the helper ISO are written to a 0700 directory under
~/.images and stay machine-local. What is versioned is the template.

WHERE THE SCREENSHOTS COME IN. An unattended install that stops is indis-
tinguishable from one that is merely slow, so `build` saves a screenshot every
couple of minutes while it waits. When a round fails, the question "which
screen did it stop on" is already answered instead of needing a rerun.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
IMAGES = Path.home() / ".images"
URI = "qemu:///system"

# OWNERSHIP, AND WHY IT IS NOT A LIST OF NAMES. The destructive half of this
# script used to be gated by a blacklist -- PROTECTED_DOMAINS = {"win11"} and the
# matching disk path. A blacklist protects the machine it was written on and
# nothing else: on a second laptop the working guest is called something else,
# `clean` does not recognise it, and destroy -> undefine --nvram -> delete the
# disk runs to the end. A gate that never fires looks exactly like a gate that
# was passed. K13's profile gate does not catch it either -- that one asks about
# hardware, and the same model of laptop matches.
#
# So the question is not "which names are forbidden" but "did we build this
# domain", and the answer is a mark this script writes into the domain's
# <metadata> at define time. An unrecognised domain is protected: the whitelist
# falls on the closed side.
#
# MEASURED 2026-08-05 against libvirt 12.6.0:
#   * a custom namespace under <metadata> survives `define` verbatim, the same
#     way libosinfo's block does;
#   * `virsh metadata --uri` reads it back with the prefix stripped;
#   * ABSENT METADATA IS NOT AN ERROR -- rc is 0 with empty stdout, so a check
#     on the exit code alone would call every unmarked domain ours. That is why
#     marker_of() looks at the text and not at the return code;
#   * `define` REPLACES the stored XML rather than merging into it, so a domain
#     redefined from somebody else's file loses the mark and falls closed.
MARKER_NS = "https://github.com/drpars/vfioctl"

DEFAULT_VIRTIO_ISO = Path("/var/lib/libvirt/images/virtio-win.iso")

# Measured on the tr-TR 25H2 media, 2026-08-05: six images, no N editions, Pro
# at index 4 with an English NAME. Selecting by index would have picked the
# wrong edition -- see the template's header.
DEFAULT_IMAGE_NAME = "Windows 11 Pro"


# --------------------------------------------------------------------------- #
# shelling out
# --------------------------------------------------------------------------- #

def run(cmd, *, check=True, capture=True, **kw):
    return subprocess.run(
        cmd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True, **kw,
    )


def virsh(*args, check=True, capture=True):
    return run(["virsh", "-c", URI, *args], check=check, capture=capture)


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg, code=1):
    print(f"HATA: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #

def marker_of(name: str) -> str | None:
    """Our mark on a defined domain, or None when it does not carry one.

    The text is what decides, not the exit code: virsh answers rc=0 with an
    empty body for a domain that has no metadata in this namespace, so
    `returncode == 0` would report every guest on the machine as ours.
    """
    r = virsh("metadata", name, "--uri", MARKER_NS, check=False)
    if r.returncode != 0:
        return None
    text = r.stdout.strip()
    return text or None


def domain_disks(name: str) -> set[Path]:
    """Every image file the domain has attached, resolved."""
    r = virsh("dumpxml", name, check=False)
    if r.returncode != 0:
        return set()
    try:
        root = ElementTree.fromstring(r.stdout)
    except ElementTree.ParseError:
        return set()
    paths = set()
    for source in root.findall("./devices/disk/source"):
        raw = source.get("file") or source.get("dev")
        if raw:
            paths.add(Path(raw).resolve())
    return paths


def defined_domains() -> list[str]:
    r = virsh("list", "--all", "--name", check=False)
    return [n for n in r.stdout.split() if n] if r.returncode == 0 else []


def disk_claimants(disk: Path, exclude: str) -> list[str]:
    """Domains other than `exclude` that have this image attached."""
    target = disk.resolve()
    return [d for d in defined_domains()
            if d != exclude and target in domain_disks(d)]


def guard(name: str, disk: Path):
    """Refuse a destructive run unless we can show the target is ours.

    Two closed sides, and each catches what the other cannot. The mark answers
    for the domain: a defined guest without it is somebody else's, whatever it
    is called. The claim check answers for the image: a --disk pointing at a
    file some other domain has attached is refused whatever --name says, which
    is the case a name-based rule could never see.

    What is deliberately allowed: an unclaimed image at a path nobody else
    references, with no domain defined for it. That is a half-finished round --
    build creates the disk before it defines the domain -- and refusing it would
    leave `clean` unable to finish the job it exists for.
    """
    if domain_exists(name) and marker_of(name) is None:
        die(f"'{name}' bu betiğin ürettiği bir domain değil (işaret yok) -- "
            f"yıkıcı hiçbir adım ona uygulanmaz.")
    claimants = disk_claimants(disk, exclude=name)
    if claimants:
        die(f"'{disk}' başka bir domain'in diski ({', '.join(claimants)}) -- "
            f"bu betik ona dokunmaz.")


def guard_single_card(name: str):
    """Refuse to start a passthrough guest while another one holds the card.

    There is one dGPU, so two domains claiming it cannot run at once. Leaving
    that to libvirt means the operator reads "VM failed to start" and has to
    work out for themselves whether the cause was the card, the hook or the
    domain -- and this is the round where a second passthrough domain exists on
    this machine for the first time.
    """
    ours = domain_hostdevs(name)
    if not ours:
        return
    for other in defined_domains():
        if other == name or domain_state(other) != "running":
            continue
        clash = ours & domain_hostdevs(other)
        if clash:
            die(f"'{other}' çalışıyor ve aynı PCI cihazını istiyor "
                f"({', '.join(sorted(clash))}) -- tek kart var, önce onu kapat.")


def domain_hostdevs(name: str) -> set[str]:
    """PCI addresses the domain passes through, as 0000:01:00.0 strings."""
    r = virsh("dumpxml", name, check=False)
    if r.returncode != 0:
        return set()
    try:
        root = ElementTree.fromstring(r.stdout)
    except ElementTree.ParseError:
        return set()
    addrs = set()
    for hostdev in root.findall("./devices/hostdev[@type='pci']"):
        addr = hostdev.find("./source/address")
        if addr is None:
            continue
        try:
            addrs.add("{:04x}:{:02x}:{:02x}.{}".format(
                int(addr.get("domain", "0x0"), 16),
                int(addr.get("bus", "0x0"), 16),
                int(addr.get("slot", "0x0"), 16),
                int(addr.get("function", "0x0"), 16),
            ))
        except ValueError:
            continue
    return addrs


def domain_exists(name: str) -> bool:
    return virsh("domstate", name, check=False).returncode == 0


def domain_state(name: str) -> str:
    r = virsh("domstate", name, check=False)
    return r.stdout.strip() if r.returncode == 0 else "(tanımsız)"


# --------------------------------------------------------------------------- #
# autounattend rendering
# --------------------------------------------------------------------------- #

def xml_escape(s: str) -> str:
    """Passwords go into the answer file verbatim, so they must survive XML."""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def render(template: Path, values: dict) -> str:
    """Fill a template, and refuse to hand back anything that is not valid XML.

    Both checks exist because both mistakes are silent downstream. A leftover
    placeholder reaches Windows as a literal string. And a template comment
    containing a double hyphen -- illegal inside an XML comment, which is easy
    to write in English prose -- makes Setup ignore the answer file entirely,
    which looks exactly like an answer file that was never found.
    """
    text = template.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", str(value))
    leftover = sorted(set(re.findall(r"@@([A-Z_0-9]+)@@", text)))
    if leftover:
        die(f"{template.name}: doldurulmamış yer tutucu: {', '.join(leftover)}")
    try:
        ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        die(f"{template.name}: üretilen XML geçersiz: {exc}")
    return text


def build_unattend_iso(workdir: Path, unattend_xml: str, out_iso: Path):
    """Small ISO with autounattend.xml at the root.

    The 8.5 GB Windows ISO is never repacked: Setup scans the root of every
    attached drive, so a second CD-ROM answers it. That is what makes a failed
    round cheap -- rebuilding this takes under a second.
    """
    staging = workdir / "iso"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "vfioctl").mkdir(parents=True)

    (staging / "autounattend.xml").write_text(unattend_xml, encoding="utf-8")

    # CRLF on the way in. The repo copy keeps LF for readable diffs, but cmd.exe
    # mis-parses LF-only batch files in ways that look like the script simply
    # did nothing -- the one failure mode this whole file is built to avoid.
    src = (TEMPLATES / "setupcomplete.cmd").read_text(encoding="utf-8")
    (staging / "vfioctl" / "setupcomplete.cmd").write_bytes(
        src.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    )

    out_iso.unlink(missing_ok=True)
    run([
        "xorriso", "-as", "mkisofs",
        "-J", "-joliet-long", "-rational-rock",
        "-V", "UNATTEND",
        "-o", str(out_iso), str(staging),
    ])
    say(f"yardımcı ISO: {out_iso} ({out_iso.stat().st_size // 1024} KB)")


# --------------------------------------------------------------------------- #
# the round
# --------------------------------------------------------------------------- #

def press_enter_past_the_cd_prompt(name: str, seconds: int):
    """Answer the firmware's "Press any key to boot from CD" window.

    Miss it and the firmware falls through to `No bootable option or device was
    found`, which from outside is indistinguishable from a VM that is running
    fine. It is bounded on purpose: Setup reboots several times later on, and an
    ENTER arriving at one of those would start the installation over from the CD.

    The clean alternative is repacking the ISO's boot image with the
    efisys_noprompt.bin that ships inside it -- kept in reserve, because it
    means touching the 8.5 GB ISO for a problem twenty seconds of ENTER solves.
    """
    say(f"CD istemi için {seconds} sn ENTER gönderiliyor")
    deadline = time.time() + seconds
    while time.time() < deadline:
        virsh("send-key", name, "KEY_ENTER", check=False)
        time.sleep(1)


def agent_ping(name: str) -> bool:
    r = virsh("qemu-agent-command", name,
              '{"execute":"guest-ping"}', check=False)
    return r.returncode == 0 and '"return"' in r.stdout


def agent_cmd(name: str, payload: dict) -> dict | None:
    r = virsh("qemu-agent-command", name, json.dumps(payload), check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)["return"]
    except (ValueError, KeyError):
        return None


def guest_exec(name: str, path: str, args: list[str], timeout: int = 120):
    """Run a program in the guest and wait for it.

    This is the channel everything after installation uses, so it is worth
    saying what it is: commands land as NT AUTHORITY\\SYSTEM in session 0.
    That means no bootstrap problem and no password -- and also no display
    devices, which is why anything touching the screen needs a console session
    and cannot simply be run from here -- it needs a logged-in console user.

    Returns (exitcode, stdout, stderr). Exit code None means the guest never
    reported back inside the timeout.
    """
    started = agent_cmd(name, {"execute": "guest-exec", "arguments": {
        "path": path, "arg": args, "capture-output": True}})
    if not started or "pid" not in started:
        return None, "", "guest-exec kabul edilmedi"
    pid = started["pid"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        st = agent_cmd(name, {"execute": "guest-exec-status",
                              "arguments": {"pid": pid}})
        if st and st.get("exited"):
            def dec(key):
                return base64.b64decode(st.get(key, "")).decode("utf-8", "replace")
            return st.get("exitcode"), dec("out-data"), dec("err-data")
        time.sleep(1)
    return None, "", f"guest-exec {timeout} sn içinde bitmedi"


WINLOGON = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"


def set_autologon(name: str, user: str, password: str) -> bool:
    """Turn on autologon from the host, after OOBE is out of the way.

    It has to be here rather than in the answer file: 25H2's OOBE explicitly
    clears whatever autologon the unattend asked for ("fEnableAutologon = 0",
    measured 2026-08-05), and SetupComplete.cmd runs before that happens. The
    host is the first actor that gets to speak afterwards.

    AutoLogonCount is deleted rather than set, because a count is a promise that
    expires: the console session would keep coming up for a few boots and then
    quietly stop, long after anyone connects that to this step.
    """
    steps = [
        ["/v", "AutoAdminLogon", "/t", "REG_SZ", "/d", "1", "/f"],
        ["/v", "DefaultUserName", "/t", "REG_SZ", "/d", user, "/f"],
        ["/v", "DefaultPassword", "/t", "REG_SZ", "/d", password, "/f"],
    ]
    for extra in steps:
        code, _, err = guest_exec(name, "reg.exe", ["add", WINLOGON, *extra])
        if code != 0:
            say(f"autologon: reg add başarısız ({extra[1]}): {err.strip()}")
            return False
    # Absent is the desired state, so "not found" is success here.
    guest_exec(name, "reg.exe", ["delete", WINLOGON, "/v", "AutoLogonCount", "/f"])
    return True


def autologon_armed(name: str) -> bool:
    code, out, _ = guest_exec(name, "reg.exe",
                              ["query", WINLOGON, "/v", "AutoAdminLogon"])
    return code == 0 and out.strip().split()[-1:] == ["1"]


def rearm_autologon(name: str, user: str, password: str, attempts: int = 3) -> bool:
    """Write autologon again after the first logon, and check it survived.

    MEASURED 2026-08-05, and the reason this function exists at all. Setting
    autologon and rebooting does produce a console session -- exactly once. The
    first-ever logon is also when OOBE finishes its own work, and that cleanup
    deletes AutoAdminLogon and DefaultUserName on its way out (the same
    "Clearing Auto-logon values per request" seen in UnattendGC's log). What is
    left behind is the worst of both: a plaintext DefaultPassword in the
    registry and no working autologon, and the guest drops back to the lock
    screen with nothing to say why.

    Writing the same values a second time, after that cleanup has run, sticks --
    verified across a reboot. So the state is asserted and then read back rather
    than assumed, because "reg add succeeded" was true the first time too.
    """
    for _ in range(attempts):
        if not set_autologon(name, user, password):
            return False
        time.sleep(15)
        if autologon_armed(name):
            return True
    return False


def console_users(name: str) -> list[str]:
    users = agent_cmd(name, {"execute": "guest-get-users"})
    return [u.get("user", "?") for u in users] if users else []


def guest_boot_time(name: str) -> str | None:
    """The guest's own idea of when it last booted, as an ISO string.

    CIM rather than `wmic`, which Microsoft has been removing.
    """
    code, out, _ = guest_exec(name, "powershell.exe", [
        "-NoProfile", "-NonInteractive", "-Command",
        "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')",
    ], timeout=90)
    return out.strip() if code == 0 and out.strip() else None


def reboot_and_wait(name: str, workdir: Path, timeout_min: int) -> bool:
    """Reboot, and do not believe it happened until the guest says so.

    Polling for the agent after a fixed sleep is not a reboot test: the agent
    that answers may be the one that was already running, and a shutdown that
    never started looks identical to one that finished quickly. So the boot
    time is read before and after, and only a CHANGED value counts. The first
    round of this script passed on the sleep-then-poll version and happened to
    be right, which is exactly the kind of luck that hides a broken check.
    """
    before = guest_boot_time(name)
    if before is None:
        say("yeniden başlatma: misafirin açılış zamanı okunamadı")
        return False
    say(f"yeniden başlatma öncesi açılış zamanı: {before}")

    virsh("reboot", name, capture=False)

    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        time.sleep(10)
        if not agent_ping(name):
            continue
        after = guest_boot_time(name)
        if after and after != before:
            say(f"yeniden başladı; yeni açılış zamanı: {after}")
            return True

    say(f"yeniden başlatma {timeout_min} dk içinde doğrulanamadı")
    screenshot(name, workdir / "shots" / "reboot-dogrulanamadi.png")
    return False


def screenshot(name: str, path: Path) -> bool:
    """Measured 2026-08-05: libvirt hands back PNG for this QXL guest, not the
    PPM the man page's example implies. The extension follows what arrives, so
    the files open by double-click instead of needing `file` to identify them."""
    r = virsh("screenshot", name, str(path), check=False)
    return r.returncode == 0


def wait_for_agent(name: str, workdir: Path, timeout_min: int) -> bool:
    shots = workdir / "shots"
    shots.mkdir(exist_ok=True)
    say(f"ajan bekleniyor (en fazla {timeout_min} dk) -- her 2 dk'da ekran görüntüsü")

    start = time.time()
    deadline = start + timeout_min * 60
    next_shot = 0.0

    while time.time() < deadline:
        if agent_ping(name):
            say(f"ajan yanıt verdi -- {int(time.time() - start) // 60} dk {int(time.time() - start) % 60} sn")
            return True

        state = domain_state(name)
        if state == "shut off":
            # Setup reboots via reset, not poweroff. A powered-off guest this
            # early means it stopped, and waiting out the timeout learns nothing.
            say("misafir kapandı -- kurulum bitmeden durdu")
            screenshot(name, shots / "kapandi.png")
            return False

        now = time.time()
        if now >= next_shot:
            stamp = time.strftime("%H%M%S")
            if screenshot(name, shots / f"{stamp}.png"):
                say(f"  {int(now - start) // 60:>3} dk | {state} | ekran: {stamp}.png")
            else:
                say(f"  {int(now - start) // 60:>3} dk | {state}")
            next_shot = now + 120

        time.sleep(5)

    say(f"zaman aşımı ({timeout_min} dk) -- ajan gelmedi")
    screenshot(name, shots / "zamanasimi.png")
    return False


# --------------------------------------------------------------------------- #
# the guest-side setup: three scripts, pushed and driven
# --------------------------------------------------------------------------- #

WINDOWS = HERE / "windows"

# WHY THE SCRIPTS LIVE ON DISK IN THE GUEST rather than being folded into the
# command. display-layout.ps1 re-launches itself into the console session
# through a scheduled task -- it needs a path to point that task at -- and its
# -Reattach escape hatch is only reachable by somebody sitting at the guest
# with no working display. -EncodedCommand breaks both.
#
# C:\Users\Public, because the console user has to be able to read it.
# C:\Windows\Temp swallowed this once: the task ran, read nothing, and reported
# success, which looks exactly like a task that did its job.
GUEST_DIR = r"C:\Users\Public\vfioctl"

# qemu-ga takes base64 in a JSON command line; 48 KB of payload is about 64 KB
# of argument, which keeps every write comfortably inside the agent's limits.
PUSH_CHUNK = 48 * 1024

# Red Hat QXL, the emulated adapter every one of these guests has. Anything the
# indirect display should render on is by definition NOT this.
QXL_HWID = "PCI\\VEN_1B36"


def guest_push(name: str, local: Path, remote: str) -> bool:
    """Copy a file into the guest over the agent, in chunks."""
    data = local.read_bytes()
    handle = agent_cmd(name, {"execute": "guest-file-open",
                              "arguments": {"path": remote, "mode": "wb"}})
    if not isinstance(handle, int):
        say(f"  {local.name}: guest-file-open reddedildi")
        return False
    ok = True
    try:
        for start in range(0, len(data), PUSH_CHUNK):
            buf = base64.b64encode(data[start:start + PUSH_CHUNK]).decode()
            if agent_cmd(name, {"execute": "guest-file-write", "arguments": {
                    "handle": handle, "buf-b64": buf}}) is None:
                say(f"  {local.name}: guest-file-write düştü ({start} baytta)")
                ok = False
                break
    finally:
        agent_cmd(name, {"execute": "guest-file-close",
                         "arguments": {"handle": handle}})
    return ok


def powershell(name: str, args: list[str], timeout: int = 120):
    return guest_exec(name, "powershell.exe", [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args,
    ], timeout=timeout)


def ps_json(name: str, expression: str, timeout: int = 120):
    """Evaluate a PowerShell expression in the guest and parse its JSON.

    ConvertTo-Json hands back a bare object for a single result and an array for
    several, so the shape is normalised here -- code that has to remember which
    it is gets that wrong on the day a machine has one display adapter.
    """
    code, out, _ = powershell(name, ["-Command", expression], timeout)
    if code != 0 or not out.strip():
        return []
    try:
        parsed = json.loads(out)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def display_adapters(name: str) -> list[dict]:
    return ps_json(name, "Get-CimInstance Win32_VideoController | "
                         "Select-Object Name,PNPDeviceID | ConvertTo-Json -Compress")


def monitors(name: str) -> list[dict]:
    return ps_json(name, "Get-PnpDevice -Class Monitor -PresentOnly | "
                         "Select-Object Status,FriendlyName,InstanceId | "
                         "ConvertTo-Json -Compress")


def lg_service(name: str) -> list[dict]:
    return ps_json(name, "Get-Service | Where-Object { $_.DisplayName -like "
                         "'*Looking Glass*' } | Select-Object Name,Status | "
                         "ConvertTo-Json -Compress")


def choose_gpu(name: str) -> str | None:
    """Which adapter the indirect display should render on -- asked, not assumed.

    THE DEFAULT USED TO BE THIS MACHINE'S CARD, spelled out in vdd.ps1's
    parameters, which is the machine-specific value K9 forbids and one that
    cannot be derived from sysfs: it is Windows' friendly name for the card, and
    only Windows knows it. So the guest is asked.

    The failure this prevents is measured and quiet: vdd.ps1 that cannot match
    the name it was given falls back to `default`, `default` lets Windows pick,
    Windows picks QXL, and the symptom is "Looking Glass connects and no frames
    ever arrive" -- three layers away from the cause.

    It discovers rather than verifies (core/probe.py's rule, guest side): it
    does not look for the card it expects, it looks at what is there. With no
    passthrough adapter present -- the cardless rehearsal -- it says so and uses
    the one adapter that exists. With more than one candidate it refuses to
    guess, because guessing wrong is the silent failure above.
    """
    adapters = display_adapters(name)
    if not adapters:
        say("  ekran bağdaştırıcıları okunamadı")
        return None
    for a in adapters:
        say(f"  bağdaştırıcı: {a.get('Name')}  [{a.get('PNPDeviceID')}]")

    candidates = [a for a in adapters
                  if QXL_HWID not in (a.get("PNPDeviceID") or "").upper()
                  and not (a.get("PNPDeviceID") or "").upper().startswith("ROOT\\")]
    if len(candidates) == 1:
        return candidates[0].get("Name")
    if not candidates:
        if len(adapters) == 1:
            say("  devredilmiş kart yok -- kartsız prova, tek bağdaştırıcı seçiliyor")
            return adapters[0].get("Name")
        say("  devredilmiş kart yok ve birden çok bağdaştırıcı var")
        return None
    say(f"  {len(candidates)} aday var -- tahmin edilmez, --gpu-name ile ver")
    return None


def vdd_monitor(name: str) -> dict | None:
    """The monitor the Virtual Display Driver creates, if it has appeared.

    This is the evidence half of the step. `msiexec exit=0` and a driver that
    installed without error are both claims; a monitor device in the guest's own
    inventory is what the next script actually depends on. Matched loosely on
    purpose -- the friendly name is upstream's to change -- and the whole
    inventory is printed when nothing matches, so a renamed device shows up as a
    name to read rather than as "no display".
    """
    found = monitors(name)
    for m in found:
        friendly = (m.get("FriendlyName") or "")
        if "VDD" in friendly.upper() or "VIRTUAL DISPLAY" in friendly.upper():
            return m
    for m in found:
        say(f"  monitör: {m.get('Status')} {m.get('FriendlyName')}")
    return None


def run_guest_script(name: str, script: Path, args: list[str],
                     timeout: int) -> bool:
    """Push one script and run it, reporting what it said either way."""
    remote = f"{GUEST_DIR}\\{script.name}"
    say(f"{script.name} -> {remote}")
    if not guest_push(name, script, remote):
        return False

    code, out, err = powershell(name, ["-File", remote, *args], timeout)
    for line in (out or "").splitlines():
        print(f"    | {line}")
    if err.strip():
        for line in err.strip().splitlines():
            print(f"    ! {line}")
    if code is None:
        say(f"{script.name}: {timeout} sn içinde bitmedi")
        return False
    if code != 0:
        say(f"{script.name}: çıkış kodu {code}")
        return False
    return True


def guest_setup(a, name: str) -> int:
    """Drive the three guest scripts in order, stopping at the first failure.

    THE ORDER IS A DEPENDENCY, NOT A PREFERENCE. display-layout has nothing to
    isolate until VDD's screen exists, and Looking Glass captures whatever the
    desktop is at the time. So the round does not walk the list -- it checks
    that each step produced the thing the next one needs, and the check is the
    guest's own inventory rather than the installer's exit code.

    The one retry is deliberate and bounded: an indirect display driver that has
    installed cleanly sometimes needs the boot to enumerate, so a missing
    monitor is worth exactly one reboot before it counts as a failure.
    """
    if not agent_ping(name):
        die(f"'{name}' ajanı yanıt vermiyor -- önce build")

    users = console_users(name)
    if not users:
        die("misafirde açık konsol oturumu yok -- display-layout ekrana "
            f"ulaşamaz. Önce: {sys.argv[0]} --name {name} autologon")
    say(f"konsol oturumu: {', '.join(users)}")

    # mkdir exits 1 on a directory that already exists, which is the state we
    # want; only silence from the agent is a failure worth stopping for.
    code, _, _ = guest_exec(name, "cmd.exe", ["/c", "mkdir", GUEST_DIR])
    if code is None:
        die(f"{GUEST_DIR} oluşturulamadı -- ajan cevap vermedi")

    gpu = a.gpu_name or choose_gpu(name)
    if not gpu:
        die("VDD'nin render edeceği bağdaştırıcı belirlenemedi -- --gpu-name ver")
    say(f"VDD bağdaştırıcısı: {gpu}")

    # 1. VDD
    if not run_guest_script(name, WINDOWS / "vdd.ps1",
                            ["-GpuName", gpu], timeout=900):
        return 1
    monitor = vdd_monitor(name)
    if monitor is None:
        say("VDD ekranı görünmüyor -- bir kez yeniden başlatılıp tekrar bakılacak")
        workdir = IMAGES / f"{name}-unattend"
        workdir.mkdir(parents=True, exist_ok=True)
        if not reboot_and_wait(name, workdir, 10):
            say("ADIM DÜŞTÜ -- yeniden başlatma doğrulanamadı.")
            return 1
        monitor = vdd_monitor(name)
    if monitor is None:
        say("ADIM DÜŞTÜ -- VDD ekranı yeniden başlatmadan sonra da yok.")
        return 1
    say(f"VDD ekranı: {monitor.get('Status')} {monitor.get('FriendlyName')}")

    # 2. Looking Glass host
    if not run_guest_script(name, WINDOWS / "looking-glass.ps1", [], timeout=600):
        return 1
    services = lg_service(name)
    running = [s for s in services if str(s.get("Status")) in ("4", "Running")]
    if not running:
        say(f"ADIM DÜŞTÜ -- LG host servisi çalışmıyor ({services or 'servis yok'}).")
        return 1
    say(f"LG host servisi: {running[0].get('Name')} çalışıyor")

    # 3. one display
    if not run_guest_script(name, WINDOWS / "display-layout.ps1", [], timeout=600):
        say("ADIM DÜŞTÜ -- ekran topolojisi kurulamadı.")
        say(f"  geri alma: misafirde {GUEST_DIR}\\display-layout.ps1 -Reattach")
        return 1

    say("KURULUM GEÇTİ -- VDD ekranı var, LG host çalışıyor, misafir tek ekranda.")
    return 0


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_build(a):
    disk = Path(a.disk)
    guard(a.name, disk)

    for tool in ("xorriso", "qemu-img", "virsh"):
        if not shutil.which(tool):
            die(f"'{tool}' bulunamadı")

    win_iso = Path(a.win_iso)
    virtio_iso = Path(a.virtio_iso)
    for iso in (win_iso, virtio_iso):
        if not iso.is_file():
            die(f"ISO yok: {iso}")

    # Credentials are collected before anything is destroyed, and not only to
    # fail fast: --force wipes the work directory, which is a perfectly sensible
    # place for --password-file to live.
    user = a.user or input("misafir kullanıcı adı: ").strip()
    if not user:
        die("kullanıcı adı boş olamaz")
    # --password-file exists because --password would put the password in the
    # process table for anyone running ps, and an unattended round is exactly
    # when nobody is at the prompt to type it.
    if a.password_file:
        password = Path(a.password_file).read_text(encoding="utf-8").strip()
    else:
        password = a.password or getpass.getpass("misafir parolası: ")
    if not password:
        die("parola boş olamaz")

    if domain_exists(a.name):
        if not a.force:
            die(f"'{a.name}' zaten tanımlı. Önce: {sys.argv[0]} clean")
        cmd_clean(a)

    workdir = IMAGES / f"{a.name}-unattend"
    workdir.mkdir(parents=True, exist_ok=True)
    workdir.chmod(0o700)
    unattend_iso = workdir / "unattend.iso"

    # 1. autounattend.xml
    xml = render(TEMPLATES / "autounattend.xml", {
        "UILANG": a.locale,
        "INPUTLOCALE": a.locale,
        "SYSLOCALE": a.locale,
        "USERLOCALE": a.locale,
        "IMAGENAME": a.image_name,
        "HOSTNAME": a.hostname or a.name.upper()[:15],
        "TIMEZONE": a.timezone,
        "USER": user,
        "PASSWORD": xml_escape(password),
    })
    rendered = workdir / "autounattend.xml"
    rendered.write_text(xml, encoding="utf-8")
    rendered.chmod(0o600)
    say(f"autounattend.xml üretildi: {rendered} (parola taşır, makine-yerel)")

    # 2. helper ISO
    build_unattend_iso(workdir, xml, unattend_iso)

    # 3. blank disk
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.unlink(missing_ok=True)
    run(["qemu-img", "create", "-f", "qcow2", str(disk), a.size])
    say(f"disk imajı: {disk} ({a.size}, seyrek)")

    # 4. define
    dom_xml = render(TEMPLATES / "domain.xml", {
        "NAME": a.name,
        "MEMORY_KIB": a.memory * 1024,
        "VCPU": a.vcpu,
        "CORES": a.vcpu // 2,
        "DISK": disk,
        "WINISO": win_iso,
        "VIRTIOISO": virtio_iso,
        "UNATTENDISO": unattend_iso,
    })
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
        fh.write(dom_xml)
        tmp = fh.name
    try:
        virsh("define", tmp, capture=False)
    finally:
        os.unlink(tmp)
    say(f"domain tanımlandı: {a.name}")

    # 5. run it
    guard_single_card(a.name)
    virsh("start", a.name, capture=False)
    press_enter_past_the_cd_prompt(a.name, a.enter_seconds)

    if not wait_for_agent(a.name, workdir, a.timeout):
        print()
        say("TUR DÜŞTÜ -- ajan gelmedi.")
        say(f"  ekran görüntüleri: {workdir / 'shots'}")
        say(f"  üretilen XML:      {rendered}")
        return 1

    say("ajan ulaşılabilir; kurulum sonrası adımlar başlıyor")

    # 6. autologon, which only the host can do and only from here on
    if not set_autologon(a.name, user, password):
        say("TUR YARIM -- ajan geldi ama autologon kurulamadı.")
        return 1
    say("autologon kaydı yazıldı; doğrulaması yeniden başlatmayı gerektiriyor")

    # A registry value is a claim; a logged-in console session is the evidence.
    # Nothing about the display works without one, so the round does not get to
    # call itself finished on the strength of a successful `reg add`.
    if not reboot_and_wait(a.name, workdir, min(a.timeout, 10)):
        say("TUR YARIM -- yeniden başlatma doğrulanamadı.")
        return 1

    # First logon builds the user profile, which takes longer than the agent
    # takes to come back; guest-get-users is empty until it finishes.
    users = []
    deadline = time.time() + 300
    while time.time() < deadline and not users:
        users = console_users(a.name)
        if not users:
            time.sleep(5)

    print()
    if user.lower() not in [u.lower() for u in users]:
        say(f"TUR YARIM -- ajan var, konsol oturumu yok (guest-get-users: {users or 'boş'}).")
        say(f"  ekran görüntüleri: {workdir / 'shots'}")
        return 1

    # That was the first logon, which is also when OOBE wipes the settings that
    # produced it. Re-arm, or the guest silently stops logging in from the next
    # boot onward -- a failure that would surface days later, far from its cause.
    say(f"konsol oturumu açıldı ({', '.join(users)}); autologon yeniden yazılıyor")
    if not rearm_autologon(a.name, user, password):
        say("TUR YARIM -- konsol oturumu açıldı ama autologon kalıcı olmadı.")
        say(f"  elle: {sys.argv[0]} --name {a.name} autologon")
        return 1

    say("TUR GEÇTİ -- ajan ulaşılabilir, konsol oturumu açık, autologon kalıcı.")

    # THE GUEST-SIDE SETUP IS OPT-IN, and the reason is the domain this round
    # builds: it carries no ivshmem device, so looking-glass.ps1 refuses at its
    # own precondition and the round would end in a failure that means nothing.
    # A tool whose normal outcome is a red line teaches people to skip reading
    # the line. `setup` runs it whenever the guest is ready for it.
    if not a.setup:
        say(f"  sıradaki: {sys.argv[0]} --name {a.name} setup")
        return 0
    print()
    return guest_setup(a, a.name)


def cmd_status(a):
    print(f"domain : {a.name} -> {domain_state(a.name)}")
    if domain_exists(a.name):
        print(f"ajan   : {'yanıt veriyor' if agent_ping(a.name) else 'yok'}")
        r = virsh("domifaddr", a.name, "--source", "agent", check=False)
        if r.returncode == 0 and r.stdout.strip():
            print(r.stdout.strip())
    workdir = IMAGES / f"{a.name}-unattend"
    if workdir.exists():
        print(f"çalışma: {workdir}")
    return 0


def cmd_screenshot(a):
    if not domain_exists(a.name):
        die(f"'{a.name}' tanımlı değil")
    out = Path(a.out or (IMAGES / f"{a.name}-unattend" / "shots" /
                         f"{time.strftime('%H%M%S')}.png"))
    out.parent.mkdir(parents=True, exist_ok=True)
    if screenshot(a.name, out):
        print(out)
        return 0
    die("ekran görüntüsü alınamadı")


def cmd_autologon(a):
    """Set autologon on a guest that is already up.

    Separate from `build` because a round can get this far and no further, and
    re-running the whole install to retry a registry write is absurd.
    """
    if not agent_ping(a.name):
        die(f"'{a.name}' ajanı yanıt vermiyor")
    user = a.user or input("misafir kullanıcı adı: ").strip()
    password = (Path(a.password_file).read_text(encoding="utf-8").strip()
                if a.password_file else
                a.password or getpass.getpass("misafir parolası: "))
    if not (user and password):
        die("kullanıcı adı ve parola gerekli")
    if not set_autologon(a.name, user, password):
        return 1
    say("autologon yazıldı -- etkisi bir sonraki açılışta görünür")
    say(f"  doğrulama: {sys.argv[0]} --name {a.name} status")
    return 0


def cmd_clean(a):
    disk = Path(a.disk)
    guard(a.name, disk)

    if domain_exists(a.name):
        if domain_state(a.name) != "shut off":
            virsh("destroy", a.name, check=False, capture=False)
        # --nvram or the per-domain VARS file outlives the domain and the next
        # define inherits a boot order pointing at a disk that no longer exists.
        virsh("undefine", a.name, "--nvram", check=False, capture=False)
        say(f"domain kaldırıldı: {a.name}")

    if disk.exists():
        disk.unlink()
        say(f"disk silindi: {disk}")

    workdir = IMAGES / f"{a.name}-unattend"
    if workdir.exists():
        shutil.rmtree(workdir)
        say(f"çalışma dizini silindi: {workdir}")
    return 0


# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(
        description="Windows misafirini gözetimsiz kur (autounattend.xml turu)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--name", default="win11-test",
                   help="domain adı (varsayılan: win11-test)")
    p.add_argument("--disk", default=str(IMAGES / "win11-test.qcow2"))

    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="baştan sona bir tur koş")
    b.add_argument("--win-iso", default="", help="Windows kurulum ISO'su")
    b.add_argument("--virtio-iso", default=str(DEFAULT_VIRTIO_ISO))
    b.add_argument("--size", default="64G")
    b.add_argument("--memory", type=int, default=8192, help="MiB")
    b.add_argument("--vcpu", type=int, default=8)
    b.add_argument("--locale", default="tr-TR")
    b.add_argument("--timezone", default="Turkey Standard Time")
    b.add_argument("--image-name", default=DEFAULT_IMAGE_NAME)
    b.add_argument("--hostname", default="")
    b.add_argument("--user", default="")
    b.add_argument("--password", default="",
                   help="verilmezse sorulur (kabuk geçmişine düşmesin)")
    b.add_argument("--password-file", default="",
                   help="parolayı dosyadan oku -- ps çıktısına düşmez")
    b.add_argument("--enter-seconds", type=int, default=20)
    b.add_argument("--timeout", type=int, default=45, help="dakika")
    b.add_argument("--force", action="store_true",
                   help="tanımlıysa önce temizle")
    b.add_argument("--setup", action="store_true",
                   help="tur bitince misafir betiklerini de sür")
    b.add_argument("--gpu-name", default="",
                   help="VDD'nin render edeceği bağdaştırıcı (boşsa misafirde keşfedilir)")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("status", help="domain ve ajan durumu")
    s.set_defaults(func=cmd_status)

    sc = sub.add_parser("screenshot", help="ekran görüntüsü al")
    sc.add_argument("--out", default="")
    sc.set_defaults(func=cmd_screenshot)

    al = sub.add_parser("autologon", help="konsol oturumunu aç (ajan gerekir)")
    al.add_argument("--user", default="")
    al.add_argument("--password", default="")
    al.add_argument("--password-file", default="")
    al.set_defaults(func=cmd_autologon)

    st = sub.add_parser("setup", help="misafir betiklerini sür (VDD, LG, ekran)")
    st.add_argument("--gpu-name", default="",
                    help="VDD'nin render edeceği bağdaştırıcı (boşsa misafirde keşfedilir)")
    st.set_defaults(func=lambda a: guest_setup(a, a.name))

    c = sub.add_parser("clean", help="domain + disk + çalışma dizini sil")
    c.set_defaults(func=cmd_clean)

    a = p.parse_args()

    if getattr(a, "win_iso", None) == "":
        found = sorted(Path.home().glob("İndirilenler/*windows*11*.iso"))
        if not found:
            die("Windows ISO'su bulunamadı, --win-iso ile ver")
        a.win_iso = str(found[-1])

    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
