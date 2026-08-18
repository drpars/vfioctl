"""Build a Windows guest unattended, from a blank disk to a reachable qemu-ga.

    ./vfioctl guest build            # the whole round, ~7-10 min, unattended
    ./vfioctl guest setup            # push and drive the guest-side scripts
    ./vfioctl guest passthrough      # give the domain the dGPU (or --off)
    ./vfioctl guest nvme             # give the domain a whole NVMe controller
    ./vfioctl guest status           # where is it now
    ./vfioctl guest screenshot       # what is on its screen
    ./vfioctl guest autologon        # re-run just the console-session step
    ./vfioctl guest clean            # undefine + delete disk, nvram, helper ISO

    THIS FILE IS NOT RUNNABLE ON ITS OWN (K15). It is reached through the one
    entry point above; a second runnable name is the one that goes stale.

WHAT THIS REPLACES. Five stages used to be done by hand: defining the domain,
answering Setup, waiting for the agent, opening a console session, and running
the three post-install scripts. All five are here now. `build` stops at a guest
that can be driven; `setup` is the half that drives it, separate because a
guest is worth building for reasons that have nothing to do with the display.

WHAT `setup` MEASURES RATHER THAN CLAIMS. Each script in the chain installs
something the next one needs, so between them the guest's own inventory is read
back: the monitor VDD created, the Looking Glass service's state, the display
topology's verdict, and at the end the adapter Looking Glass actually captures
on. An installer's exit code says the installer finished, which is a different
sentence.

THE SAME ROUND RUNS WITH AND WITHOUT THE CARD, and that is the point of
`passthrough` being a separate step. Without a hostdev the domain can be driven
from the desktop as often as debugging needs, because nothing takes the card
away; with one, `setup --start` is a handover round and belongs on a plain VT
like `vfioctl selftest` does -- so it starts the guest itself, prints which
driver the card ended up on, and tees everything to a log that outlives the
terminal. The two rounds differ by exactly one thing, which is what makes the
second one's result readable.

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
PCI_DEVICES = Path("/sys/bus/pci/devices")

# Same reason core/selftest.py writes one: a round that hands the card over can
# take the graphics session with it, and a result that only existed in a dead
# terminal is a round that has to be run again.
SETUP_LOG = Path("/tmp/vfioctl-setup.log")

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


class Tee:
    """stdout that also lands in a file, so a dying session cannot eat the run."""

    def __init__(self, path: Path):
        self.stream = sys.stdout
        try:
            self.file = path.open("a", encoding="utf-8")
        except OSError:
            self.file = None

    def write(self, text: str) -> int:
        if self.file:
            self.file.write(text)
            self.file.flush()
        return self.stream.write(text)

    def flush(self) -> None:
        if self.file:
            self.file.flush()
        self.stream.flush()

    def isatty(self) -> bool:
        """False even when the terminal half is one, so nothing paints.

        Half of this stream is SETUP_LOG, and that file is what a round is read
        from after the fact. Escape codes written for the terminal land in it
        as well. core/term.py owns the rest of that rule.
        """
        return False


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg, code=1):
    print(f"HATA: {msg}", file=sys.stderr)
    sys.exit(code)


def self_cmd(*words: str) -> str:
    """A copy-pasteable invocation of this tool.

    The guest side is reached as `vfioctl guest ...` and nowhere else since
    K15, so a hint that prints only the flags prints a command that does not
    run. Six of them did, left over from the rename; going through one place
    means the next one cannot.

    The spelling itself is core/provenance.py's answer, not raw argv[0]: from a
    PATH invocation argv[0] is an absolute path, which runs but reads as noise
    (K20). Imported here rather than at module scope because core/ is a sibling
    directory this file reaches through sys.path, the same way every other
    function below does.
    """
    if str(HERE.parent) not in sys.path:
        sys.path.insert(0, str(HERE.parent))
    from core.provenance import command
    return command(*words)


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


def guard_exclusive_devices(name: str):
    """Refuse to start a guest while another running one claims the same device.

    Two things here cannot be shared and both are single: the dGPU, and the
    kvmfr window at /dev/kvmfr0. Leaving the clash to libvirt means the operator
    reads "VM failed to start" and has to work out for themselves whether the
    cause was the card, the shared memory, the hook or the domain. This is the
    round where a second domain claiming either exists on this machine for the
    first time.
    """
    ours = domain_claims(name)
    if not ours:
        return
    for other in defined_domains():
        if other == name or domain_state(other) != "running":
            continue
        clash = ours & domain_claims(other)
        if clash:
            die(f"'{other}' çalışıyor ve aynı cihazı istiyor "
                f"({', '.join(sorted(clash))}) -- paylaşılamaz, önce onu kapat.")


def domain_claims(name: str) -> set[str]:
    """Host devices the domain takes exclusively: PCI functions and mem-paths.

    The mem-path comes out of qemu:commandline with a regex rather than an XML
    walk, because the argument is a JSON string inside an attribute and libvirt
    is free to hand it back with either quoting style.
    """
    r = virsh("dumpxml", name, check=False)
    if r.returncode != 0:
        return set()
    claims = {f"mem-path:{p}" for p in
              re.findall(r"mem-path[\"']?\s*:\s*[\"']([^\"']+)", r.stdout)}
    try:
        root = ElementTree.fromstring(r.stdout)
    except ElementTree.ParseError:
        return claims
    for hostdev in root.findall("./devices/hostdev[@type='pci']"):
        addr = hostdev.find("./source/address")
        if addr is None:
            continue
        try:
            claims.add("{:04x}:{:02x}:{:02x}.{}".format(
                int(addr.get("domain", "0x0"), 16),
                int(addr.get("bus", "0x0"), 16),
                int(addr.get("slot", "0x0"), 16),
                int(addr.get("function", "0x0"), 16),
            ))
        except ValueError:
            continue
    return claims


def pci_claims(name: str) -> set[str]:
    """Only the PCI half of domain_claims -- i.e. does this domain take the card."""
    return {c for c in domain_claims(name) if not c.startswith("mem-path:")}


def host_driver_of(address: str) -> str:
    """Which host driver holds a PCI function right now, or "(none)".

    Read straight from sysfs, and read rather than assumed: after a start it is
    the one line that says whether the handover hook did its job. Nothing here
    writes to these paths -- the hook is the only writer, deliberately.
    """
    link = PCI_DEVICES / address / "driver"
    if link.is_symlink():
        return os.path.basename(os.path.realpath(link))
    return "(none)"


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


KVMFR_NODE = Path("/dev/kvmfr0")
KVMFR_MODPROBE = Path("/etc/modprobe.d/kvmfr.conf")

QEMU_NS = " xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'"

IVSHMEM_BLOCK = """  <qemu:commandline>
    <qemu:arg value='-device'/>
    <qemu:arg value="{{'driver':'ivshmem-plain','id':'shmem0','memdev':'looking-glass'}}"/>
    <qemu:arg value='-object'/>
    <qemu:arg value="{{'qom-type':'memory-backend-file','id':'looking-glass','mem-path':'{node}','size':{size},'share':true}}"/>
  </qemu:commandline>
"""


def kvmfr_size_bytes() -> int | None:
    """The host's kvmfr window, in bytes, read from the host rather than guessed.

    The size in the domain and static_size_mb on the host MUST agree; a guest
    that maps a different size gives a client which attaches and shows nothing.
    The parameter cannot be read back from sysfs -- kvmfr declares it with
    perm=0, so /sys/module/kvmfr/parameters never appears -- so the file that
    set it is the only source there is.
    """
    try:
        text = KVMFR_MODPROBE.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"static_size_mb\s*=\s*(\d+)", text)
    return int(m.group(1)) * 1024 * 1024 if m else None


def ivshmem_parts() -> tuple[str, str]:
    """(namespace attribute, qemu:commandline block) for the Looking Glass path.

    Both halves or neither: a qemu:commandline block without xmlns:qemu is
    dropped by libvirt SILENTLY -- no error, and the arguments simply never
    reach QEMU. Absent kvmfr on the host, the domain is written without either,
    which is a guest that boots and installs fine and simply has no LG.
    """
    if not KVMFR_NODE.exists():
        say(f"{KVMFR_NODE} yok -- domain ivshmem'siz yazılıyor (LG adımı çalışmaz)")
        return "", ""
    size = kvmfr_size_bytes()
    if size is None:
        say(f"{KVMFR_MODPROBE} okunamadı -- domain ivshmem'siz yazılıyor")
        return "", ""
    say(f"ivshmem: {KVMFR_NODE}, {size // (1024 * 1024)} MB (host'un static_size_mb'si)")
    return QEMU_NS, IVSHMEM_BLOCK.format(node=KVMFR_NODE, size=size)


# --------------------------------------------------------------------------- #
# handing the card to the domain
# --------------------------------------------------------------------------- #

# managed='no' IS THE WHOLE POINT AND IT IS NOT A DETAIL. With managed='yes'
# libvirt detaches the device from its host driver itself, which would make it a
# second writer to the same sysfs paths as the handover hook -- and a second
# writer is what wedged this machine three times. The hook binds, libvirt only
# opens what it is given. This mirrors the working guest's XML byte for byte.
#
# The guest-side <address> is deliberately absent: libvirt assigns a PCIe root
# port and a slot, the same way it did for the guest this was copied from.
HOSTDEV_XML = """    <hostdev mode='subsystem' type='pci' managed='no'>
      <source>
        <address domain='0x{domain:04x}' bus='0x{bus:02x}' \
