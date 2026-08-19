"""Every file this tool owns on the host, and why each one has to exist.

These five pieces used to be tasks in an Arch install tool, and moving them
here is the whole point of this phase: their only reason to exist is
passthrough, so a second machine wanting passthrough had to run a full distro
installer to get four files. What follows is the reasoning that was learned
the expensive way, kept with the code it explains.

WHAT IS DECLARATIVE HERE AND WHY. Each file is a `Managed` value -- path,
content, mode, what to reload afterwards. `install`, `install --check` and
`uninstall` all walk the same list, so the set of files the tool claims cannot
drift between installing them, checking them and removing them. A file that
uninstall forgets is a file that makes the next install look like it worked.

---------------------------------------------------------------------------
1. THE INTEGRATED GPU'S STABLE NAME (70-vfio-igpu.rules)

Handing the discrete GPU to a VM means telling the compositor, before the
handover, to drive the integrated GPU and nothing else. Hyprland reads that
from AQ_DRM_DEVICES -- and that variable is where every obvious way of naming
a card falls over:

- /dev/dri/card* numbers are not stable. On this laptop the dGPU came up as
  card1 on one boot and card0 on the next, so a config naming a number points
  at the wrong GPU sooner or later.
- /dev/dri/by-path/pci-0000:05:00.0-card is stable but unusable here:
  AQ_DRM_DEVICES splits its list on ":", so a PCI address arrives as several
  broken paths. Documented upstream at
  https://wiki.hypr.land/Configuring/Advanced-and-Cool/Multi-GPU/

What is left is a name of our own: a udev SYMLINK carrying neither a colon nor
a card number.

AND THE RULE THAT CREATES IT KEYS ON THE PCI ID, NOT THE ADDRESS. Reading the
address off this machine instead of baking one in is not enough, because the
address is not a property of the card at all -- it is a property of the bus
layout, and it moves when the layout does. Measured 2026-08-17: an added NVMe
pushed the iGPU from 0000:05:00.0 to 0000:06:00.0. The rule stopped matching,
the symlink was never created, the dotfiles guard on exists("/dev/dri/amd-igpu")
fell through, and the compositor came up on whatever it liked -- holding the
dGPU. Nothing logged an error. The failure surfaced three weeks later as a
handover the hook refused, with "the dGPU is still open".

The ID is also the identity the profile already uses to find the card
(`[igpu] ids`), so the rule and the search now agree on what the card is.

THE NAME KEEPS A VENDOR IN IT ON PURPOSE. "amd-igpu" is not a description, it
is a contract: the session half lives in dotfiles, whose Hyprland config
guards on exists("/dev/dri/amd-igpu") and points AQ_DRM_DEVICES at it.
Renaming this to something vendor-neutral would silently disarm that guard --
the compositor would come up on whichever card it liked and the next handover
would fail with no clue as to why. It changes when dotfiles changes, together.

This rule alone changes nothing: the compositor half -- AQ_DRM_DEVICES plus
the EGL/GLX/Vulkan ICD variables that must accompany it -- belongs to the
user's session and is not ours to write.

---------------------------------------------------------------------------
2. THE DISCRETE GPU'S SEAT RULE (72-vfio-dgpu-no-uaccess.rules)

The other half of pinning the compositor, and the one that makes a handover
repeatable rather than a once-per-boot trick.

AQ_DRM_DEVICES is read once, when the compositor starts. Everything after
that escapes it: when the hook gives the card back it reloads nvidia_drm, the
DRM node is born again as a hotplug event, and the compositor opens it without
ever consulting the filter. nvidia_drm then sits at refcnt=1 and the next
handover fails -- so, before this rule, the step that returned the card was
also the step that limited the machine to one handover per boot. A *failed*
handover did it too, since the hook's undo path reloads the module as well:
one bad attempt poisoned every attempt until the next reboot.

The file number is not decoration. /usr/lib/udev/rules.d/71-seat.rules adds
the tags this file removes and 73-seat-late.rules turns "uaccess" into an ACL;
a rule outside 71..73 is read too early or too late and does nothing at all.
The suffix matters just as much: udev reads only *.rules, and a first attempt
named ".rule" was silently ignored for a whole boot -- from the outside,
indistinguishable from a rule that does not work.

What must not be touched is as important as what is: the render node stays
0666 (PRIME offload goes through it), and the integrated card keeps
master-of-seat, which is what keeps seat0 alive.

---------------------------------------------------------------------------
3. THE HANDOVER HOOK AND ITS CONFIG (50-vfio-handover, vfio.conf)

The behaviour is in data/50-vfio-handover and is not re-derived here; what
this module owns is *installing* it: where the file goes, which PCI addresses
go into vfio.conf, and the mode without which libvirt silently skips it.

WHERE THE ADDRESSES COME FROM, NOW THAT THERE ARE PROFILES. The profile says
which card (vendor:device), the machine says where it is (PCI address). That
split is deliberate: ids identify the same model on a second machine of the
same kind, addresses are per-board and must never be written into a profile.
The IOMMU group is read from sysfs and re-checked here even though the gate
already asked -- this is the file that decides what gets handed to VFIO, and
it should not be able to disagree with the check that allowed it.

---------------------------------------------------------------------------
4. THE XORG SWITCH (20-vfio-no-autoaddgpu.conf)

Pinning the compositor is not enough, because the compositor is not the only
thing running. SDDM starts a plain Xorg for its greeter and, with -noreset,
that server stays up for the whole login session -- measured here as the
*only* process holding the card, from boot onwards. Xorg does not need to
display anything on it: AutoAddGPU is on by default, so the card arrives from
the udev backend as a secondary GPU screen, nvidia-utils'
10-nvidia-drm-outputclass.conf loads nvidia_drv.so for it, and that is enough
to open the device.

What that costs is the entire point of the project. Handing the card over with
Xorg still holding it makes the kernel say "Attempting to remove device ...
with non-zero usage count" and SIGABRT the greeter's server; SDDM loses its
display server and ends the user's session with it -- compositor, portals,
terminals. The hook cannot detect this either: its lsmod gate looks a second
later, by which time the kernel has already killed the holder.

Option "AutoAddGPU" "off" cuts exactly that link (man xorg.conf: "no GPU
devices will be added from the udev backend") and leaves the primary screen
alone. Three other candidates were measured and dropped: DisplayServer=wayland
needs weston and trades a measured certainty for an unknown; SDDM's [X11]
ServerArguments cannot scope it, because no such Xorg flag exists; AutoBindGPU
off only stops output-sink binding, the GPU screen and the open device remain.

The file is global, though, which is the whole gate: on a machine that really
runs X11 desktops it would also take away their PRIME offload outputs. So
installing refuses where /usr/share/xsessions/ has anything in it.

---------------------------------------------------------------------------
5. THE SHARED-MEMORY DEVICE FOR LOOKING GLASS (kvmfr)

A terminology trap first, because it inverts the usual meaning of the words:
Looking Glass calls the program running *inside* the VM the "host" (it
captures frames) and the program on the physical machine the "client" (it
displays them). This is the physical machine's side.

The two AUR packages hand over a client binary and DKMS sources, and stop
there. Everything that makes the shared-memory device usable is missing from
them, and that gap is what these three files close:

- the module is never loaded -- no modules-load.d entry,
- static_size_mb has no default -- no modprobe.d entry, and no constant we
  could ship either: the value follows the resolution being streamed,
- /dev/kvmfr0 comes up root:root 0600 -- no udev rule, so the client cannot
  open the device it exists to open.

The fourth piece, libvirt's cgroup device ACL, is an *edit* rather than a
generated file and lives further down for that reason.

THE SIZE HAS A SECOND READER. The domain XML carries the same number in bytes
(mem-path=/dev/kvmfr0, size=...), and the two must agree. They are written by
different things at different times, so a mismatch is not caught anywhere --
it shows up as a guest that boots fine and never produces a frame. install
prints the byte value for that reason.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_ASSET = PROJECT_ROOT / "data" / "50-vfio-handover"

def state_log(name: str) -> Path:
    """Where a round's transcript goes -- deliberately not /tmp.

    A transcript exists so a result survives the reader. It also has to survive
    the machine, and here those are different things: /tmp is a tmpfs, and the
    one failure these rounds cannot reconstruct afterwards is also the one
    whose only recovery is a reboot -- measured 2026-08-18, the round that left
    `modprobe` in D state took its own transcript with it, and what had
    happened had to be rebuilt from the journal and the hook log.

    XDG_STATE_HOME is the drawer for exactly this by its own definition --
    state that should persist between restarts and is not precious enough for
    XDG_DATA_HOME. The fallback is the spec's default and not /tmp: an
    unwritable path costs the file, which Tee already survives, while a tmpfs
    one costs the evidence.

    IT IS ONE FUNCTION BECAUSE IT IS ONE RULE, and the day it was two is what
    put it here. selftest moved off /tmp on 2026-08-18; guest setup did not,
    and kept a hardcoded tmpfs path under a comment that already said its
    transcript had to outlive a dead terminal. So for a while the round that
    actually held the card -- the only one that can wedge the rebind this
    reasoning is about -- was the one writing where a reboot erases it
    (measured 2026-08-19, the cardful mode 2 round).

    THE DIRECTORY IS NOT CREATED HERE. A path is not a file, and the callers
    that write one already create its parent inside their own Tee, where an
    unwritable drawer degrades to "no log" instead of killing the round.
    """
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base) if base.startswith("/") else Path.home() / ".local" / "state"
    return root / "vfioctl" / name


DRM_CLASS = Path("/sys/class/drm")

UDEV_IGPU = Path("/etc/udev/rules.d/70-vfio-igpu.rules")
UDEV_DGPU_SEAT = Path("/etc/udev/rules.d/72-vfio-dgpu-no-uaccess.rules")
UDEV_KVMFR = Path("/etc/udev/rules.d/99-kvmfr.rules")

HOOK_NAME = "50-vfio-handover"
HOOK_DIR = Path("/etc/libvirt/hooks/qemu.d")
HOOK = HOOK_DIR / HOOK_NAME
# One level up, out of the drop-in directory: libvirt runs every executable
# file in qemu.d/ whatever it is called, so a sibling .bak is a second hook --
# and the copy being replaced is, by definition, the older behaviour. Nothing
# reads /etc/libvirt/hooks/ itself; libvirt wants the exact name "qemu" there.
HOOK_BACKUP = HOOK_DIR.parent / (HOOK_NAME + ".bak")
# Where the hook writes. It is the hook's own default (VFIO_LOG in the script,
# overridable), repeated here because two readers on this side need it --
# selftest reads it to tell one round's lines from the last one's, doctor reads
# it to surface an NVMe identity mismatch the hook recorded. Third copy
# avoided: the two of them import this one.
HOOK_LOG = Path("/var/log/vfio-hook.log")
VFIO_CONF = Path("/etc/libvirt/hooks/vfio.conf")

XORG_CONF_DIR = Path("/etc/X11/xorg.conf.d")
XORG_AUTOADDGPU = XORG_CONF_DIR / "20-vfio-no-autoaddgpu.conf"
# X11 session desktop entries. Empty or absent means no X11 desktop is offered
# on this machine, which is what makes a global AutoAddGPU switch acceptable.
XSESSIONS = Path("/usr/share/xsessions")
# Where the running server says what it did. Only useful as a "before/after":
# it describes the Xorg that is up now, not the one the next boot will start.
XORG_LOG = Path("/var/log/Xorg.0.log")
# Xorg logs a GPU screen as "NVIDIA(G0)"; the primary screen would be
# "NVIDIA(0)". The G is the whole distinction being checked.
GPU_SCREEN_MARK = "NVIDIA(G"

MODULES_LOAD_KVMFR = Path("/etc/modules-load.d/kvmfr.conf")
MODPROBE_KVMFR = Path("/etc/modprobe.d/kvmfr.conf")
QEMU_CONF = Path("/etc/libvirt/qemu.conf")
KVMFR_NODE = Path("/dev/kvmfr0")

CLIENT_PKG = "looking-glass"
MODULE_PKG = "looking-glass-module-dkms"

ACL_KEY = "cgroup_device_acl"
ACL_DEVICE = "/dev/kvmfr0"

# The name the compositor is pointed at. Kept free of ":" on purpose, and kept
# vendor-named on purpose -- see the module docstring.
SYMLINK = "dri/amd-igpu"
DEV_LINK = Path("/dev") / SYMLINK

# What logind reads off a DRM node to decide it belongs to a seat, plus the
# uaccess tag that becomes the session ACL.
SEAT_TAGS = ("seat", "master-of-seat")
UACCESS_TAG = "uaccess"

# 32-bit RGB. Looking Glass can carry 64-bit HDR, but neither Xorg nor Wayland
# can display it today -- the drivers convert it back to SDR while it costs
# twice the memory and bandwidth. Doubling this is a deliberate, separate
# decision, not a default.
BPP = 4
# The client needs room for two frames plus about 10 MiB of overhead, rounded
# up to a power of two (Looking Glass B7 docs, "Determining memory").
OVERHEAD_MIB = 10

_MODE = re.compile(r"^(\d+)x(\d+)")


# --------------------------------------------------------------------------- #
# the managed set
# --------------------------------------------------------------------------- #

@dataclass
class Managed:
    """One file the tool owns end to end: it writes it, checks it, removes it."""

    key: str
    path: Path
    content: str
    what: str                      # one line, shown by --check
    mode: str | None = None
    backup: Path | None = None
    reload: str = ""               # "udev" | "libvirtd"


# --------------------------------------------------------------------------- #
# content
# --------------------------------------------------------------------------- #

IGPU_RULE = """\
# Written by vfioctl (install). Stable name for the GPU that keeps driving the
# host while the discrete card is bound to vfio-pci. Card numbers change
# between boots and AQ_DRM_DEVICES splits on ":", so by-path names cannot be
# used. The session half (AQ_DRM_DEVICES and the EGL/GLX/Vulkan pins) guards on
# this exact name and is not written by this tool.
#
# Matched by PCI ID, not by address. An address is not a property of the card,
# it is a property of the bus layout, and adding a disk renumbers it. Measured
# 2026-08-17 on this laptop: an added NVMe moved the iGPU from 0000:05:00.0 to
# 0000:06:00.0, this rule stopped firing, the symlink was never created,
# AQ_DRM_DEVICES pointed at a path that did not exist, and the compositor came
# up holding the dGPU. Nothing reported an error -- passthrough simply refused
# the next time a guest was started, three weeks later.
KERNEL=="card*", SUBSYSTEM=="drm", SUBSYSTEMS=="pci", \\
    ATTRS{{vendor}}=="0x{vendor}", ATTRS{{device}}=="0x{device}", \\
    SYMLINK+="{symlink}"
