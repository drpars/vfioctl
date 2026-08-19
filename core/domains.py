"""Which libvirt domain wants which host device -- asked in one place only.

WHY THIS IS IN core/ AND NOT IN guest/build.py. Two readers need the same fact
and they sit on opposite sides of this tool's one import edge: guest/build.py
guards destructive steps with it, and core/inventory.py annotates its report
with it. build.py cannot be imported from core -- the entry script loads it by
path, late, and only for the `guest` subcommand -- so leaving the fact there
would have meant writing it a second time in inventory. That is the failure
this codebase names everywhere else: two tables drift, and the one that drifts
is the one nobody reads until it lets something through.

IT ONLY READS, AND IT IS BUILT SO THAT IT CANNOT DO ANYTHING ELSE. Every call
goes through _virsh(), which refuses a verb outside READ_ONLY_VERBS with a
ValueError rather than an assert -- an assert vanishes under `python -O` and
takes the guarantee with it. stdin is /dev/null on every call, because a virsh
that decides to prompt would otherwise sit forever on a terminal nobody is
watching, and a timeout does not help a process waiting on a read.

THE TIMEOUT IS THE CALLER'S CHOICE, AND FOR A GUARD IT MUST BE None. A bounded
read turns "libvirtd is wedged" into "this domain claims nothing", and every
guard built on these functions refuses on a NON-EMPTY answer -- so a timeout
does not make them refuse sooner, it makes them pass. That is not hypothetical
here: core/selftest.py records this machine wedging libvirtd inside the tool's
own hook, and the guard that would then fall open is the one stopping
`build --system-nvme` from repartitioning a drive another domain boots from.
guest/build.py therefore passes None and keeps today's behaviour -- hang until
the operator interrupts, which is at least loud. core/inventory.py is the
opposite case: nothing is guarded by its report, a hung `inventory` helps
nobody, so it passes a budget and then says out loud which domains went unread.

THE STDERR IS KEPT SEPARATE, unlike core/selftest.py's own _sh, which returns
stdout and stderr merged. That is right for a log line and wrong here: a libvirt
warning printed on stderr would land inside the XML and ElementTree would fail
on a domain that answered perfectly well.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree

URI = "qemu:///system"

# The whole verb vocabulary of this module. Anything that could change a domain
# is absent by construction rather than by convention.
READ_ONLY_VERBS = frozenset({"list", "dumpxml"})

# What timeout(1) reports, reused here so that "never answered" is one value
# everywhere in this tool (core/selftest.py:92 picked it first).
TIMED_OUT = 124

# Bounds for the reporting path only; guards pass None. The first call carries
# the connection, which on a cold daemon includes libvirtd's socket activation,
# so it gets its own larger allowance -- a flat per-call bound would report
# "sorulamadı" on a machine that was merely starting up.
CONNECT_TIMEOUT = 8.0
READ_TIMEOUT = 5.0
WALL_BUDGET = 12.0

def _virsh(args: list[str], timeout: float | None) -> tuple[int, str]:
    """One read-only virsh call. Returns (returncode, stdout)."""
    if not args or args[0] not in READ_ONLY_VERBS:
        raise ValueError(f"salt-okuma olmayan virsh fiili: {args[:1]}")
    try:
        out = subprocess.run(
            ["virsh", "-c", URI, *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TIMED_OUT, ""
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return out.returncode, out.stdout


def available() -> bool:
    """Is there a virsh to ask at all -- answered without spawning anything.

    The live-ISO and never-installed cases are the common ones, and they are
    also the ones where spawning a process to find out would be the slowest
    possible way to learn nothing.
    """
    return shutil.which("virsh") is not None


def defined_domains(timeout: float | None = None) -> list[str]:
    """Every defined domain, one name per line as virsh prints them."""
    rc, out = _virsh(["list", "--all", "--name"], timeout)
    if rc != 0:
        return []
    return [n for n in (line.strip() for line in out.splitlines()) if n]


def running_domains(timeout: float | None = None) -> list[str]:
    rc, out = _virsh(["list", "--name"], timeout)
    if rc != 0:
        return []
    return [n for n in (line.strip() for line in out.splitlines()) if n]


def domain_xml(name: str, *, inactive: bool = False,
               timeout: float | None = None) -> str | None:
    """A domain's XML, or None if it could not be read.

    None and "" are kept apart on purpose: an empty claim set read successfully
    means the domain wants nothing, and a domain that could not be read means
    nothing is known. Collapsing the two is how a report ends up printing
    "nobody wants this" about a device somebody wants.
    """
    args = ["dumpxml", name] + (["--inactive"] if inactive else [])
    rc, out = _virsh(args, timeout)
    return out if rc == 0 else None


# The qemu:commandline namespace, and the one argument this tool cares about.
QEMU_NS = {"qemu": "http://libvirt.org/schemas/domain/qemu/1.0"}
MEM_PATH = re.compile(r"mem-path[\"']?\s*:\s*[\"']([^\"']+)")


def _mempaths_of(xml: str) -> set[str]:
    """The mem-path arguments a domain hands straight to qemu.

    THROUGH THE PARSER FIRST, THEN THE REGEX -- IN THAT ORDER, AND THAT IS THE
    WHOLE FIX. The value is a JSON blob living inside an XML attribute, so it
    comes back escaped:

        <qemu:arg value='{&apos;mem-path&apos;:&apos;/dev/kvmfr0&apos;,...}'/>

    A regex over the raw document therefore never finds a quote to match, and
    this one never matched: measured on this machine, the pattern returns
    nothing at all against `virsh dumpxml`, so /dev/kvmfr0 was missing from
    every claim set guard_exclusive_devices() has ever compared. The clash it
    exists to catch went unguarded, and only the card kept it honest -- both
    domains here want the card too, so the guard fired on that instead and the
    hole never showed. A card-less mode-2 guest is exactly the case that would
    have removed the card from the comparison and left nothing behind it.

    Letting ElementTree unescape the attribute hands over the JSON as qemu
    itself reads it; the regex then still absorbs either quoting style, which
    is why it was a regex rather than a JSON parse in the first place.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return set()
    return {path
            for arg in root.findall("./qemu:commandline/qemu:arg", QEMU_NS)
            for path in MEM_PATH.findall(arg.get("value", ""))}