slot='0x{slot:02x}' function='0x{function:x}'/>
      </source>
    </hostdev>
"""

HOSTDEV_BLOCK = re.compile(
    r"[ \t]*<hostdev mode='subsystem' type='pci'.*?</hostdev>\n",
    re.DOTALL,
)


def address_parts(address: str) -> dict[str, int]:
    """0000:01:00.0 -> the four numbers libvirt wants, or a refusal."""
    m = re.fullmatch(r"([0-9a-f]{4}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])",
                     address.lower())
    if not m:
        die(f"PCI adresi anlaşılmadı: {address}")
    d, b, s, f = m.groups()
    return {"domain": int(d, 16), "bus": int(b, 16),
            "slot": int(s, 16), "function": int(f, 16)}


def redefine(xml_text: str):
    """Hand libvirt an edited domain XML. Callers verify the result afterwards."""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
        fh.write(xml_text)
        tmp = fh.name
    try:
        virsh("define", tmp, capture=False)
    finally:
        os.unlink(tmp)


def dgpu_addresses(profile_name: str | None) -> list[str]:
    """The PCI functions this machine's discrete GPU occupies, gate first.

    THE GATE IS core.doctor.gate() AND NOT A CHECK OF OUR OWN. A domain that
    names a PCI address is as machine-specific as anything `install` writes, and
    on a machine the tool does not recognise the address would be a guess with a
    VM built on top of it. CLAUDE.md: one owner for that question, so that a new
    subcommand cannot accidentally skip it.

    The audio function comes along because it shares the IOMMU group: handing
    over one without the other leaves the guest with a card whose sound device
    is still on the host, and the group is the unit a handover moves.
    """
    sys.path.insert(0, str(HERE.parent))
    from core import doctor, hostfiles, install as install_mod, probe

    open_gate, p, _ = doctor.gate(profile_name)
    if not open_gate or p is None:
        die("Kapı kapalı -- bu makinede karta dokunan bir domain tanımlanmaz. "
            f"Teşhis: {self_cmd('doctor')}")
    layout = install_mod.resolve(probe.read_machine(), p)
    if layout is None:
        die(f"Kartın adresleri çözülemedi. Teşhis: {self_cmd('doctor')}")
    if not hostfiles.HOOK.exists():
        say(f"UYARI: {hostfiles.HOOK} yok -- kartı devredecek hook kurulu değil, "
            f"domain başlatılamaz. Önce: {self_cmd('install')}")
    return [layout.dgpu] + ([layout.dgpu_audio] if layout.dgpu_audio else [])


def cmd_passthrough(a):
    """Put the card into a domain we built, or take it back out.

    WHY THIS IS A COMMAND AND NOT `virsh edit`. It is the one difference between
    the cardless rehearsal and the round that proves the setup, so it has to be
    a single reversible step rather than a hand edit made at the wrong moment --
    and the moment is usually a plain VT with the desktop's card at stake.

    IT ONLY EDITS A SHUT-OFF DOMAIN. Attaching a hostdev to a running guest asks
    libvirt to bind the device there and then, outside the hook and with the
    host still driving the card. The hook runs at domain start; that is the only
    time the card moves.

    The edit is a text insertion rather than an XML round trip on purpose: the
    stored XML carries blocks this script did not write -- libosinfo's metadata,
    our own ownership mark, the qemu:commandline that carries Looking Glass --
    and re-serialising all of them to change two is how one of them quietly
    comes back different. What changed is read back from libvirt afterwards.
    """
    disk = Path(a.disk)
    guard(a.name, disk)
    if not domain_exists(a.name):
        die(f"'{a.name}' tanımlı değil")
    state = domain_state(a.name)
    if state != "shut off":
        die(f"'{a.name}' şu an '{state}' -- kart yalnızca kapalı bir domain'e "
            f"eklenir. Kartı domain açılırken hook taşır, çalışırken kimse.")

    addresses = dgpu_addresses(a.profile)
    xml = virsh("dumpxml", a.name).stdout
    kept, dropped = [], []
    for block in HOSTDEV_BLOCK.findall(xml):
        (dropped if any(_matches(block, addr) for addr in addresses)
         else kept).append(block)

    if a.off:
        if not dropped:
            say(f"'{a.name}' zaten kartsız -- değişiklik yok")
            return 0
        for block in dropped:
            xml = xml.replace(block, "", 1)
    else:
        if len(dropped) == len(addresses):
            say(f"'{a.name}' kartı zaten alıyor -- değişiklik yok")
            return 0
        # A partial set is worse than none -- the guest would get the GPU
        # without its audio function -- so the pair is rewritten as a whole.
        for block in dropped:
            xml = xml.replace(block, "", 1)
        added = "".join(HOSTDEV_XML.format(**address_parts(addr))
                        for addr in addresses)
        xml = xml.replace("  </devices>", added + "  </devices>", 1)

    redefine(xml)

    # Read back rather than trust the define: this is the step where a lost
    # ownership mark or a dropped ivshmem block would only surface as "Looking
    # Glass shows nothing" a quarter of an hour later.
    after = virsh("dumpxml", a.name).stdout
    claims = pci_claims(a.name)
    say(f"'{a.name}' aldığı PCI işlevleri: {' '.join(sorted(claims)) or '(yok)'}")
    say(f"  işaret: {'duruyor' if marker_of(a.name) else 'YOK -- kaybolmuş'}")
    say(f"  ivshmem: {'duruyor' if 'mem-path' in after else 'yok'}")
    if a.off:
        remaining = set(addresses) & claims
        if remaining:
            die(f"kart çıkarılamadı -- domain hâlâ istiyor: "
                f"{' '.join(sorted(remaining))}")
    else:
        missing = set(addresses) - claims
        if missing:
            die(f"kart eklenemedi -- eksik: {' '.join(sorted(missing))}")
        for addr in addresses:
            say(f"  {addr} şu an host'ta: {host_driver_of(addr)}")
        say("Bundan sonrası düz VT'den koşulur: "
            + self_cmd("guest", "--name", a.name, "setup", "--start"))
    return 0


def _matches(block: str, address: str) -> bool:
    """Does this <hostdev> block point at that PCI function?

    Matched on the four attributes rather than on a rendered line, because the
    text being searched is libvirt's output and this script's rendering only
    has to agree with it semantically.
    """
    parts = address_parts(address)
    return all(f"{key}='0x{parts[key]:0{width}x}'" in block
               for key, width in (("domain", 4), ("bus", 2),
                                  ("slot", 2), ("function", 1)))


# --------------------------------------------------------------------------- #
# handing a whole NVMe controller to the domain
# --------------------------------------------------------------------------- #

# managed='yes' HERE, AND managed='no' FOR THE CARD -- THE DIFFERENCE IS WHO
# BINDS, AND IT IS THE SAME ONE-WRITER RULE IN BOTH CASES.
#
# The card has a writer already: the handover hook, which has to unload the
# nvidia stack in a particular order before anything touches unbind. Letting
# libvirt detach it too would put a second writer on the same sysfs paths, and
# a second writer is what wedged this machine three times. So the card is
# managed='no' -- the hook binds, libvirt only opens what it is given.
#
# A disk has no such writer and needs none. There is no module to unload, no
# poller reading driver_override, no desktop holding a device node: the nvme
# driver releases the controller on unbind and takes it back on probe.
# Measured 2026-08-18 on this machine, on the empty Crucial (0000:02:00.0):
# driver_override -> nvme/unbind -> drivers_probe put it on vfio-pci, created
# /dev/vfio/15, and removed it from lsblk; the symmetric undo brought nvme0 and
# the by-id links back. Not one kernel-log line either way.
#
# So for a disk libvirt is the ONLY writer, and managed='yes' is what makes it
# the only one -- it binds at domain start and gives the controller back at
# domain stop, in the same three writes measured above. The alternative that
# was NOT taken: a boot-time rule (modprobe.d ids= or a udev driver_override)
# that keeps the disk on vfio-pci permanently. It would have to key on
# vendor:device, which names a *model* and not a drive -- on a machine whose
# boot disk is the same model it would claim the boot disk, before any of this
# tool's protections can run, and the symptom is a machine that does not boot.
# K14's hard protection can only be honest if it runs where it can read the
# host's mounts and fstab, and that is here, not in early boot.
NVME_HOSTDEV_XML = """    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source>
        <address domain='0x{domain:04x}' bus='0x{bus:02x}' \
slot='0x{slot:02x}' function='0x{function:x}'/>
      </source>
    </hostdev>