"""

DGPU_SEAT_RULE = """\
# Written by vfioctl (install). Keeps the discrete GPU's KMS node out of the
# session's hands, so a running compositor can never grab it.
#
# WHY: AQ_DRM_DEVICES filters GPUs only when the compositor *starts*. The node
# that appears when the handover hook reloads nvidia_drm arrives as a hotplug
# event, and the filter is not re-applied to it -- the compositor opens it,
# nvidia_drm sticks at refcnt=1, and the NEXT handover fails. So the step that
# gives the card back is what limits handovers to one per boot, and a failed
# handover does the same, because the hook's undo path reloads nvidia_drm too.
#
# HOW -- and why dropping "uaccess" alone is NOT enough (measured 2026-08-04):
# 71-seat.rules tags every drm card* with "seat" and "master-of-seat", so
# logind treats the node as a seat device. It opens it AS ROOT, hands the fd to
# the session over TakeDevice, and re-applies the uaccess ACL itself. With only
# TAG-="uaccess" the node came up root:root with group::--- and the compositor
# still held two fds on it -- proof it never opened the node itself. Dropping
# the seat tags is what takes the device out of logind's inventory; the mode
# below is defence in depth against a direct open.
#
# COST: none that was not already accepted -- the host does not drive a display
# on this card. seat0 survives, because the integrated GPU's card node keeps
# master-of-seat. Render offload is untouched: it goes through
# /dev/dri/renderD*, which this rule does not match, and the
# switcheroo-discrete-gpu tag is left in place.
#
# ESCAPE HATCH: if the session will not start, delete this file from a plain VT
# (Ctrl+Alt+F3), run `udevadm control --reload` and reboot.
# ADDRESSES ARE NOT IDENTITIES: matched by PCI ID for the same reason as
# 70-vfio-igpu.rules -- an added disk renumbered the iGPU here on 2026-08-17
# and silently disarmed that rule. This one would fail the same way, and its
# failure is quieter still: the dGPU's node would simply stay in the seat
# inventory and handovers would go back to one per boot.
ACTION=="remove", GOTO="vfio_dgpu_end"
SUBSYSTEM=="drm", KERNEL=="card*", SUBSYSTEMS=="pci", \\
    ATTRS{{vendor}}=="0x{vendor}", ATTRS{{device}}=="0x{device}", \\
    TAG-="uaccess", TAG-="seat", TAG-="master-of-seat", \\
    GROUP="root", MODE="0660"