def _pci_of(xml: str) -> set[str]:
    """PCI functions a domain's XML takes, as 0000:01:00.0.

    ONLY <source> IS READ. A <hostdev> block carries two addresses -- the host
    function it points at and the slot libvirt gives it inside the guest -- and
    reading the block as a whole let the four attributes come from two
    different elements (fixed in cdb4059, measured on the card's audio pair).
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return set()
    found: set[str] = set()
    for hostdev in root.findall("./devices/hostdev[@type='pci']"):
        addr = hostdev.find("./source/address")
        if addr is None:
            continue
        try:
            found.add("{:04x}:{:02x}:{:02x}.{}".format(
                int(addr.get("domain", "0x0"), 16),
                int(addr.get("bus", "0x0"), 16),
                int(addr.get("slot", "0x0"), 16),
                int(addr.get("function", "0x0"), 16),
            ))
        except ValueError:
            continue
    return found


def _usb_of(xml: str) -> set[str]:
    """USB devices a domain's XML takes, as vendor:product -- inventory's `ids`."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return set()
    found: set[str] = set()
    for source in root.findall("./devices/hostdev[@type='usb']/source"):
        vendor, product = source.find("vendor"), source.find("product")
        if vendor is None or product is None:
            continue
        found.add("{}:{}".format(
            vendor.get("id", "").removeprefix("0x").lower(),
            product.get("id", "").removeprefix("0x").lower()))
    return found


def claims_of(name: str, timeout: float | None = None) -> set[str]:
    """Host things this domain takes exclusively: PCI functions and mem-paths.

    This is the guards' entry point and it reads the ACTIVE view, which is what
    they have always asked: for a running domain that is what it holds now, and
    for a shut-off one virsh answers with the stored definition.
    """
    xml = domain_xml(name, timeout=timeout)
    if xml is None:
        return set()
    return {f"mem-path:{p}" for p in _mempaths_of(xml)} | _pci_of(xml)


