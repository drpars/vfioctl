"""Is the graphics session keeping its hands off the discrete card?

WHY THIS IS MEASURED AND NOT INSTALLED. Pinning the compositor to the
integrated GPU is a load-bearing condition of the whole design, and it is the
one part of it this tool does not own: the variables live in the user's own
session configuration -- on this machine a dotfiles repository, on another
whatever starts their desktop. Writing there would mean editing files with a
different owner, on a path this tool cannot check back afterwards. The
precedent is already in the code: qemu.conf's user/group lines are reported
and never written, for the same reason. So the criterion is stated and this
module measures whether the machine meets it.

WHAT THE CRITERION ACTUALLY IS, AND WHY IT IS NOT COMPOSITOR-SPECIFIC. It is a
fact about file descriptors: nothing in the graphics session may hold the
discrete card's DRM node or a /dev/nvidia* node when the handover runs. Which
setting produces that state does differ per compositor -- AQ_DRM_DEVICES is
Hyprland's, KWin has its own -- but three of the four variables this machine
sets are not compositor variables at all. The two glvnd ones and
VK_DRIVER_FILES belong to the EGL/GLX and Vulkan loaders and do the same job
under any desktop; only where they are set changes. That is what lets the tool
state a criterion it can measure on the user's machine instead of promising
support it has not tested.

WHY THE CHECKS ARE SOFT, AND WHY "COULD NOT MEASURE" IS ITS OWN ANSWER.
doctor.gate() asks whether this machine is the kind of machine the design
works on, which is a permanent question. This asks what the session is doing
at this moment, which changes at every boot. Putting it behind the gate would
lock the gate against the tool's own users: selftest is meant to be run from a
plain VT, where there is no compositor to measure. So an unanswerable question
returns None, prints as "olculemedi", and blocks nothing -- calling it a
failure would teach the reader to ignore the check.

DISCOVERY GOES THROUGH LOGIND, NOT THROUGH A PROCESS NAME. selftest takes a
--compositor argument because it has to watch one named process survive N
rounds; this does not. It asks logind which session is active on the seat and
reads the desktop's name from the session's own properties. Grepping /proc for
"Hyprland" would report "no compositor" on exactly the machines this
measurement exists for. It also works from a seatless shell: the question is
about seat0, not about the caller.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")

# Nodes whose open fds keep the nvidia modules loaded, and so keep the card
# from moving. Deliberately narrow: /dev/nvidia0 and /dev/nvidiactl are the
# ones a probing process opens. The DRM node is matched separately because it
# is the one that is easy to miss -- nvidia-smi reports nothing holding the
# card, no /dev/nvidia* fd exists, and nvidia_drm still sits at refcnt=1.
NVIDIA_NODE = re.compile(r"^/dev/nvidia([0-9]+|ctl)$")


@dataclass
class Session:
    id: str
    seat: str
    type: str        # wayland | x11 | tty
    desktop: str     # what logind recorded, e.g. "Hyprland" -- may be empty
    leader: int
    active: bool


@dataclass
class Holder:
    pid: int
    comm: str
    nodes: list[str]

    def __str__(self) -> str:
        return f"{self.comm}({self.pid}) → {' '.join(self.nodes)}"


# --------------------------------------------------------------------------- #
# who is logged in on the seat
# --------------------------------------------------------------------------- #

def _properties(args: list[str]) -> dict[str, str]:
    try:
        out = subprocess.run(["loginctl", *args],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    props: dict[str, str] = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            props[key] = value
    return props


def active_session(seat: str = "seat0") -> Session | None:
    """The graphics session on the seat, or None when there is nothing to ask.

    None is an answer rather than a failure. A machine with no seat at all --
    a headless host, a container -- has no session half to measure, and saying
    so is more useful than reporting that it failed.
    """
    active = _properties(["show-seat", seat, "-p", "ActiveSession"]).get(
        "ActiveSession", "")
    if not active:
        return None
    props = _properties([
        "show-session", active, "-p", "Id", "-p", "Seat", "-p", "Type",
        "-p", "Desktop", "-p", "Leader", "-p", "Active",
    ])
    if not props:
        return None
    try:
        leader = int(props.get("Leader", "0"))
    except ValueError:
        leader = 0
    return Session(
        id=props.get("Id", active),
        seat=props.get("Seat", seat),
        type=props.get("Type", ""),
        desktop=props.get("Desktop", ""),
        leader=leader,
        active=props.get("Active", "") == "yes",
    )


# --------------------------------------------------------------------------- #
# what is holding the card
# --------------------------------------------------------------------------- #

def open_dev_nodes(pid: int | str | None) -> list[str]:
    """Every /dev/nvidia* and /dev/dri/* node one process has open."""
    if not pid:
        return []
    found = []
    try:
        entries = list((PROC / str(pid) / "fd").iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("/dev/nvidia") or target.startswith("/dev/dri/"):
            found.append(target)
    return sorted(found)


def _scan(match) -> list[Holder]:
    """Processes holding a device node the predicate accepts, right now.

    THE FD TABLE IS THE MEASUREMENT, NOT THE ENVIRONMENT. Looking for
    AQ_DRM_DEVICES in /proc/PID/environ gives a false negative on this
    machine: Hyprland sets those variables on itself from its own config, so
    they never appear in the environment it was started with. What decides
    whether a handover works is which files are open, and that is what is
    read.

    Only this user's processes are readable without privilege, and the one
    holder that hides there is the display manager's X greeter, running as
    root. It is not missed: it is measured from the GPU screens it writes into
    its own log (hostfiles.gpu_screens), which is how it was caught the first
    time.
    """
    out: list[Holder] = []
    own = os.getpid()
    for entry in PROC.glob("[0-9]*"):
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if pid == own:
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        nodes = set()
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if match(target):
                nodes.add(target)
        if nodes:
            try:
                comm = (entry / "comm").read_text(encoding="utf-8").strip()
            except OSError:
                comm = "?"
            out.append(Holder(pid=pid, comm=comm, nodes=sorted(nodes)))
    return sorted(out, key=lambda h: h.pid)


def card_holders(card: str | None) -> list[Holder]:
    """Who is holding the discrete card: its DRM node, or any /dev/nvidia*.

    `card` is the DRM node name resolved from the PCI address a moment ago,
    never a remembered number: minor numbers shuffle between boots and across
    a handover, so card0 is the discrete GPU on one boot and the integrated
    one on the next.
    """
    wanted = f"/dev/dri/{card}" if card else None

    def match(target: str) -> bool:
        return bool(NVIDIA_NODE.match(target)) or (
            wanted is not None and target == wanted)

    return _scan(match)


# --------------------------------------------------------------------------- #
# what is said when it does not pass
# --------------------------------------------------------------------------- #
#
# Three kinds of statement, and the difference between them is the whole point:
# the criterion is measurable and so is stated unconditionally, the address is
# stated with the fact that it was not measured, and a recipe -- "write this
# there and it works" -- is not written at all. A recipe for a desktop nobody
# here has run would be a promise this tool cannot keep.

CRITERION = """\
Ölçüt (vaat değil, tasarımın olgusu — ve ölçülebilir):
  Devir anında grafik oturumunun hiçbir süreci dGPU'nun DRM düğümünü
  (/dev/dri/card*) ya da /dev/nvidia*'ı açık tutmamalı.
  Sabitleme compositor BAŞLAMADAN ÖNCE kurulmalı: cihaz seçimi süreç
  başlarken bir kez okunur, sonradan set etmek çalışmaz."""

ADDRESS = """\
Nerede kurulur (bu araç seans yarısını yazmaz — sahibi kullanıcının kendi
masaüstü yapılandırmasıdır):
  · dGPU'yu compositor'ün cihaz listesinden çıkaran değişken compositor'e
    özgüdür: Hyprland'de AQ_DRM_DEVICES (bu makinede ölçüldü), KWin'in de
    kendi DRM cihaz seçici değişkeni var — KWIN_DRM_DEVICES (BU ARAÇ
    TARAFINDAN ÖLÇÜLMEDİ).
  · Diğer üç değişken compositor'e özgü DEĞİL: glvnd ve Vulkan loader'ına
    aitler, her masaüstünde aynı işi yaparlar ve /dev/nvidia*'ı açan yolu
    (EGL/Vulkan probe'unu) keserler. Değişen yalnızca nerede set edildikleri:
        __EGL_VENDOR_LIBRARY_FILENAMES  → iGPU'nun EGL vendor JSON'u
        __GLX_VENDOR_LIBRARY_NAME       → mesa
        VK_DRIVER_FILES                 → iGPU'nun Vulkan ICD'si
  · DRM düğümü tarafını vfioctl'in kendi udev kuralı
    (72-vfio-dgpu-no-uaccess.rules) zaten generic olarak kapatır: erişimi
    kesen şey seat etiketleri, fd'yi veren logind (TakeDevice) — mekanizma
    logind'in, compositor'ün değil. Hyprland'de ölçüldü, diğerlerinde
    ölçülmedi."""