LABEL="vfio_dgpu_end"
"""

VFIO_CONF_CONTENT = """\
# Written by vfioctl (install); read by
# {hook}.
#
# IOMMU group {group}, which was checked to hold nothing but the discrete GPU's
# own functions -- the kernel hands a group to VFIO whole, so anything else in
# it would make passthrough impossible. Which card this is comes from the
# profile ({profile}); where it sits comes from this machine's own PCI tree,
# because addresses are per-board and a profile that named one would be wrong
# on the second machine of the same model.
VFIO_DEVICES="{devices}"
"""

AUTOADDGPU_CONF = """\
# Written by vfioctl (install).
#
# Keeps the display manager's X greeter off the discrete GPU. Without this,
# Xorg adds the card from the udev backend as a secondary GPU screen, loads
# nvidia_drv.so for it and holds /dev/nvidia0 open for the whole session --
# even though it draws nothing there. Handing the card to a VM then makes the
# kernel kill the greeter's server, and SDDM ends the user's session with it.
#
# The primary screen is unaffected: it is not a GPU device and does not come
# from the udev backend. This file is global, so it is only installed on a
# machine that offers no X11 desktop session of its own -- on one that does, it
# would also remove its PRIME offload outputs.
Section "ServerFlags"
    Option "AutoAddGPU" "off"