def pci_claims_of(name: str, timeout: float | None = None) -> set[str]:
    return {c for c in claims_of(name, timeout) if not c.startswith("mem-path:")}


def usb_claims_of(name: str, timeout: float | None = None) -> set[str]:
    """vendor:product of every USB device the domain holds RIGHT NOW.

    The active view is the only record there is for these: `guest usb` attaches
    --live and never --config, so a lent device exists in libvirt's running
    copy and nowhere else.
    """
    xml = domain_xml(name, timeout=timeout)
    return _usb_of(xml) if xml is not None else set()

# --------------------------------------------------------------------------- #
# the snapshot the report annotates itself with
# --------------------------------------------------------------------------- #

@dataclass
class Claimants:
    """Who wants what, plus how much of that answer is actually known.

    `known` is False when nothing could be asked at all. `unread` names the
    domains that were asked and did not answer, and it exists so the report can
    qualify its own silence: an empty claim line and an unasked question look
    identical on the page, and only one of them is a measurement.
    """
    known: bool = False
    detail: str = ""
    unread: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    # ident -> domain names. `defined` is the stored definition, `live` is what
    # a running domain holds beyond it -- a loan that ends when it shuts down.
    defined: dict[str, list[str]] = field(default_factory=dict)
    live: dict[str, list[str]] = field(default_factory=dict)

    def of(self, ident: str) -> tuple[list[str], list[str]]:
        return self.defined.get(ident, []), self.live.get(ident, [])


def _add(where: dict[str, list[str]], ident: str, name: str) -> None:
    names = where.setdefault(ident, [])
    if name not in names:
        names.append(name)


def read(budget: float = WALL_BUDGET) -> Claimants:
    """Every domain's claims, within a wall budget, never raising.

    THE BUDGET IS A WALL AND NOT A PER-CALL BOUND. Thirty domains each
    answering just under a per-call timeout would take thirty times that
    timeout while never once timing out, so the deadline is taken from a
    monotonic clock and each call is clamped to whatever is left of it.
    Whatever the budget cuts off is reported as unread rather than as empty.
    """
    if not available():
        return Claimants(known=False, detail="virsh yok — libvirt kurulu değil")

    deadline = time.monotonic() + budget

    def left(first: bool = False) -> float:
        return min(CONNECT_TIMEOUT if first else READ_TIMEOUT,
                   deadline - time.monotonic())

    if left(first=True) <= 0:
        return Claimants(known=False, detail="süre bütçesi tükendi")
    names = defined_domains(timeout=left(first=True))
    if not names:
        return Claimants(known=True, detail="tanımlı domain yok")

    running = set(running_domains(timeout=max(left(), 0.5)))
    snapshot = Claimants(known=True)

    for name in names:
        remaining = left()
        if remaining <= 0.5:
            snapshot.unread.append(name)
            continue
        stored = domain_xml(name, inactive=True, timeout=remaining)
        if stored is None:
            snapshot.unread.append(name)
            continue
        snapshot.read.append(name)
        for ident in _pci_of(stored) | _usb_of(stored):
            _add(snapshot.defined, ident, name)

        if name not in running:
            continue
        # A running domain may hold more than its definition says: `guest usb`
        # attaches --live and never --config on purpose, so a lent radio exists
        # in exactly one place and disappears when the guest shuts down.
        remaining = left()
        if remaining <= 0.5:
            continue
        active = domain_xml(name, timeout=remaining)
        if active is None:
            continue
        for ident in (_pci_of(active) | _usb_of(active)) - (
                _pci_of(stored) | _usb_of(stored)):
            _add(snapshot.live, ident, name)

    if snapshot.unread:
        snapshot.detail = (f"{len(snapshot.read)}/{len(names)} tanım okundu — "
                           f"{', '.join(snapshot.unread)} okunamadı")
    return snapshot