"""


def nvme_item(address: str):
    """core.inventory's verdict for one PCI address, or a refusal.

    THE VERDICT IS NOT RE-DERIVED HERE. core.inventory owns "may this device
    move and what does the host lose" for every bus, and it is the half of K14
    that was written before anything existed that could hand a disk over. A
    second table in this file would be a second answer, and the one that
    drifted would be the one nobody reads until it lets a mounted disk through.
    """
    sys.path.insert(0, str(HERE.parent))
    from core import inventory, probe
    from core import profile as profile_mod

    machine = probe.read_machine()
    p = profile_mod.select(machine.dmi_vendor, machine.dmi_product)
    items, _ = inventory.pci_items(machine, p, inventory.host_storage_claims(),
                                   probe.usb_devices())
    for item in items:
        if item.ident == address:
            return item, inventory
    die(f"{address} bu makinede bir PCI cihazı değil. "
        f"Ne var: {self_cmd('inventory')}")


def cmd_nvme(a):
    """Give a whole NVMe controller to a shut-off domain, take it back, or ask.

    WHY A CONTROLLER AND NOT A DISK IMAGE. `--disk` elsewhere in this file is a
    qcow2 file the host owns and qemu reads; this is the physical controller,
    handed over whole. The two are opposite in every way that matters, which is
    why this subcommand is `nvme` and not `disk`: the guest gets the drive with
    its own queues and its own NVMe namespace, and the host stops seeing it at
    all for as long as the domain runs.

    IT IS ALL OR NOTHING, AND THAT IS THE HARDWARE TALKING. A PCI handover moves
    the whole IOMMU group; there is no half of a controller. If the host wants
    to keep using part of that drive the answer is not this command -- it is a
    raw partition on a virtio-blk disk, with the controller left where it is.

    IT ONLY EDITS A SHUT-OFF DOMAIN, for the same reason `passthrough` does: the
    bind happens when libvirt starts the domain. Attaching to a running guest
    asks it to bind the controller there and then, while the host may still have
    the drive mounted.

    THE EDIT IS A TEXT INSERTION, not an XML round trip -- the stored XML
    carries blocks this script did not write (libosinfo metadata, the ownership
    mark, the qemu:commandline that carries Looking Glass) and re-serialising
    all of them to change one is how one of them quietly comes back different.
    """
    if not domain_exists(a.name):
        die(f"'{a.name}' tanımlı değil")

    held = sorted(c for c in pci_claims(a.name) if _is_nvme(c))
    if not a.attach and not a.detach:
        say(f"'{a.name}' aldığı NVMe denetleyicileri: "
            f"{' '.join(held) or '(yok)'}")
        for addr in held:
            say(f"  {addr} şu an host'ta: {host_driver_of(addr)}")
        say(f"Aday olanlar: {self_cmd('inventory')}")
        return 0

    state = domain_state(a.name)
    if state != "shut off":
        die(f"'{a.name}' şu an '{state}' -- denetleyici yalnızca kapalı bir "
            f"domain'e eklenir. Bağlamayı libvirt domain açılırken yapar.")

    address = (a.attach or a.detach).lower()
    address_parts(address)          # refuses a malformed address before anything

    xml = virsh("dumpxml", a.name).stdout
    blocks = [b for b in HOSTDEV_BLOCK.findall(xml) if _matches(b, address)]

    if a.detach:
        if not blocks:
            say(f"'{a.name}' zaten {address}'i almıyor -- değişiklik yok")
            return 0
        for block in blocks:
            xml = xml.replace(block, "", 1)
    else:
        item, inv = nvme_item(address)
        # The subcommand's name is a promise about what moves. Without this the
        # verdict alone would let a Wi-Fi card through `nvme --attach`, because
        # inventory rightly judges it "uyarı" and not "red" -- a true answer to
        # a question this command is not asking.
        if not _is_nvme(address):
            die(f"{address} bir NVMe denetleyicisi değil ({item.title}). "
                f"Bu komut yalnızca depolama denetleyicisi devreder.")
        say(f"{item.ids} — {item.title} (grup {item.group or '?'}, "
            f"host sürücüsü {item.driver})")
        for reason in item.reasons:
            say(f"  {reason}")
        if item.verdict == inv.REFUSE:
            die(f"{address} devredilemez -- yukarıdaki gerekçe. "
                f"Bu K14'ün sert koruması ve bayrağı yoktur.")
        if blocks:
            say(f"'{a.name}' {address}'i zaten alıyor -- değişiklik yok")
            return 0
        xml = xml.replace("  </devices>",
                          NVME_HOSTDEV_XML.format(**address_parts(address))
                          + "  </devices>", 1)

    redefine(xml)

    # Read back rather than trust the define: a lost ownership mark or a dropped
    # ivshmem block only surfaces as "Looking Glass shows nothing" much later.
    after = virsh("dumpxml", a.name).stdout
    claims = pci_claims(a.name)
    say(f"'{a.name}' aldığı PCI işlevleri: {' '.join(sorted(claims)) or '(yok)'}")
    say(f"  işaret: {'duruyor' if marker_of(a.name) else 'YOK -- kaybolmuş'}")
    say(f"  ivshmem: {'duruyor' if 'mem-path' in after else 'yok'}")
    if a.detach:
        if address in claims:
            die(f"çıkarılamadı -- domain hâlâ istiyor: {address}")
    else:
        if address not in claims:
            die(f"eklenemedi -- eksik: {address}")
        say(f"  {address} şu an host'ta: {host_driver_of(address)} "
            f"(domain kapalıyken host'undur, bağlamayı libvirt açılışta yapar)")
        say("Misafir onu boş bir disk olarak görür; bölümlemeyi misafir yapar.")
    return 0


def _is_nvme(address: str) -> bool:
    """Is that PCI function an NVMe controller on this machine right now?"""
    sys.path.insert(0, str(HERE.parent))
    from core import probe
    for device in probe.read_machine().devices:
        if device.address == address:
            return device.pci_class.startswith("0x0108")
    return False


# --------------------------------------------------------------------------- #
# lending a USB device to a running guest
# --------------------------------------------------------------------------- #

# No `managed` attribute: libvirt's own documentation says it is read for
# type='pci' and ignored everywhere else. USB devices "are detached from the
# host on guest startup and reattached after the guest exits", and that is the
# whole mechanism -- there is no vfio-pci to bind, no IOMMU group to move, and
# nothing for the handover hook to do. Measured: a domain XML carrying only a
# USB hostdev makes the hook report "no handover", because it filters on
# type='pci'.
USB_HOSTDEV_XML = """<hostdev mode='subsystem' type='usb'>
  <source>
    <vendor id='0x{vendor}'/>
    <product id='0x{product}'/>
  </source>