EndSection
"""

MODULES_LOAD_CONTENT = """\
# Written by vfioctl (install). The DKMS package ships the kvmfr sources and
# nothing that loads them.
kvmfr
"""

MODPROBE_CONTENT = """\
# Written by vfioctl (install). The size follows the resolution being streamed,
# so it is read off this machine rather than baked in:
#   {basis}
# Raising it past what the guest needs buys no speed -- it only takes that RAM
# away from the host for good. The domain XML carries the same number in bytes
# ({size_bytes}); if the two disagree the guest boots and never shows a frame.
options kvmfr static_size_mb={mb}
"""

KVMFR_RULE = """\
# Written by vfioctl (install). The module creates /dev/kvmfr0 as root:root
# 0600; the Looking Glass client runs as the user and has to open it.
SUBSYSTEM=="kvmfr", OWNER="{user}", GROUP="kvm", MODE="0660"
"""


def _ids(ids: str) -> dict[str, str]:
    """Split "1002:1681" into the two halves a udev ATTRS match needs.

    The profile already names cards this way (`[igpu] ids`), so a rule keyed on
    the ID is keyed on the same identity the tool used to find the card in the
    first place -- unlike the address, which is only where that card happened
    to sit on the boot the tool was run.
    """
    vendor, _, device = ids.partition(":")
    return {"vendor": vendor, "device": device}


def igpu_rule(ids: str) -> str:
    return IGPU_RULE.format(symlink=SYMLINK, **_ids(ids))


def dgpu_seat_rule(ids: str) -> str:
    return DGPU_SEAT_RULE.format(**_ids(ids))


def vfio_conf(group: str, devices: list[str], profile: str) -> str:
    return VFIO_CONF_CONTENT.format(
        hook=HOOK, group=group, devices=" ".join(devices), profile=profile
    )


def modprobe_conf(mb: int, basis: str) -> str:
    return MODPROBE_CONTENT.format(mb=mb, basis=basis, size_bytes=mb * 1024 * 1024)


def kvmfr_rule(user: str) -> str:
    return KVMFR_RULE.format(user=user)


# --------------------------------------------------------------------------- #
# reading the machine for the values the templates need
# --------------------------------------------------------------------------- #

def connected_modes() -> list[tuple[int, int]]:
    """(width, height) of the preferred mode of every connected output.

    The first line of a connector's `modes` file is its preferred mode, which
    for a fixed panel is its native resolution. Disconnected outputs still have
    the file and would otherwise contribute a mode nothing can display.
    """
    found = []
    try:
        connectors = sorted(DRM_CLASS.iterdir())
    except OSError:
        return found
    for connector in connectors:
        try:
            status = (connector / "status").read_text(encoding="utf-8").strip()
            modes = (connector / "modes").read_text(encoding="utf-8")
        except OSError:
            continue
        if status != "connected":
            continue
        for line in modes.splitlines():
            match = _MODE.match(line.strip())
            if match:
                found.append((int(match.group(1)), int(match.group(2))))
                break
    return found


def largest_mode() -> tuple[int, int] | None:
    """The biggest connected display, or None on a machine showing nothing.

    The client draws the guest into a window on this machine, so no guest
    resolution above the largest local display is worth paying memory for.
    """
    modes = connected_modes()
    return max(modes, key=lambda mode: mode[0] * mode[1]) if modes else None


def required_mb(width: int, height: int, bpp: int = BPP) -> int:
    """Shared memory a frame of this size needs, in MiB."""
    frame = width * height * bpp * 2
    needed = frame / 1024 / 1024 + OVERHEAD_MIB
    return 1 << math.ceil(math.log2(needed))


def kvmfr_size(override: int | None = None) -> tuple[int | None, str]:
    """(MiB, why that number). None when there is nothing to calculate from."""
    if override:
        return override, f"elle verildi: {override} MiB"
    mode = largest_mode()
    if mode is None:
        return None, ""
    width, height = mode
    return required_mb(width, height), (
        f"{width}x{height} x {BPP} bytes x 2 frames + {OVERHEAD_MIB} MiB,"
        " rounded up to a power of two"
    )


def x11_sessions() -> list[str]:
    """X11 desktop sessions this machine offers, by desktop-entry name."""
    try:
        return sorted(entry.name for entry in XSESSIONS.glob("*.desktop"))
    except OSError:
        return []


def gpu_screens() -> int | None:
    """NVIDIA GPU screens the *running* X server created; None if unreadable.

    This answers "is it in effect yet", not "is the file right": the log
    belongs to the server that is up now, which on a machine that has not
    rebooted was started before the file existed.
    """
    try:
        return XORG_LOG.read_text(encoding="utf-8", errors="replace").count(
            GPU_SCREEN_MARK
        )
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# libvirt's cgroup device ACL -- an edit, not a generated file
# --------------------------------------------------------------------------- #
#
# THIS FILE HAS TWO OWNERS AND THAT IS THE DESIGN. qemu.conf's user/group lines
# say which account QEMU runs as; that is true of any virtualisation host and
# is not this tool's business. Only the ACL entry below is, because it exists
# solely so a passthrough guest can reach the Looking Glass device. So the two
# edits are line-scoped and neither rewrites the file: this one never touches
# user/group, and install reports them rather than setting them.
#
# Naming cgroup_device_acl at all replaces libvirt's built-in list, so a block
# holding only /dev/kvmfr0 would take /dev/null, /dev/urandom and the rest away
# from every VM on the machine. Rather than reconstruct that list from memory,
# this uncomments the sample block libvirt itself ships and adds one line to
# it; a file carrying neither an active nor a commented block is refused
# instead of guessed at.

def _acl_block(text: str, commented: bool) -> tuple[int, int] | None:
    """(start, end) line indices of the ACL block, or None if there is none."""
    prefix = "#" if commented else ""
    start = None
    for index, line in enumerate(text.splitlines()):
        if start is None:
            if line.startswith(f"{prefix}{ACL_KEY}") and line.rstrip().endswith("["):
                start = index
            continue
        if line.rstrip() == f"{prefix}]":
            return start, index
    return None


def _has_active_key(text: str) -> bool:
    return any(line.startswith(ACL_KEY) for line in text.splitlines())


def acl_allows_kvmfr(text: str) -> bool:
    """True when an *active* cgroup_device_acl already lists the device."""
    if not _has_active_key(text):
        return False
    lines = text.splitlines()
    block = _acl_block(text, commented=False)
    if block is None:
        # The key is set but not as a block we recognise; the honest answer
        # about a list we cannot parse is whatever it plainly contains.
        return any(line.startswith(ACL_KEY) and ACL_DEVICE in line for line in lines)
    start, end = block
    return any(ACL_DEVICE in line for line in lines[start : end + 1])


def acl_with_kvmfr(text: str) -> str | None:
    """qemu.conf text with /dev/kvmfr0 added to cgroup_device_acl.

    None when there is no block that can be edited safely. An active key we
    cannot parse is refused rather than joined -- a second active assignment
    leaves the outcome to file order.
    """
    lines = text.splitlines()

    block = _acl_block(text, commented=False)
    if block is not None:
        start, _ = block
        lines.insert(start + 1, f'    "{ACL_DEVICE}",')
        return "\n".join(lines) + "\n"

    if _has_active_key(text):
        return None

    block = _acl_block(text, commented=True)
    if block is None:
        return None
    start, end = block
    uncommented = [
        line[1:] if line.startswith("#") else line for line in lines[start : end + 1]
    ]
    uncommented.insert(1, f'    "{ACL_DEVICE}",')
    return "\n".join(lines[:start] + uncommented + lines[end + 1 :]) + "\n"


def acl_without_kvmfr(text: str) -> str | None:
    """qemu.conf text with our one ACL line taken back out, or None if absent.

    Only the line this tool added is removed. The rest of the block stays as
    it is, including the uncommenting -- putting the sample back into comments
    would hand every VM on the machine a different device list than the one it
    has been running with, which is a bigger change than the one being undone.
    """
    lines = text.splitlines()
    kept = [
        line for line in lines
        if line.strip().strip(",").strip('"') != ACL_DEVICE
    ]
    if len(kept) == len(lines):
        return None
    return "\n".join(kept) + "\n"


# --------------------------------------------------------------------------- #
# assembling the set
# --------------------------------------------------------------------------- #

@dataclass
class Layout:
    """The addresses the templates need, resolved on this machine."""

    dgpu: str
    dgpu_audio: str | None
    igpu: str
    group: str
    group_members: list[str]
    profile: str
    # Addresses say where the cards sat on the boot this was resolved; the IDs
    # say which cards they are. Anything written to a file that outlives the
    # boot keys on the IDs -- see IGPU_RULE.
    dgpu_ids: str
    igpu_ids: str


def managed_files(
    layout: Layout, user: str, kvmfr_mb: int, kvmfr_basis: str
) -> list[Managed]:
    """Every file the tool writes, in the order it writes them."""
    return [
        Managed(
            "igpu-symlink", UDEV_IGPU, igpu_rule(layout.igpu_ids),
            "iGPU'ya kararlı ad (/dev/dri/amd-igpu) — compositor buna sabitlenir",
            reload="udev",
        ),
        Managed(
            "dgpu-seat", UDEV_DGPU_SEAT, dgpu_seat_rule(layout.dgpu_ids),
            "dGPU'nun KMS düğümünü seat envanterinden çıkarır — devri tekrarlanabilir kılan kural",
            reload="udev",
        ),
        Managed(
            "hook", HOOK, HOOK_ASSET.read_text(encoding="utf-8"),
            "devri yapan libvirt hook'u",
            mode="0755", backup=HOOK_BACKUP, reload="libvirtd",
        ),
        Managed(
            "vfio-conf", VFIO_CONF,
            vfio_conf(layout.group, layout.group_members, layout.profile),
            "hook'un okuduğu PCI adresleri",
        ),
        Managed(
            "xorg-autoaddgpu", XORG_AUTOADDGPU, AUTOADDGPU_CONF,
            "SDDM greeter'ının Xorg'unu dGPU'dan uzak tutar — kalkarsa sıcak devir kalkar",
        ),
        Managed(
            "kvmfr-modules-load", MODULES_LOAD_KVMFR, MODULES_LOAD_CONTENT,
            "kvmfr modülünü boot'ta yükler",
        ),
        Managed(
            "kvmfr-modprobe", MODPROBE_KVMFR,
            modprobe_conf(kvmfr_mb, kvmfr_basis),
            f"kvmfr paylaşımlı bellek boyutu ({kvmfr_mb} MiB)",
        ),
        Managed(
            "kvmfr-udev", UDEV_KVMFR, kvmfr_rule(user),
            "/dev/kvmfr0'ı kullanıcının açabilmesi için",
            reload="udev",
        ),
    ]
