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
import subprocess
from xml.etree import ElementTree

URI = "qemu:///system"

# The whole verb vocabulary of this module. Anything that could change a domain
# is absent by construction rather than by convention.
READ_ONLY_VERBS = frozenset({"list", "dumpxml"})

# What timeout(1) reports, reused here so that "never answered" is one value
# everywhere in this tool (core/selftest.py:92 picked it first).
TIMED_OUT = 124

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

    The mem-path comes out of qemu:commandline with a regex rather than an XML
    walk, because the argument is a JSON string inside an attribute and libvirt
    is free to hand it back with either quoting style.

    This is the guards' entry point and it reads the ACTIVE view, which is what
    they have always asked: for a running domain that is what it holds now, and
    for a shut-off one virsh answers with the stored definition.
    """
    xml = domain_xml(name, timeout=timeout)
    if xml is None:
        return set()
    claims = {f"mem-path:{p}" for p in
              re.findall(r"mem-path[\"']?\s*:\s*[\"']([^\"']+)", xml)}
    return claims | _pci_of(xml)


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