</hostdev>
"""

USB_IDS = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4})$")


def _core_inventory():
    """core.inventory, imported late and by path like the rest of core.

    It owns the verdict -- may this device be lent, and what does the host lose.
    A second table here would be a second answer, and the one that drifted
    would be the one nobody reads until it lets something through.
    """
    sys.path.insert(0, str(HERE.parent))
    from core import inventory
    return inventory


def _core_probe():
    sys.path.insert(0, str(HERE.parent))
    from core import probe
    return probe


def domain_usb(name: str) -> list[tuple[str, str]]:
    """(vendor, product) of every USB device the domain holds right now.

    Read from the live XML rather than remembered, because that is the only
    record there is: an attach made this way is never written to the persistent
    definition, so libvirt's running copy is the single source of truth.
    """
    r = virsh("dumpxml", name, check=False)
    if r.returncode != 0:
        return []
    try:
        root = ElementTree.fromstring(r.stdout)
    except ElementTree.ParseError:
        return []
    out = []
    for hostdev in root.findall("./devices/hostdev[@type='usb']/source"):
        vendor, product = hostdev.find("vendor"), hostdev.find("product")
        if vendor is None or product is None:
            continue
        out.append((vendor.get("id", "").removeprefix("0x").lower(),
                    product.get("id", "").removeprefix("0x").lower()))
    return out


def host_drivers_of(usb_name: str) -> list[str]:
    """Which host drivers hold that USB device's interfaces at this moment.

    THIS IS THE PROOF, AND IT IS READ ON THE HOST SIDE. A successful
    attach-device is a claim; the device leaving btusb behind is what shows it
    happened. The device itself stays in sysfs while a guest holds it -- only
    its interfaces lose their drivers -- so a check for the node's existence
    would report success either way.
    """
    probe = _core_probe()
    for device in probe.usb_devices():
        if device.name == usb_name:
            return device.drivers
    return []


def guest_pnp(name: str, vendor: str, product: str) -> list[dict]:
    """What the guest itself says it has for that vendor:product.

    The guest's own inventory is the other half of the proof: the host losing a
    device says it left, not that it arrived. Windows names USB devices by
    VID/PID in the instance id, which is the one identifier both sides share.
    """
    pattern = f"USB\\VID_{vendor.upper()}&PID_{product.upper()}*"
    return ps_json(name, "Get-PnpDevice | Where-Object { $_.InstanceId -like "
                   f"{_ps_quote(pattern)} " "} | "
                   "Select-Object Status,Problem,Class,FriendlyName,InstanceId | "
                   "ConvertTo-Json -Compress")


def cmd_usb(a):
    """Lend a USB device to a running guest, take it back, or say who has what.

    WHY THIS IS SEPARATE FROM `passthrough`, WHICH ALSO ADDS A hostdev. They
    differ in every part except the word: passthrough edits a shut-off domain's
    stored definition so the card is there at the next start, and the card is
    then moved by the hook at start time. This one attaches to a guest that is
    already running, writes nothing that survives it, and the handover is
    libvirt's own -- the hook never sees it. Folding them into one command
    would mean one flag deciding whether a domain must be running or must not
    be.

    NOTHING IS PERSISTED, DELIBERATELY. --live only, never --config: a lent
    device is meant to come back, and a Bluetooth radio silently claimed by a
    guest at every boot is a fault report six months later that nobody connects
    to this command. Shutting the guest down is therefore always a complete
    undo, which is also what makes the input-device rule in core.inventory
    enough on its own.

    THE OWNERSHIP MARK IS NOT CHECKED HERE, AND THAT IS THE POINT. guard()
    refuses destructive steps on domains this tool did not build; this step is
    not destructive, and the guest it exists for is the working one, which
    predates the mark and does not carry it (measured: `win11` has none). A
    guard that made the feature refuse its only real target would be protecting
    nothing.
    """
    inventory, probe = _core_inventory(), _core_probe()
    if not domain_exists(a.name):
        die(f"'{a.name}' tanımlı değil")

    devices = probe.usb_devices()
    held = domain_usb(a.name)

    if not (a.attach or a.detach):
        return _usb_status(a.name, devices, held, inventory, probe)

    ids = a.attach or a.detach
    m = USB_IDS.match(ids)
    if not m:
        die(f"vendor:product bekleniyordu (ör. 8087:0032), gelen: {ids}")
    vendor, product = m.group(1).lower(), m.group(2).lower()

    state = domain_state(a.name)
    if state != "running":
        die(f"'{a.name}' şu an '{state}' -- bu komut koşan bir misafire ödünç "
            f"verir. Kapalı bir domain'e kalıcı cihaz eklemek bu aracın işi değil.")

    matching = [d for d in devices if (d.vendor, d.product) == (vendor, product)]
    attached = (vendor, product) in held

    if a.detach:
        if not attached:
            die(f"'{a.name}' {vendor}:{product} aygıtını almıyor -- geri "
                f"alınacak bir şey yok")
        return _usb_move(a.name, vendor, product, matching, attach=False)

    if attached:
        say(f"'{a.name}' {vendor}:{product} aygıtını zaten alıyor -- değişiklik yok")
        return 0
    if not matching:
        die(f"host'ta {vendor}:{product} diye bir USB aygıtı yok. "
            f"Takılı olanlar: {self_cmd('guest', '--name', a.name, 'usb')}")
    if len(matching) > 1:
        # vendor:product is what libvirt matches on, so two identical devices
        # are ambiguous to it as well; it would take one of them and never say
        # which. Refused rather than guessed, because the wrong one leaving the
        # host is exactly the surprise this command is supposed to avoid.
        die(f"host'ta aynı kimliği taşıyan {len(matching)} aygıt var "
            f"({', '.join(d.name for d in matching)}) -- libvirt hangisini "
            f"alacağını vendor:product ile ayırt edemez, devir yapılmadı")

    device = matching[0]
    verdict, reasons = inventory.usb_verdict(device, devices, probe.input_devices())
    say(f"{device.ids} — {inventory._usb_title(device)} ({device.name})")
    for reason in reasons:
        say(f"  {reason}")
    if verdict == inventory.REFUSE:
        die("devredilemez -- yukarıdaki gerekçe. (Envanterin tümü: "
            f"{self_cmd('inventory')})")
    return _usb_move(a.name, vendor, product, matching, attach=True)


# Windows PnP problem codes, only the ones this path actually produces. The
# number is what the guest reports; the words are what makes it a diagnosis
# instead of something to search for.
PNP_PROBLEM = {
    0: "çalışıyor",
    10: "başlatılamıyor (Kod 10)",
    22: "devre dışı bırakılmış (Kod 22)",
    28: "sürücü kurulu değil (Kod 28)",
    43: "aygıt sorun bildirdi (Kod 43)",
}


def usb_node_owner(device) -> str | None:
    """Who owns /dev/bus/usb/BBB/DDD right now.

    THIS IS LIBVIRT'S HALF OF THE PROOF, AND IT IS THE RELIABLE HALF. libvirt
    chowns the usbfs node to the QEMU user when it lends a device and gives it
    back to root when it takes it away, so the ownership says whether libvirt
    did its job even in the cases where nothing else moves. Measured 2026-08-05
    on the Bluetooth radio: the node changed hands both ways while the host
    driver never let go once.
    """
    if device is None or device.busnum is None or device.devnum is None:
        return None
    node = Path(f"/dev/bus/usb/{device.busnum:03d}/{device.devnum:03d}")
    try:
        import pwd
        return pwd.getpwuid(node.stat().st_uid).pw_name
    except (OSError, KeyError):
        return None


def _usb_move(name: str, vendor: str, product: str,
              matching: list, attach: bool) -> int:
    """attach-device / detach-device, then read back both sides.

    WHAT IS AND IS NOT A CRITERION HERE. The first version of this waited for
    the host driver to disappear and called that success. It is not: measured
    on this machine's Bluetooth radio, libvirt handed the device over, the
    guest enumerated it by name, and btusb stayed bound the whole time -- two
    owners, and the guest stuck at Code 10. So the host driver is now reported
    as a diagnosis rather than tested as a verdict, and the criteria are the
    two that answer the question: libvirt's own record (the live XML and the
    node it chowns) and the guest's own opinion of the device.
    """
    device = matching[0] if matching else None
    usb_name = device.name if device else None
    before = host_drivers_of(usb_name) if usb_name else []

    xml = USB_HOSTDEV_XML.format(vendor=vendor, product=product)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
        fh.write(xml)
        tmp = fh.name
    try:
        verb = "attach-device" if attach else "detach-device"
        r = virsh(verb, name, tmp, "--live", check=False)
        if r.returncode != 0:
            die(f"libvirt {verb} reddetti: {r.stderr.strip() or r.stdout.strip()}")
    finally:
        os.unlink(tmp)

    say(f"libvirt: {'takıldı' if attach else 'geri alındı'} ({vendor}:{product})")

    held = (vendor, product) in domain_usb(name)
    say(f"  domain'in canlı XML'i: {'alıyor' if held else 'almıyor'}")
    say(f"  aygıt düğümünün sahibi: {usb_node_owner(device) or '(okunamadı)'}")

    if attach and not held:
        die("cihaz eklenemedi -- domain XML'inde görünmüyor")
    if not attach and held:
        die("cihaz geri alınamadı -- domain hâlâ istiyor")

    if not attach:
        # Coming back takes a moment: the host has to re-probe the device
        # before its driver is bound again. Polled rather than slept, and a
        # driver that never returns is said out loud instead of assumed.
        deadline = time.time() + 15
        while time.time() < deadline and not host_drivers_of(usb_name):
            time.sleep(0.5)
        after = host_drivers_of(usb_name)
        say(f"  host tarafı sürücüler: {', '.join(after) or '(yok -- geri gelmedi)'}")
        return 0

    # The guest needs seconds to enumerate and try to start the device, so the
    # wait is for it to say something rather than for a fixed interval.
    deadline = time.time() + 60
    rows: list[dict] = []
    while time.time() < deadline:
        rows = guest_pnp(name, vendor, product)
        if rows:
            break
        time.sleep(2)

    after = host_drivers_of(usb_name)
    say(f"  host tarafı sürücüler: {', '.join(after) or '(yok -- misafirde)'}"
        f"{'' if after == before else f' (önce: {", ".join(before) or "(yok)"})'}")

    if not rows:
        say("  misafirde: aygıt görünmedi ya da ajan cevap vermedi "
            "-- kanıt alınamadı, iddia edilmiyor")
        return 0

    started = False
    for row in rows:
        problem = row.get("Problem")
        started = started or (row.get("Status") == "OK" and problem in (0, None))
        say(f"  misafirde: {row.get('FriendlyName') or '(adsız)'} "
            f"[{row.get('Class') or '?'}] {row.get('Status')} — "
            f"{PNP_PROBLEM.get(problem, f'sorun kodu {problem}')}")

    if not started and after:
        # Both sides holding the same device is the failure this command can
        # actually diagnose, and it looks like nothing at all from the host.
        say("  UYARI: aygıt misafirde ama başlamadı ve host sürücüsü de hâlâ "
            f"bağlı ({', '.join(after)}) -- iki sahip. Host sürücüsü aygıtı "
            "bırakmadıkça misafir onu başlatamaz.")
    return 0


def _usb_status(name: str, devices: list, held: list, inventory, probe) -> int:
    """Who has what, and what may still be lent."""
    state = domain_state(name)
    print(f"Domain   : {name} ({state})")
    print(f"Ödünç    : {len(held)} aygıt misafirde")
    print()
    inputs = probe.input_devices()
    for device in devices:
        verdict, reasons = inventory.usb_verdict(device, devices, inputs)
        mark, colour = inventory.MARKS[verdict]
        where = "MİSAFİRDE" if (device.vendor, device.product) in held else "host"
        print(f"  {inventory._paint(mark, colour)} {device.ids}  {device.name:<6} "
              f"{where:<10} {inventory._usb_title(device)}")
        for reason in reasons:
            print(f"      {reason}")
    for vendor, product in held:
        if not any((d.vendor, d.product) == (vendor, product) for d in devices):
            # A guest can hold a device the host no longer sees -- it was
            # unplugged while lent. Said out loud, because the domain will keep
            # asking for it and the next attach of the same ids would collide.
            print(f"  ? {vendor}:{product}  (host'ta artık yok, misafir hâlâ istiyor)")
    print()
    print(f"Ödünç ver : {self_cmd('guest', '--name', name, 'usb')} "
          f"--attach <vendor:product>")
    print(f"Geri al   : {self_cmd('guest', '--name', name, 'usb')} "
          f"--detach <vendor:product>")
    print("Misafir kapanınca libvirt hepsini kendiliğinden host'a geri verir.")
    return 0


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


def registry_value(name: str, key: str, value: str) -> str | None:
    code, out, _ = guest_exec(name, "reg.exe", ["query", key, "/v", value])
    if code != 0:
        return None
    m = re.search(rf"^\s*{re.escape(value)}\s+REG_\w+\s+(.*)$",
                  out, re.MULTILINE)
    return m.group(1).strip() if m else None


def repair_autologon(name: str) -> bool:
    """Put back the autologon OOBE's cleanup removed, without a password.

    MEASURED 2026-08-05, and it is the same cleanup rearm_autologon was written
    for -- it simply runs again, later, at the next logoff. The signature is
    unmistakable and is exactly what was found here: AutoAdminLogon and
    DefaultUserName gone, DefaultPassword still sitting in the registry in clear
    text, guest on the lock screen.

    That residue is what makes this repair possible without the caller holding
    credentials: the password is already there, and the account name survives in
    LastUsedUsername. So `setup` can put a console session back on its own
    rather than demanding the password file a second time -- which matters,
    because the session can disappear between two steps of the same run.
    """
    user = (registry_value(name, WINLOGON, "DefaultUserName")
            or registry_value(name, WINLOGON, "LastUsedUsername"))
    if not user:
        say("  autologon onarılamadı: kullanıcı adı kayıtta yok")
        return False
    if registry_value(name, WINLOGON, "DefaultPassword") is None:
        say("  autologon onarılamadı: kayıtta parola yok, --password-file gerekir")
        return False
    for extra in (["/v", "AutoAdminLogon", "/t", "REG_SZ", "/d", "1", "/f"],
                  ["/v", "DefaultUserName", "/t", "REG_SZ", "/d", user, "/f"]):
        code, _, err = guest_exec(name, "reg.exe", ["add", WINLOGON, *extra])
        if code != 0:
            say(f"  autologon onarılamadı ({extra[1]}): {err.strip()}")
            return False
    guest_exec(name, "reg.exe", ["delete", WINLOGON, "/v", "AutoLogonCount", "/f"])
    say(f"  autologon geri yazıldı ({user})")
    return True


def ensure_console_session(name: str, workdir: Path) -> bool:
    """Make sure somebody is logged in at the console, repairing it if not.

    THE SESSION IS NOT A PRECONDITION THAT HOLDS ONCE. It held at the start of
    this round and was gone ninety seconds later: installing the display driver
    logged the user off (Winlogon 7002, no reboot -- the boot time never moved),
    and OOBE's cleanup took the autologon values with it. So the check belongs
    immediately before the step that needs a session, not once at the top, and
    it has to be able to put one back.
    """
    users = console_users(name)
    if users:
        return True
    say("konsol oturumu yok -- autologon onarılıp yeniden başlatılacak")
    if not repair_autologon(name):
        return False
    if not reboot_and_wait(name, workdir, 10):
        return False
    deadline = time.time() + 300
    while time.time() < deadline:
        users = console_users(name)
        if users:
            say(f"konsol oturumu geri geldi ({', '.join(users)})")
            return True
        time.sleep(5)
    say("konsol oturumu yeniden başlatmadan sonra da açılmadı")
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
# the guest-side setup: a chain of scripts, pushed and driven
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


def display_devices(name: str) -> list[dict]:
    """Every display-class PnP device the guest has, working or not.

    Win32_VideoController only lists adapters that have a display driver bound,
    so a passed-through card whose driver the guest does not have is INVISIBLE
    there -- it sits in this list instead, with a problem code. Printed before
    the round starts choosing anything, because "the card is not in the guest"
    and "the card is in the guest with no driver" need different answers and
    look identical from the adapter list.
    """
    return ps_json(name, "Get-PnpDevice -Class Display | Select-Object "
                         "Status,Problem,FriendlyName,InstanceId | "
                         "ConvertTo-Json -Compress")


def monitors(name: str) -> list[dict]:
    return ps_json(name, "Get-PnpDevice -Class Monitor -PresentOnly | "
                         "Select-Object Status,FriendlyName,InstanceId | "
                         "ConvertTo-Json -Compress")


def lg_service(name: str) -> list[dict]:
    return ps_json(name, "Get-Service | Where-Object { $_.DisplayName -like "
                         "'*Looking Glass*' } | Select-Object Name,Status | "
                         "ConvertTo-Json -Compress")


def display_modes(name: str) -> list[dict]:
    """Every adapter with the mode it is driving, or nulls when it drives none.

    The resolution is the acceptance number for the card-side round (the shared
    memory window is sized for it on the host), and an adapter that is present
    but attached to nothing answers with nulls rather than with an error --
    which is itself the reading that says the topology step did not take.
    """
    return ps_json(name, "Get-CimInstance Win32_VideoController | Select-Object "
                         "Name,CurrentHorizontalResolution,"
                         "CurrentVerticalResolution,CurrentRefreshRate | "
                         "ConvertTo-Json -Compress")


# NOT NEXT TO THE BINARY, whatever the installer's own instructions suggest:
# measured 2026-08-05, the host writes under ProgramData and rotates -- .1, .2,
# .3 -- one file per process. Under the service that is one file per capture
# attempt, so the unsuffixed name is the newest attempt and the only one worth
# reading. looking-glass.ps1 pointed at Program Files and printed "no log yet"
# for a file that existed all along, which is why this constant is here.
LG_HOST_LOG = r"C:\ProgramData\Looking Glass (host)\looking-glass-host.txt"

# What the host says on its way to capturing, or failing to: which backend it
# tried, which adapters it rejected, and whether it gave up.
LG_LOG_SIGNALS = re.compile(
    r"enumerateDevices|dxgi_init|captureStart|Trying |adapter|IVSHMEM|exited",
    re.IGNORECASE,
)
LG_CAPTURE_FAILED = re.compile(
    r"Failed to (initialize the capture device|find a supported capture "
    r"interface|locate a valid output device)",
    re.IGNORECASE,
)


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _core_lg():
    """core.lookingglass, imported the way core.doctor is: late and by path.

    Both halves of the version question have one owner and it lives in core/,
    because doctor asks it too and a second copy here would be a second answer.
    """
    sys.path.insert(0, str(HERE.parent))
    from core import lookingglass
    return lookingglass


def _core_hostfiles():
    """core.hostfiles, imported the same way, and for the same reason.

    It already owns how a display is measured and how big a frame of that size
    needs its shared window to be -- install sizes kvmfr with exactly these two
    functions. Asking them here rather than restating them is what keeps the
    guest's resolution and the host's window from drifting apart.
    """
    sys.path.insert(0, str(HERE.parent))
    from core import hostfiles
    return hostfiles


def lg_host_log(name: str) -> list[str] | None:
    """The guest's own Looking Glass host log, or None when there is none.

    Read in one round trip and handed to the callers that want different
    sentences out of it -- which release is running (its first lines) and
    whether capture started (its last). Asking the guest twice for the same
    file would let the two answers come from two different processes, since
    the host rotates the file per capture attempt.
    """
    code, out, _ = powershell(name, ["-Command",
        f"$p={_ps_quote(LG_HOST_LOG)}; if (Test-Path -LiteralPath $p) "
        f"{{ Get-Content -LiteralPath $p }} else {{ 'NOLOG' }}"], timeout=120)
    if code != 0 or out.strip() in ("", "NOLOG"):
        return None
    return out.splitlines()


def lg_capture_device(name: str, gpu: str,
                      log: list[str] | None = None) -> tuple[list[str], bool | None]:
    """Is Looking Glass really capturing, and on the adapter VDD renders on?

    THIS IS THE EVIDENCE HALF OF THE CARD-SIDE ROUND, and the measurement that
    showed the earlier ones were not enough: a service reading `Running` is a
    service that keeps RESTARTING a host process which exits immediately, and
    from outside the two are identical. Measured on the cardless rehearsal --
    "Not using unsupported adapter: Microsoft Basic Render Driver" three times,
    then "Failed to find a supported capture interface", then "Host application
    exited", with the service still Running and every other check green.

    The other half of the question is which adapter it settled on: everything
    can be true while the captured desktop lives on the emulated adapter, and
    that reaches the operator as "the client connects and no frames arrive".

    Returns (lines worth printing, verdict): False when the log says capture
    failed, True when it names the adapter we chose and does not, None when the
    log cannot be read or says neither -- unknown is reported as unknown rather
    than counted as a pass.
    """
    if log is None:
        log = lg_host_log(name)
    if log is None:
        return [], None

    lines = [line.strip() for line in log if LG_LOG_SIGNALS.search(line)]
    if any(LG_CAPTURE_FAILED.search(line) for line in log):
        return lines, False
    if any(gpu.lower() in line.lower() for line in log):
        return lines, True
    return lines, None


def choose_gpu(name: str, expect_card: bool = False) -> str | None:
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

    expect_card TURNS THE REHEARSAL BRANCH INTO A REFUSAL, and it is set when
    the domain claims a PCI function. "The card was handed over and the guest
    shows no card" is a contradiction, not a rehearsal: the usual cause is that
    the guest has no driver for it, which leaves the device out of the adapter
    list entirely (see display_devices). Falling back to the one remaining
    adapter there would render the indirect display on the emulated one and
    pass every later check -- the exact silent failure this function exists for.
    """
    adapters = display_adapters(name)
    if not adapters:
        say("  ekran bağdaştırıcıları okunamadı")
        return None
    for a in adapters:
        say(f"  bağdaştırıcı: {a.get('Name')}  [{a.get('PNPDeviceID')}]")

    # VDD's own adapter is discounted first, and that is what makes a second run
    # behave like the first: after step 1 the guest has one more display adapter
    # than it started with, and counting it would leave the round unable to
    # answer a question it answered ten minutes earlier.
    real = [a for a in adapters
            if not (a.get("PNPDeviceID") or "").upper().startswith("ROOT\\")]
    candidates = [a for a in real
                  if QXL_HWID not in (a.get("PNPDeviceID") or "").upper()]
    if len(candidates) == 1:
        chosen = candidates[0].get("Name")
        # A UNIQUE DEVICE IS NOT A UNIQUE NAME, and the name is all VDD gets.
        # Measured: before its driver is installed the card calls itself
        # "Microsoft Basic Display Adapter" -- the same string the emulated
        # adapter carries -- so the settings file would match both and the
        # indirect display could render on either. Refusing here is the whole
        # point of the function; passing the name on would be the silent
        # failure it exists to prevent.
        twins = [a for a in adapters
                 if a is not candidates[0] and a.get("Name") == chosen]
        if twins:
            say(f"  seçilen bağdaştırıcının adı ('{chosen}') benzersiz değil -- "
                f"{len(twins)} bağdaştırıcı daha aynı adı taşıyor")
            say("  VDD adla eşleştirir; bu ad yanlış olana da uyar. Kartın "
                "sürücüsü kurulu mu?")
            return None
        return chosen
    if not candidates:
        if expect_card:
            say("  domain kartı alıyor ama misafirde kart görünmüyor -- bu bir "
                "prova değil, çelişki")
            say("  muhtemel sebep: misafirde kartın sürücüsü yok, cihaz "
                "bağdaştırıcı listesine hiç girmiyor (yukarıdaki PnP dökümü)")
            return None
        if len(real) == 1:
            say("  devredilmiş kart yok -- kartsız prova, tek bağdaştırıcı seçiliyor")
            return real[0].get("Name")
        say("  devredilmiş kart yok ve birden çok bağdaştırıcı var")
        return None
    say(f"  {len(candidates)} aday var -- tahmin edilmez, --gpu-name ile ver")
    return None


def vdd_mode() -> tuple[int, int] | None:
    """The resolution the guest's virtual display gets -- measured, not written down.

    IT USED TO BE A CONSTANT in vdd.ps1's parameters while the two other places
    that depend on the same number were already measured: install sizes the
    kvmfr window from the largest connected display, and the domain's ivshmem
    size is read back off the host's modprobe.d. A machine-specific value spelled
    out in the repo is what K9 keeps out, and this was the only one of the three
    that could go stale on its own -- silently, because a guest at the wrong
    resolution still boots.

    THE INSTALLED WINDOW BINDS, NOT THE PANEL. The client draws the guest into a
    window on this machine, so a guest bigger than the largest local display buys
    nothing; but a guest whose frame does not fit the kvmfr window that is
    installed *today* is worse than nothing -- the client attaches and no picture
    ever arrives, which is indistinguishable from a handover that did not work.
    That window can be smaller than what install would size now: kvmfr takes its
    size at boot, so a display plugged in afterwards moves largest_mode() and
    not the window. So the mode is the biggest connected one that still fits.

    None means "say nothing, let the script's own defaults stand": on a host with
    nothing connected there is no display to measure, and failing there would
    break a setup run that has no reason to fail.
    """
    hostfiles = _core_hostfiles()
    modes = hostfiles.connected_modes()
    if not modes:
        say("  bağlı ekran yok -- VDD çözünürlüğü betiğin varsayılanında kalıyor")
        return None

    window = kvmfr_size_bytes()
    if window is None:
        largest = max(modes, key=lambda mode: mode[0] * mode[1])
        say(f"  {KVMFR_MODPROBE} okunamadı -- {largest[0]}x{largest[1]} veriliyor, "
            "pencereye sığdığı doğrulanmadan")
        return largest

    window_mb = window // (1024 * 1024)
    fits = [mode for mode in modes
            if hostfiles.required_mb(*mode) <= window_mb]
    if not fits:
        smallest = min(modes, key=lambda mode: mode[0] * mode[1])
        say(f"  bağlı ekranların en küçüğü bile ({smallest[0]}x{smallest[1]}) "
            f"{hostfiles.required_mb(*smallest)} MB pencere istiyor, kurulu "
            f"pencere {window_mb} MB")
        say("  VDD çözünürlüğü betiğin varsayılanında kalıyor -- pencereyi "
            f"büyütmek: {self_cmd('install')}, sonra reboot (kvmfr boot'ta "
            "yükleniyor)")
        return None

    chosen = max(fits, key=lambda mode: mode[0] * mode[1])
    say(f"  VDD çözünürlüğü: {chosen[0]}x{chosen[1]} "
        f"({hostfiles.required_mb(*chosen)} MB, kurulu pencere {window_mb} MB)")
    return chosen


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


def confirm_plain_vt(a, name: str, claims: set[str]) -> bool:
    """Refuse to take the desktop's card away from inside the desktop, unasked.

    Same guard as `vfioctl selftest`, and the same reason: if the handover goes
    wrong the graphics session is what dies, and a terminal inside it dies with
    it -- taking the output of the round that would have explained why. It only
    asks when the domain actually claims a PCI function, because the cardless
    rehearsal is meant to be run from the desktop as often as debugging needs.

    A MISSING TTY IS A REFUSAL, NOT A YES. Both the "no terminal" and the
    "cannot read the terminal's name" paths used to return True, so a caller
    with no pty -- a script, a job runner, an agent -- got the card handed over
    with nobody asked, and the line that looked like a guard was the line that
    waved it through. --yes is how consent is given; the absence of somebody to
    ask is not consent.
    """
    if getattr(a, "yes", False):
        return True

    # This invocation plus --yes, not a rebuilt one: a hint that named the
    # subcommand from constants would drop --gpu-name, --timeout and every
    # other flag the caller actually typed, and re-running it would do a
    # different thing than the one just refused.
    deliberate = self_cmd(*sys.argv[1:], "--yes")
    if not sys.stdin.isatty():
        say(f"'{name}' kartı istiyor ({' '.join(sorted(claims))}) ama bu "
            "çağrının tty'si yok, yani düz VT sorusu sorulamıyor.")
        say(f"Bilerek isteniyorsa: {deliberate}")
        return False
    try:
        tty = os.ttyname(sys.stdin.fileno())
    except OSError as exc:
        say(f"Bu kabuğun tty adı okunamadı ({exc.strerror}), düz VT olup "
            "olmadığı ölçülemiyor.")
        say(f"Bilerek isteniyorsa: {deliberate}")
        return False
    if not tty.startswith("/dev/pts/") or os.environ.get("SSH_CONNECTION"):
        return True
    print(f"'{name}' kartı istiyor ({' '.join(sorted(claims))}) ve bu kabuk "
          "grafik oturumunun içindeki bir terminale benziyor.")
    print("Devir sırasında oturum ölürse bu kabuk da onunla ölür; düz bir VT "
          "(Ctrl+Alt+F3) doğru yerdir.")
    return input("Yine de devam? [e/H] ").strip().lower() in ("e", "y")


def start_for_setup(a, name: str, workdir: Path) -> bool:
    """Bring the guest up for a round, and report what the card did.

    The driver each claimed function ends up on is read from sysfs before and
    after: with managed='no' a domain whose card is still held by the host
    driver does not start at all, so `running` plus `vfio-pci` is the handover's
    receipt. Nothing here moves the card -- the hook does, at domain start.
    """
    state = domain_state(name)
    if state == "running":
        say(f"'{name}' zaten çalışıyor")
        return True
    if state != "shut off":
        say(f"'{name}' '{state}' -- başlatılamaz")
        return False

    claims = pci_claims(name)
    if claims and not confirm_plain_vt(a, name, claims):
        return False
    guard_exclusive_devices(name)
    for addr in sorted(claims):
        say(f"başlamadan önce {addr} -> {host_driver_of(addr)}")

    virsh("start", name, capture=False)
    for addr in sorted(claims):
        driver = host_driver_of(addr)
        say(f"başladıktan sonra {addr} -> {driver}"
            + ("" if driver == "vfio-pci" else "   <-- beklenen vfio-pci"))
    return wait_for_agent(name, workdir, a.timeout)


def guest_setup(a, name: str) -> int:
    """Drive the guest-side scripts in order, stopping at the first failure.

    THE ORDER IS A DEPENDENCY, NOT A PREFERENCE. display-layout has nothing to
    isolate until VDD's screen exists, and Looking Glass captures whatever the
    desktop is at the time. So the round does not walk the list -- it checks
    that each step produced the thing the next one needs, and the check is the
    guest's own inventory rather than the installer's exit code.

    The one retry is deliberate and bounded: an indirect display driver that has
    installed cleanly sometimes needs the boot to enumerate, so a missing
    monitor is worth exactly one reboot before it counts as a failure.

    AND THE CONSOLE SESSION IS CHECKED TWICE, not once. Installing the display
    driver logs the console user off -- measured, and it takes the autologon
    values with it -- so a session that was there when the round started can be
    gone by the time the step that needs it runs.
    """
    sys.stdout = Tee(SETUP_LOG)  # type: ignore[assignment]
    workdir = IMAGES / f"{name}-unattend"
    workdir.mkdir(parents=True, exist_ok=True)

    print()
    say(f"===== setup {name}   günlük: {SETUP_LOG}")
    if getattr(a, "start", False) and not start_for_setup(a, name, workdir):
        say("TUR DÜŞTÜ -- misafir sürülebilir hâle gelmedi.")
        return 1

    if not agent_ping(name):
        die(f"'{name}' ajanı yanıt vermiyor -- önce build (ya da setup --start)")

    if not ensure_console_session(name, workdir):
        die("misafirde konsol oturumu açılamadı -- display-layout ekrana "
            f"ulaşamaz. Elle: {self_cmd('guest', '--name', name, 'autologon')}")

    # mkdir exits 1 on a directory that already exists, which is the state we
    # want; only silence from the agent is a failure worth stopping for.
    code, _, _ = guest_exec(name, "cmd.exe", ["/c", "mkdir", GUEST_DIR])
    if code is None:
        die(f"{GUEST_DIR} oluşturulamadı -- ajan cevap vermedi")

    # With a card in the domain the guest's own view of it is worth printing
    # before anything is chosen: a device sitting there with a problem code is a
    # different story from one that never arrived, and neither shows up in the
    # adapter list the choice is made from.
    claims = pci_claims(name)
    if claims:
        say(f"domain'in aldığı PCI işlevleri: {' '.join(sorted(claims))}")
        for device in display_devices(name):
            say(f"  PnP: {device.get('Status')} problem={device.get('Problem')} "
                f"{device.get('FriendlyName')} [{device.get('InstanceId')}]")

        # 0. the card's own driver, and only when there is a card. A handed-over
        # GPU running on Windows' inbox display driver is not a usable adapter
        # (problem=10, measured), and it does not even carry a distinguishable
        # name yet -- so this has to come before the adapter is chosen, not
        # after. There is nothing to install on the cardless rehearsal.
        if not run_guest_script(name, WINDOWS / "nvidia.ps1", [], timeout=2400):
            say("ADIM DÜŞTÜ -- kartın sürücüsü kurulamadı.")
            return 1
        stuck = [d for d in display_devices(name)
                 if (d.get("InstanceId") or "").upper().startswith("PCI\\")
                 and QXL_HWID not in (d.get("InstanceId") or "").upper()
                 and d.get("Problem") not in (0, None)]
        for device in stuck:
            say(f"  PnP: {device.get('Status')} problem={device.get('Problem')} "
                f"{device.get('FriendlyName')} [{device.get('InstanceId')}]")
        if stuck:
            say("ADIM DÜŞTÜ -- devredilen ekran cihazı sürücüden sonra da "
                "çalışmıyor.")
            return 1

    gpu = a.gpu_name or choose_gpu(name, expect_card=bool(claims))
    if not gpu:
        die("VDD'nin render edeceği bağdaştırıcı belirlenemedi -- --gpu-name ver")
    say(f"VDD bağdaştırıcısı: {gpu}")

    # 1. VDD
    vdd_args = ["-GpuName", gpu]
    wanted = vdd_mode()
    if wanted:
        vdd_args += ["-Width", str(wanted[0]), "-Height", str(wanted[1])]
    if not run_guest_script(name, WINDOWS / "vdd.ps1", vdd_args, timeout=900):
        return 1
    monitor = vdd_monitor(name)
    if monitor is None:
        say("VDD ekranı görünmüyor -- bir kez yeniden başlatılıp tekrar bakılacak")
        if not reboot_and_wait(name, workdir, 10):
            say("ADIM DÜŞTÜ -- yeniden başlatma doğrulanamadı.")
            return 1
        monitor = vdd_monitor(name)
    if monitor is None:
        say("ADIM DÜŞTÜ -- VDD ekranı yeniden başlatmadan sonra da yok.")
        return 1
    say(f"VDD ekranı: {monitor.get('Status')} {monitor.get('FriendlyName')}")

    # 2. Looking Glass host -- THE RELEASE IS SETTLED BEFORE THE INSTALLER RUNS.
    # The two halves must be the same release or the client refuses the shared
    # memory segment, and that refusal reaches the operator as "no picture",
    # which is exactly what a handover that did not work looks like. Installing
    # a host the client will refuse costs a download and buys a green line that
    # is wrong. An unreadable version is reported, not treated as a mismatch:
    # on a machine whose client did not come from a package there is nothing to
    # compare, and refusing there would make the tool unusable off Arch.
    lg = _core_lg()
    client, pin = lg.client_release(), lg.read_pin()
    if pin.release and not pin.coherent:
        say(f"ADIM DÜŞTÜ -- {lg.PS1.name} pin'i kendi içinde tutarsız: "
            f"$Version = {pin.release}, $Url = {pin.url or '(yok)'}. "
            "Kurulacak sürüm ile söylenen sürüm ayrı.")
        return 1
    if lg.compare(client.release, pin.release) is False:
        say(f"ADIM DÜŞTÜ -- Looking Glass sürümleri ayrı: bu makinedeki istemci "
            f"{client.release}, kurulacak misafir tarafı {pin.release}.")
        say("  İki yarı aynı release olmak zorunda; ayrıysa istemci paylaşımlı "
            "belleği reddeder ve arıza 'görüntü hiç gelmiyor' diye görünür.")
        for line in lg.remedy(client.release, None).splitlines():
            say(f"  {line}")
        return 1
    if client.release and pin.release:
        say(f"LG sürümü: istemci {client.release} = kurulacak misafir tarafı "
            f"{pin.release}")
    else:
        say(f"LG sürümü: eşleşme doğrulanmadı -- istemci: {client.detail}; "
            f"pin: {pin.detail}")

    if not run_guest_script(name, WINDOWS / "looking-glass.ps1", [], timeout=600):
        return 1
    services = lg_service(name)
    running = [s for s in services if str(s.get("Status")) in ("4", "Running")]
    if not running:
        say(f"ADIM DÜŞTÜ -- LG host servisi çalışmıyor ({services or 'servis yok'}).")
        return 1
    say(f"LG host servisi: {running[0].get('Name')} çalışıyor")

    # 3. one display -- and the session is re-checked here rather than trusted
    # from the top of the round, because installing the driver above is what
    # took the last one away.
    if not ensure_console_session(name, workdir):
        say("ADIM DÜŞTÜ -- ekran topolojisi için konsol oturumu yok.")
        return 1
    if not run_guest_script(name, WINDOWS / "display-layout.ps1", [], timeout=600):
        say("ADIM DÜŞTÜ -- ekran topolojisi kurulamadı.")
        say(f"  geri alma: misafirde {GUEST_DIR}\\display-layout.ps1 -Reattach")
        return 1

    # 4. what the round produced, read back once more. The three steps above each
    # proved their own half; these two lines are the sentence the whole round was
    # for -- at what resolution, and on which card.
    print()
    say("Sonuç ölçümü")
    driving = display_modes(name)
    for mode in driving:
        width = mode.get("CurrentHorizontalResolution")
        pixels = (f"{width}x{mode.get('CurrentVerticalResolution')} @ "
                  f"{mode.get('CurrentRefreshRate')} Hz" if width
                  else "mod yok (hiçbir ekranı sürmüyor)")
        say(f"  {mode.get('Name')}: {pixels}")

    # THE NUMBER WAS ALREADY READ HERE AND CALLED THE ACCEPTANCE NUMBER (see
    # display_modes) -- it simply was not compared to the one that was asked for.
    # Printing it makes a reader responsible for noticing; comparing it makes the
    # tool responsible, and this is the number the host sized its shared window
    # for. A guest driving MORE pixels than the window holds is the failure this
    # whole path exists to prevent, and it reaches the operator as "no picture".
    #
    # WHY A MISMATCH IS POSSIBLE AT ALL, and what is not known about it: vdd.ps1
    # writes vdd_settings.xml and then installs the driver, so a first run cannot
    # disagree with itself. A LATER run with a different mode -- a second machine,
    # a panel swap -- rewrites the file against a driver that is already there,
    # and whether VDD picks that up without a restart HAS NOT BEEN MEASURED. It
    # has never had to: on this machine the measured mode equals the one the
    # guest already runs (verified against the live guest, 2560x1440). So the
    # check exists to make that day loud rather than to predict it.
    if wanted and not any(
            (mode.get("CurrentHorizontalResolution"),
             mode.get("CurrentVerticalResolution")) == wanted
            for mode in driving):
        say(f"ADIM DÜŞTÜ -- istenen {wanted[0]}x{wanted[1]} hiçbir "
            "bağdaştırıcıda yürürlükte değil (yukarıdaki ölçüm).")
        say("  Bu sayı kvmfr penceresinin boyutlandırıldığı sayı; misafir daha "
            "büyük bir modu sürüyorsa kare pencereye sığmaz ve istemci bağlanıp "
            "hiç kare almaz.")
        say("  Muhtemel sebep ÖLÇÜLMEDİ: VDD ayar dosyasını sürücü kurulumunda "
            "okuyor olabilir, yani değişen bir mod yeniden başlatma isteyebilir. "
            f"Denenecek: virsh -c {URI} reboot {name}, sonra setup tekrar.")
        return 1

    log = lg_host_log(name)
    lines, captures = lg_capture_device(name, gpu, log=log)
    for line in lines[-10:]:
        print(f"    | {line}")

    # What the guest ended up RUNNING, which is not what the pin said it would
    # install until the installer has actually run: an install that silently
    # fell back to an older host would otherwise pass every line above.
    installed = lg.release_from_log(log or [])
    if installed and lg.compare(installed, client.release) is False:
        say(f"  LG sürümü: misafirde {installed}, istemci {client.release} -- "
            "EŞLEŞMİYOR, istemci paylaşımlı belleği reddedecek")
    elif installed:
        say(f"  LG sürümü: misafirde {installed}"
            + (f" = istemci {client.release}" if client.release else
               " (istemci tarafı okunamadı)"))
    if captures is None:
        say("  LG yakalama: BİLİNMİYOR -- günlük okunamadı ya da hangi "
            "bağdaştırıcıyı seçtiğini yazmıyor")
    elif captures:
        say(f"  LG yakalama: '{gpu}' -- VDD'ye verilen bağdaştırıcı")
    elif not claims:
        # Measured, and it is the hardware's answer rather than a defect: with
        # no real GPU in the guest neither D12 nor DXGI finds a supported
        # adapter, so the host exits and the service restarts it forever. The
        # rehearsal is about the scripts running in order; making its
        # normal outcome a red line would teach the reader to skip the line.
        say("  LG yakalama: YOK -- kartsız provada beklenen. Gerçek bir "
            "bağdaştırıcı olmadan D12 de DXGI de yakalayamıyor; servis "
            "'Running' görünse de host süreci her seferinde çıkıyor.")
    else:
        say("ADIM DÜŞTÜ -- LG host yakalamayı başlatamadı (günlük yukarıda). "
            "Servisin 'Running' olması yalnızca onu yeniden başlattığını "
            "söyler; istemci bağlanır ve hiç kare gelmez.")
        return 1

    say("KURULUM GEÇTİ -- VDD ekranı var, LG host çalışıyor, misafir tek ekranda.")
    if pci_claims(name):
        say(f"  kapatmak: virsh -c {URI} shutdown {name}  (kart o zaman geri döner)")
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
            die(f"'{a.name}' zaten tanımlı. Önce: {self_cmd('guest', 'clean')}")
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
    qemu_ns, ivshmem = ivshmem_parts()
    dom_xml = render(TEMPLATES / "domain.xml", {
        "QEMU_NS": qemu_ns,
        "IVSHMEM": ivshmem,
        "NAME": a.name,
        "MEMORY_KIB": a.memory * 1024,
        "VCPU": a.vcpu,
        "CORES": a.vcpu // 2,
        "DISK": disk,
        "WINISO": win_iso,
        "VIRTIOISO": virtio_iso,
        "UNATTENDISO": unattend_iso,
    })
    redefine(dom_xml)
    say(f"domain tanımlandı: {a.name}")

    # 5. run it
    guard_exclusive_devices(a.name)
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
        say(f"  elle: {self_cmd('guest', '--name', a.name, 'autologon')}")
        return 1

    say("TUR GEÇTİ -- ajan ulaşılabilir, konsol oturumu açık, autologon kalıcı.")

    # THE GUEST-SIDE SETUP IS OPT-IN, and the reason is the domain this round
    # builds: it carries no ivshmem device, so looking-glass.ps1 refuses at its
    # own precondition and the round would end in a failure that means nothing.
    # A tool whose normal outcome is a red line teaches people to skip reading
    # the line. `setup` runs it whenever the guest is ready for it.
    if not a.setup:
        say(f"  sıradaki: {self_cmd('guest', '--name', a.name, 'setup')}")
        return 0
    print()
    return guest_setup(a, a.name)


def cmd_status(a):
    """Where the guest is now -- and, when it can be asked, which Looking Glass.

    THE VERSION PAIR IS HERE BECAUSE THIS IS WHERE "no picture" IS DIAGNOSED.
    A client upgraded past the host in the guest produces a black window and no
    error anywhere else in the stack; without this line the next step is taking
    the working parts apart. It is only asked of a running guest, since the
    reading comes from the host application's own log.
    """
    print(f"domain : {a.name} -> {domain_state(a.name)}")
    if domain_exists(a.name):
        alive = agent_ping(a.name)
        print(f"ajan   : {'yanıt veriyor' if alive else 'yok'}")
        r = virsh("domifaddr", a.name, "--source", "agent", check=False)
        if r.returncode == 0 and r.stdout.strip():
            print(r.stdout.strip())

        lg = _core_lg()
        client = lg.client_release()
        installed = lg.release_from_log(lg_host_log(a.name) or []) if alive else None
        agrees = lg.compare(installed, client.release)
        print(f"LG     : istemci {client.release or '?'} | misafir "
              f"{installed or '?'}"
              + ("  -- EŞLEŞMİYOR" if agrees is False else
                 "  (eşleşiyor)" if agrees else "  (karşılaştırılamadı)"))
        if agrees is False:
            for line in lg.remedy(client.release, installed).splitlines():
                print(f"         {line}")
        elif agrees is None and client.release is None:
            print(f"         {client.detail}")
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
    say(f"  doğrulama: {self_cmd('guest', '--name', a.name, 'status')}")
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

def main(argv: list[str] | None = None) -> int:
    """The guest side's subcommands, parsed. NOT AN ENTRY POINT ANY MORE.

    It is reached as `vfioctl guest ...` and nowhere else: one job, one runnable
    name (K15). This file kept its own `__main__` while the guest side was being
    built, which is why prog= has to be spelled out -- argparse would otherwise
    name the program after this file and print help nobody can copy.
    """
    if str(HERE.parent) not in sys.path:
        sys.path.insert(0, str(HERE.parent))
    from core import provenance

    p = argparse.ArgumentParser(
        prog="vfioctl guest",
        description="Windows misafirini gözetimsiz kur (autounattend.xml turu)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # The header above is written for a reader in a clone, where ./vfioctl
        # is the invocation. It is also this subcommand's help text, and a
        # printed command that does not resolve closes the diagnosis path it
        # was there to open (K20) -- so the name is resolved on the way out,
        # here, where the docstring becomes output and nowhere else.
        epilog=provenance.rewrite(__doc__ or ""),
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
    st.add_argument("--start", action="store_true",
                    help="domain kapalıysa başlat ve ajanı bekle (kartlı tur: düz VT)")
    st.add_argument("--timeout", type=int, default=15,
                    help="ajan için dakika (varsayılan 15)")
    st.add_argument("--yes", action="store_true",
                    help="düz VT uyarısını sorma")
    st.set_defaults(func=lambda a: guest_setup(a, a.name))

    u = sub.add_parser("usb", help="koşan misafire USB aygıtı ödünç ver / geri al")
    u.add_argument("--attach", metavar="VENDOR:PRODUCT", default="",
                   help="aygıtı koşan misafire ver (ör. 8087:0032)")
    u.add_argument("--detach", metavar="VENDOR:PRODUCT", default="",
                   help="aygıtı host'a geri al")
    u.set_defaults(func=cmd_usb)

    nv = sub.add_parser("nvme",
                        help="NVMe denetleyicisini domain'e ver ya da geri al")
    nv.add_argument("--attach", metavar="PCI", default="",
                    help="devredilecek denetleyicinin PCI adresi (0000:02:00.0)")
    nv.add_argument("--detach", metavar="PCI", default="",
                    help="domain'den çıkarılacak denetleyicinin PCI adresi")
    nv.set_defaults(func=cmd_nvme)

    pt = sub.add_parser("passthrough", help="kartı domain'e ver ya da geri al")
    pt.add_argument("--off", action="store_true", help="kartı domain'den çıkar")
    pt.add_argument("--profile", default=None,
                    help="DMI ile seçmek yerine adı verilen profili kullan")
    pt.set_defaults(func=cmd_passthrough)

    c = sub.add_parser("clean", help="domain + disk + çalışma dizini sil")
    c.set_defaults(func=cmd_clean)

    a = p.parse_args(argv)

    if getattr(a, "win_iso", None) == "":
        found = sorted(Path.home().glob("İndirilenler/*windows*11*.iso"))
        if not found:
            die("Windows ISO'su bulunamadı, --win-iso ile ver")
        a.win_iso = str(found[-1])

    return a.func(a)
