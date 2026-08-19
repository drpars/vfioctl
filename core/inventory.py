"""`vfioctl inventory` -- what else on this machine could go to a guest, and
what handing it over would cost the host.

IT REPORTS AND NEVER APPLIES (K14). The question it answers is not "can it be
done" but "what does the host lose". Those are different questions and only the
second one has an answer worth writing down: every device here is technically
detachable, and roughly half of them would take the running desktop with them.

IT IS ALSO THE POLICY OWNER FOR THE ONE COMMAND THAT DOES APPLY. `guest usb`
lends a USB device to a running guest, and it does not carry a verdict table of
its own -- it asks usb_verdict() here. Two tables would drift, and the one that
drifted would be the one nobody reads until it lets something through.

WHY IT IS NOT PART OF doctor. doctor answers one question -- may this tool
write on this machine -- and its verdict is the last line of its output. An
inventory of thirty PCI functions printed above that line buries it. The two
also differ in when they are useful: inventory is worth reading precisely when
the gate is closed, because what it lists is why. (K14 names doctor as the
place; the split is this session's call, and the part of K14 that mattered --
asks and reports, never applies -- is unchanged.)

THE UNIT OF HANDOVER DIFFERS BY BUS, AND CONFUSING THE TWO IS EXPENSIVE. A PCI
device moves as its whole IOMMU group, so a group with a stranger in it cannot
move at all. A USB device moves on its own, by vendor:product, and libvirt
does the detaching -- so it is the controller, not the device, that would take
everything else plugged into it. On this machine that distinction is the whole
answer: the Bluetooth radio and the laptop's built-in keyboard sit on the same
xHCI, so handing over the radio is cheap and handing over its controller costs
the keyboard.

REFUSALS ARE ABOUT DATA, ABOUT THE SCREEN, AND ABOUT BEING ABLE TO UNDO THIS.
A disk the host has mounted, has in fstab, or swaps onto is refused outright --
that is K14's hard protection, written before anything can hand a disk over,
so the day it exists it is already guarded. The card carrying the host's screen
is refused for the same reason. A lost keyboard or a lost radio is recoverable
by shutting the guest down, so it warns rather than refuses -- but shutting a
guest down takes an input device, so the last one the host has is refused too.
Everything else is said loudly and allowed.

IT ALSO SAYS WHO ELSE WANTS THE DEVICE, AND THAT HALF IS NOT SYSFS. Every
other line here is read from the machine; the claim comes from libvirt through
core.domains, read-only and on a wall budget, and it is the one thing on the
page this file does not measure itself. It is asked for once, after the items
are built, so a claiming domain cannot reach a verdict -- and when it cannot be
asked at all the report says so in its header rather than printing rows that
look like nobody wants anything. The cost half of the question was always here
("what does the HOST lose"); this is the other side of it, and until it existed
two domains could quietly claim the same card with the inventory silent.

EVERY MARK THAT IS NOT A REFUSAL COMES FROM ONE RULE IN ONE PLACE. Both buses
ask _cost_verdict(): a row the host pays nothing measurable for is a candidate,
a row it pays for is a warning. The rule used to be written twice and the two
copies disagreed -- this machine's only Bluetooth radio was counted as a cost
on its controller's row and marked a free candidate on its own row four lines
below. A refusal outranks the rule, and a refusal is the only thing that stops
anything: both places outside this file that read a verdict, in guest/build.py,
test for REFUSE and nothing else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import doctor, probe
from .profile import Profile
from .term import paint

BLOCK = Path("/sys/class/block")
MOUNTINFO = Path("/proc/self/mountinfo")
SWAPS = Path("/proc/swaps")
FSTAB = Path("/etc/fstab")
DEV_DISK = Path("/dev/disk")

CANDIDATE, WARN, REFUSE = "aday", "uyarı", "red"

# PCI class prefixes that are never a handover unit: host bridges and PCIe
# ports are the topology itself. They are counted in the footer rather than
# listed, because thirty of them is what makes an inventory unreadable.
CLASS_BRIDGE = "0x06"


def _cost_verdict(cost: list[str]) -> str:
    """The mark a row gets once nothing has refused it: the rule, for every bus.

    A "✓" says this tool measured no cost to the host; anything the host does
    lose while the guest runs is a "!". Written here once rather than at each
    bus, because it was written twice and the two copies disagreed: on PCI a
    cost demoted the verdict, on USB the single-Bluetooth cost was printed and
    the verdict left alone. One fact -- the host loses its radio -- therefore
    came out as "!" on the controller's row and "✓" on the radio's own row four
    lines below it. The next device class would have re-created that split; a
    rule cannot.

    IT NEVER RETURNS A REFUSAL, AND THAT IS WHY IT IS NOT CALLED "the verdict".
    Refusals answer a different question -- can this be undone -- and they
    outrank any cost, so each bus decides its own and applies it AFTER this.
    Letting this function have the last word would turn the one USB refusal
    (no input device left on the host) into a warning, and the only external
    reader of that refusal, guest/build.py, would then lend the host's last
    keyboard away.
    """
    return WARN if cost else CANDIDATE


# --------------------------------------------------------------------------- #
# what the host's storage sits on
# --------------------------------------------------------------------------- #

def _resolve_spec(spec: str) -> Path | None:
    """An fstab source (UUID=..., LABEL=..., /dev/...) as a sysfs block node."""
    for prefix, directory in (("UUID=", "by-uuid"), ("LABEL=", "by-label"),
                              ("PARTUUID=", "by-partuuid"),
                              ("PARTLABEL=", "by-partlabel")):
        if spec.startswith(prefix):
            link = DEV_DISK / directory / spec[len(prefix):]
            if not link.exists():
                return None
            return BLOCK / os.path.basename(os.path.realpath(link))
    if spec.startswith("/dev/"):
        node = BLOCK / os.path.basename(os.path.realpath(spec))
        return node if node.exists() else None
    return None


def _carriers(node: Path, depth: int = 0) -> set[str]:
    """The PCI addresses a block node ultimately lives on.

    A partition resolves straight to its controller through its real path. A
    device-mapper or RAID node has no PCI ancestor at all, so it is followed
    down through slaves/ -- otherwise an encrypted or LVM root would look like
    it sits on no hardware, and the protection would let its disk through.
    """
    if depth > 8 or not node.exists():
        return set()
    address = probe._pci_parent(node)
    if address:
        return {address}
    found: set[str] = set()
    for slave in sorted((node / "slaves").glob("*")):
        found |= _carriers(BLOCK / slave.name, depth + 1)
    return found


@dataclass
class Claim:
    node: str                # nvme0n1p2
    where: str               # /home
    sources: list[str] = field(default_factory=list)   # bağlı, fstab, takas

    def __str__(self) -> str:
        return f"{self.node} → {self.where} ({', '.join(self.sources)})"


@dataclass
class StorageClaims:
    """Why the host needs a given storage controller, in its own words.

    KEYED BY BLOCK NODE RATHER THAN BY REASON, because a partition that is
    mounted is almost always in fstab too, and listing both as separate lines
    turns one fact into two. What matters per partition is the union: where it
    is used and how it is claimed.
    """
    reasons: dict[str, dict[str, Claim]] = field(default_factory=dict)

    def add(self, address: str, node: str, where: str, source: str) -> None:
        claims = self.reasons.setdefault(address, {})
        claim = claims.get(node) or Claim(node=node, where=where)
        if source not in claim.sources:
            claim.sources.append(source)
        if claim.where != where and where not in claim.where:
            claim.where = f"{claim.where}, {where}"
        claims[node] = claim

    def of(self, address: str) -> list[Claim]:
        return sorted(self.reasons.get(address, {}).values(),
                      key=lambda c: c.node)


def host_storage_claims() -> StorageClaims:
    """Every PCI address the host's own filesystems depend on, and why.

    THREE SOURCES, BECAUSE ONE IS NOT ENOUGH. What is mounted right now is the
    obvious half; fstab is the disk that is unmounted at this moment and would
    be mounted at the next boot, which is exactly the disk someone would think
    is free. Swap is neither -- a swap partition appears in no mount table and
    losing it mid-run takes the host down with it.
    """
    claims = StorageClaims()

    for line in _lines(MOUNTINFO):
        fields = line.split()
        if len(fields) < 5:
            continue
        major_minor, mountpoint = fields[2], fields[4]
        node = BLOCK / os.path.basename(
            os.path.realpath(Path("/sys/dev/block") / major_minor))
        for address in _carriers(node):
            claims.add(address, node.name, mountpoint, "bağlı")

    for line in _lines(SWAPS)[1:]:
        spec = line.split()[0] if line.split() else ""
        if spec.startswith("/dev/"):
            node = _resolve_spec(spec)
            for address in _carriers(node) if node else set():
                claims.add(address, node.name, "takas", "takas alanı")

    for line in _lines(FSTAB):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            continue
        node = _resolve_spec(fields[0])
        if node is None:
            continue
        for address in _carriers(node):
            claims.add(address, node.name, fields[1], "fstab")

    return claims


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# the items
# --------------------------------------------------------------------------- #

@dataclass
class Item:
    bus: str                 # "pci" | "usb"
    ident: str               # 0000:04:00.0 | 1-4
    ids: str                 # c0a9:540a | 8087:0032
    title: str
    driver: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    group: str | None = None            # IOMMU group, PCI only
    note: str | None = None             # what it is, when that is not obvious
    # Who else wants it. Kept out of `reasons` on purpose: guest/build.py
    # prints that list verbatim and then dies with "yukarıdaki gerekçe", so a
    # domain name parked in it would read as the justification for a refusal.
    claimed_by: list[str] = field(default_factory=list)   # in a stored definition
    lent_to: list[str] = field(default_factory=list)      # live only, ends at shutdown


def _pci_title(device: probe.PciDevice) -> str:
    """A class name, since this tool refuses to depend on a PCI id database.

    lspci's names come from pci.ids, which is a package this project does not
    require and which is absent on a live ISO. The class is in sysfs and says
    the thing that matters here -- what kind of device would be leaving.
    """
    classes = {
        "0x0100": "SCSI denetleyicisi", "0x0101": "IDE denetleyicisi",
        "0x0106": "SATA denetleyicisi", "0x0108": "NVMe depolama",
        "0x0200": "Ethernet", "0x0280": "kablosuz ağ",
        "0x0300": "ekran kartı (VGA)", "0x0380": "ekran kartı",
        "0x0403": "ses (HDA)", "0x0480": "çokluortam",
        "0x0500": "bellek denetleyicisi",
        "0x0c03": "USB denetleyicisi", "0x0c05": "SMBus",
        "0x1080": "şifreleme yardımcısı",
        "0x0806": "IOMMU", "0x0880": "sistem çevre birimi",
    }
    return classes.get(device.pci_class[:6], f"sınıf {device.pci_class[:6]}")


USB_CLASS_NAMES = {"e0": "kablosuz (Bluetooth)", "03": "girdi aygıtı (HID)",
                   "01": "ses", "08": "depolama", "02": "iletişim",
                   "0e": "kamera", "09": "hub"}


def _usb_title(device: probe.UsbDevice) -> str:
    """What to call a USB device, given that half of them name themselves badly.

    THE STRINGS ARE OPTIONAL AND OFTEN REDUNDANT. Razer's descriptor repeats
    the manufacturer inside the product name ("Razer Razer DeathAdder"), and
    this machine's Bluetooth radio carries neither string at all -- it is the
    one device here someone would actually want to hand over, so falling back
    to "(adsız)" would hide the interesting row. The interface class is always
    there and says what the thing does.
    """
    maker, described = device.manufacturer, device.description
    if described and maker and described.lower().startswith(maker.lower()):
        maker = None
    name = " ".join(p for p in (maker, described) if p).strip()
    if name:
        return name
    classes = [USB_CLASS_NAMES.get(i.usb_class, f"sınıf {i.usb_class}")
               for i in device.interfaces]
    unique = list(dict.fromkeys(classes))
    return unique[0] if unique else "(tanımsız USB cihazı)"


def pci_items(machine: probe.Machine, p: Profile | None, claims: StorageClaims,
              usb: list[probe.UsbDevice]) -> tuple[list[Item], int]:
    """Every PCI leaf device, judged. Returns (items, bridges left out)."""
    dgpu, igpu = doctor._cards(machine, p)
    items: list[Item] = []
    bridges = 0

    for device in machine.devices:
        if device.pci_class.startswith(CLASS_BRIDGE):
            bridges += 1
            continue

        reasons: list[str] = []
        verdict = CANDIDATE
        note = None

        # 1. The group is the unit. A stranger in it is a hard no, and it is
        #    checked first because it makes every other question moot. The
        #    membership is printed whenever it is more than the device itself,
        #    including when it passes -- "isolated" is a claim, and the list is
        #    what lets a reader check it.
        if device.iommu_group:
            members = probe.iommu_group_members(device.iommu_group)
            others = [m for m in members if m != device.address]
            strangers = [m for m in others if not _travels_with(device, m)]
            if strangers:
                verdict = REFUSE
                reasons.append("IOMMU grubu paylaşımlı, yabancı cihazlar da "
                               "birlikte gider: " + ", ".join(strangers))
            elif others:
                reasons.append("grubun diğer üyeleri aynı fiziksel cihazın "
                               "işlevleri: " + ", ".join(others))
        else:
            verdict = REFUSE
            reasons.append("IOMMU grubu yok — devir birimi oluşturamaz")

        # 2. Storage the host is standing on. K14's hard protection, in place
        #    before anything exists that could hand a disk over.
        storage = claims.of(device.address)
        if storage:
            verdict = REFUSE
            reasons.append("host'un depolaması: "
                           + "; ".join(str(c) for c in storage))

        # 3. The card that keeps the host's screen alive.
        if igpu is not None and device.address == igpu.address:
            verdict = REFUSE
            reasons.append("host ekranını taşıyan iGPU — devri masaüstünü "
                           "götürür (K13'ün sert ölçütü)")
        elif device.boot_vga and (dgpu is None or device.address != dgpu.address):
            verdict = REFUSE
            reasons.append("boot ekran kartı")

        # 4. The one this tool already moves.
        if dgpu is not None and device.address in (dgpu.address,
                                                   _audio_address(machine, dgpu)):
            note = "bu aracın devrettiği kart" if device.address == dgpu.address \
                else "devredilen kartın ses fonksiyonu — grubuyla birlikte gider"

        # 5. Everything else the host would notice. The rule is shared with
        #    USB; the gate is not. A refusal outranks any cost, so a refused
        #    row is never asked for one -- which is what keeps "IOMMU grubu
        #    yok" from being followed by a bus-00 warning nobody can act on.
        if verdict == CANDIDATE:
            cost = _host_cost(device, machine, usb)
            reasons.extend(cost)
            verdict = _cost_verdict(cost)

        items.append(Item(
            bus="pci", ident=device.address, ids=device.ids,
            title=_pci_title(device), driver=device.driver or "(bağlı değil)",
            verdict=verdict, reasons=reasons,
            group=device.iommu_group, note=note,
        ))

    return items, bridges


def _audio_address(machine: probe.Machine, dgpu: probe.PciDevice) -> str | None:
    audio = machine.audio_beside(dgpu)
    return audio.address if audio else None


def _travels_with(device: probe.PciDevice, other: str) -> bool:
    """Is another group member part of the same physical device?

    A discrete GPU and its HDA function share a slot and a group by design;
    calling that "shared" would refuse the one handover this tool exists to
    do. Anything on a different slot is a stranger.
    """
    return other.rsplit(".", 1)[0] == device.slot


def _host_cost(device: probe.PciDevice, machine: probe.Machine,
               usb: list[probe.UsbDevice]) -> list[str]:
    """What the host loses if this leaves -- warnings, never refusals.

    BEING BOUND TO A HOST DRIVER IS NOT A COST AND IS NOT LISTED. Every
    candidate on a running machine has a driver -- the discrete GPU itself sits
    on nvidia when it is idle -- so a warning for it fires on nearly every row
    and teaches the reader to skip the column where the real ones live. The
    driver is printed on the device's own line; what belongs here is only what
    the host would actually miss.
    """
    cost: list[str] = []
    same_class = [d for d in machine.devices
                  if d.pci_class[:6] == device.pci_class[:6]]

    if device.pci_class.startswith("0x0c03"):
        attached = [u for u in usb if u.controller == device.address]
        if attached:
            names = ", ".join(f"{u.name} ({_usb_title(u)})" for u in attached)
            cost.append(f"üstündeki her USB cihazı birlikte gider: {names}")
    if device.pci_class[:6] in ("0x0200", "0x0280") and len(same_class) == 1:
        cost.append("bu türden tek ağ arabirimi — host o yolu kaybeder")
    if device.pci_class[:6] == "0x0403" and device.driver == "snd_hda_intel":
        cost.append("host'un ses çıkışı bu cihazda")
    # Functions of the root complex are the platform, not peripherals: the
    # chipset, the SMBus, the IOMMU. Warned rather than refused, because on
    # other machines a genuinely assignable SATA or USB controller lives on
    # bus 00 too -- the cost is real, the impossibility is not.
    if device.address.split(":")[1] == "00":
        cost.append("yonga setinin bir işlevi (bus 00) — platformun parçası, "
                    "çevre birimi değil")
    return cost


def _input_kinds(device: probe.UsbDevice) -> list[str]:
    """"Keyboard", "Mouse", ... -- the part of an input name that is not the
    device's own name repeated.

    THE PREFIX TO STRIP IS THE RAW DESCRIPTOR, NOT THE CLEANED-UP TITLE. Input
    nodes are named from the descriptor as the kernel found it, so this
    machine's mouse registers "Razer Razer DeathAdder V3 HyperSpeed Keyboard"
    while the title has already collapsed the doubled manufacturer -- matching
    against the title leaves the whole string, which is how one device came out
    as three identical-looking warnings. A node named exactly like its device
    announces no kind at all, and saying so is more honest than guessing one.
    """
    maker, described = device.manufacturer or "", device.description or ""
    prefixes = sorted(
        (p.lower() for p in (f"{maker} {described}", described, maker) if p.strip()),
        key=len, reverse=True)
    kinds: list[str] = []
    for name in device.inputs:
        tail = name
        for prefix in prefixes:
            if name.lower().startswith(prefix):
                tail = name[len(prefix):].strip()
                break
        kinds.append(tail or "tür bildirmiyor")
    return list(dict.fromkeys(kinds))


def _host_keeps(usb_name: str, inputs: list[probe.InputDevice]) -> tuple[bool, bool, str]:
    """What the host would still be driven by if this USB device left.

    Returns (a keyboard stays, a pointer stays, one name per kind). The
    question is asked of every bus, because the answer usually lives off USB
    entirely -- see probe.input_devices().

    ONE NAME PER KIND, NOT THE ROLL CALL. A single wireless mouse registers
    five input nodes, so listing everything that stays turned a two-word answer
    into six names and buried the part that decides the verdict. The one named
    is preferably not on USB at all, since that is the one that cannot be lent
    away by the next invocation either.
    """
    staying = [i for i in inputs if i.usb_device != usb_name and i.usable]

    def pick(kind: str) -> str | None:
        matching = [i for i in staying if getattr(i, kind)]
        if not matching:
            return None
        return min(matching, key=lambda i: (i.usb_device is not None, i.name)).name

    keyboard, pointer = pick("keyboard"), pick("pointer")
    labels = [f"{label} ({name})" for label, name
              in (("klavye", keyboard), ("işaretçi", pointer)) if name]
    return keyboard is not None, pointer is not None, ", ".join(labels)


def usb_verdict(device: probe.UsbDevice, devices: list[probe.UsbDevice],
                inputs: list[probe.InputDevice]) -> tuple[str, list[str]]:
    """Whether this USB device may be lent to a guest, and what it costs.

    THE ONE REFUSAL IS ABOUT BEING ABLE TO TAKE IT BACK. Everything else here
    is recoverable by shutting the guest down, and shutting a guest down needs
    a keyboard or a pointer. A machine whose only keyboard and only mouse are
    both USB can therefore hand over exactly one of them; handing over the
    second leaves nothing to type the command that undoes it. On this laptop
    the rule stays quiet -- the touchpad is I2C and never leaves -- and that is
    the point of counting what stays rather than what goes.

    TWO LISTS, BECAUSE NOT EVERY PRINTED LINE IS A COST. `cost` is what the
    host loses, and it is the only thing the rule is allowed to see; `reasons`
    is everything worth printing and a superset of it. "host'ta kalan girdi"
    is the clearest case: it says what the host KEEPS, so feeding it to the
    rule would make the rule's own docstring false about its input.

    UNLIKE PCI, THE COSTS ARE PRINTED UNDER A REFUSAL TOO, deliberately. The
    line naming the input device is what the refusal means; guest/build.py
    prints these and then dies with "yukarıdaki gerekçe", so dropping them
    would leave that sentence pointing at nothing.
    """
    reasons: list[str] = []
    cost: list[str] = []
    refused = False
    bluetooth = [d for d in devices if "btusb" in d.drivers]

    if "usbhid" in device.drivers:
        # One HID device registers several input nodes (a mouse announces a
        # keyboard, a consumer-control and a mouse), and printing all of
        # them repeats the device's own name five times. What the reader
        # needs is what kind of input it is, so the names are reduced to
        # their distinguishing tail.
        kinds = _input_kinds(device)
        label = ", ".join(kinds) if kinds else "girdi aygıtı"
        cost.append(f"host'un girdi aygıtı ({label}) — devredilirse "
                    "misafire geçer, host'ta çalışmaz")
        reasons.extend(cost[-1:])
        keyboard, pointer, staying = _host_keeps(device.name, inputs)
        if keyboard or pointer:
            # What stays is not a loss, so it is printed and not counted.
            reasons.append(f"host'ta kalan girdi: {staying}")
        else:
            refused = True
            reasons.append("host'ta başka girdi aygıtı kalmıyor — devri geri "
                           "alacak komutu yazacak klavye ya da fare kalmaz")
    # A separate `if`, never an `elif`: a combined HID and Bluetooth dongle
    # costs the host both, and an `elif` would silently drop the second line.
    if "btusb" in device.drivers and len(bluetooth) == 1:
        cost.append("makinedeki tek Bluetooth — misafir koşarken host "
                    "Bluetooth'unu kaybeder")
        reasons.extend(cost[-1:])
    return (REFUSE if refused else _cost_verdict(cost)), reasons


def usb_items(devices: list[probe.UsbDevice],
              inputs: list[probe.InputDevice] | None = None) -> list[Item]:
    inputs = probe.input_devices() if inputs is None else inputs
    items: list[Item] = []
    for device in devices:
        verdict, reasons = usb_verdict(device, devices, inputs)
        items.append(Item(
            bus="usb", ident=device.name, ids=device.ids,
            title=_usb_title(device),
            driver=", ".join(device.drivers) or "(sürücüsüz)",
            verdict=verdict, reasons=reasons,
            note=(f"denetleyici {device.controller}" if device.controller else None),
        ))
    return items


# --------------------------------------------------------------------------- #
# who else wants it
# --------------------------------------------------------------------------- #

def annotate_claimants(items: list[Item], guests) -> None:
    """Fill in which domains want each device -- AFTER every verdict is decided.

    THE ORDER IS THE GUARANTEE AND NOT A CONVENTION. This runs on Items that
    are already built, so a claiming domain cannot reach pci_items(),
    usb_verdict() or _host_cost() even by a later edit: by the time a claimant
    is in scope the verdict is a string on a finished object. Inventory reports
    and does not decide (K14), and "another guest wants this" is a fact about
    the machine, not a permission.

    NOTHING IS INFERRED ACROSS AN IOMMU GROUP. A domain claiming 0000:01:00.0
    does not get printed against 0000:01:00.1, even though the group moves as
    one unit -- deriving that would be the inventory deciding what a domain
    meant. Both functions appear on their own when a domain really asks for
    both, which is what these two do.

    A SNAPSHOT NOBODY COULD READ ANNOTATES NOTHING. Empty lists stay empty, and
    the caller prints no line for them, so a row never claims that nobody wants
    a device when the truth is that nobody could be asked.
    """
    if not guests.known:
        return
    for item in items:
        # PCI is addressed by function, USB by vendor:product -- the same two
        # keys libvirt writes into a <hostdev> source.
        defined, live = guests.of(item.ident if item.bus == "pci" else item.ids)
        item.claimed_by, item.lent_to = list(defined), list(live)


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

MARKS = {CANDIDATE: ("✓", "32"), WARN: ("!", "33"), REFUSE: ("✗", "31")}

# What the marks promise, kept beside them rather than inlined in report(),
# because guest/build.py draws this same table a second time (_usb_status) and
# an inline legend would leave that copy free to invent its own words.
#
# WORDED TO WHAT IS MEASURED, NOT TO THE IDEAL. "✓" cannot promise the host
# loses nothing: _host_cost() asks four questions, so this machine's unmounted
# 931G disk and the discrete GPU itself both come out "✓" while handing either
# over does cost the host something real. README.md already says it in the
# other direction -- "✓" means the host is not standing on the drive, not that
# the drive is empty -- and a legend that promised more would contradict it.
#
# The last sentence is the one worth printing: a "!" reads like a barrier and
# is not one. Measured, not promised -- guest/build.py refuses on REFUSE alone.
LEGEND = ("İşaretler — ✓ aday: ölçülen bir bedel yok · ! uyarılı: host, "
          "misafir koşarken bir işlevini\n"
          "kaybeder · ✗ devredilemez. "
          "Engelleyen tek işaret ✗'tir; ! uyarır, durdurmaz.")


def _print_item(item: Item) -> None:
    mark, colour = MARKS[item.verdict]
    group = f"grup {item.group:<3}" if item.group else " " * 8
    head = (f"  {paint(mark, colour)} {item.ident:<13} {item.ids:<10} "
            f"{group} {item.driver}")
    print(head)
    detail = f"      {item.title}"
    if item.note:
        detail += f" — {item.note}"
    print(detail)
    for reason in item.reasons:
        print(f"      {reason}")
    # Painted in a colour the verdict scheme never uses (32/33/31), because
    # this line says who wants the device and not whether it may move.
    if item.claimed_by:
        print(paint(f"      misafir tanımında: {', '.join(item.claimed_by)}", "36"))
    if item.lent_to:
        print(paint(f"      şu an misafirde: {', '.join(item.lent_to)}", "36"))


def _guest_header(guests) -> str:
    """One line saying how much of the "who wants it" answer is actually known.

    IT NAMES THE CONSEQUENCE, NOT JUST THE COUNT. An unread domain and a device
    nobody wants both print as a row with no claim line, and only one of those
    is a measurement -- so when something went unread the header says which
    lines may be short, rather than leaving the reader to treat silence as fact.
    """
    if not guests.known:
        return paint(f"sorulmadı — {guests.detail}", "33")
    if guests.unread:
        return paint(f"{guests.detail} — eksik okunan tanımların istekleri "
                     f"aşağıda görünmez", "33")
    if not guests.read:
        return guests.detail or "tanımlı domain yok"
    return f"{len(guests.read)} tanım okundu ({', '.join(guests.read)})"


def report(profile_name: str | None = None) -> int:
    """Read the machine and print what could move. Writes nothing, ever.

    IT REACHES LIBVIRT FROM HERE, AND ONLY FROM HERE. core.domains is asked once
    for who claims what, read-only and on a wall budget; every other line on the
    page comes from sysfs. The claim arrives after the items are built, so it
    annotates and never judges -- see annotate_claimants.
    """
    from . import domains
    from . import profile as profile_mod

    machine = probe.read_machine()
    p = (profile_mod.by_name(profile_name) if profile_name
         else profile_mod.select(machine.dmi_vendor, machine.dmi_product))

    print(f"Makine   : {machine.dmi_vendor or '?'} {machine.dmi_product or '?'}")
    print(f"Profil   : {p.name if p else paint('yok — yalnızca donanım okundu', '33')}")
    guests = domains.read()
    print(f"Misafir  : {_guest_header(guests)}")
    print()
    print(paint("Envanter yalnızca rapordur: v1 GPU'dan başka hiçbir cihazı "
                 "devretmez (K14).", "2"))
    print(paint(LEGEND, "2"))
    print()

    claims = host_storage_claims()
    usb_devices = probe.usb_devices()
    pci, bridges = pci_items(machine, p, claims, usb_devices)
    usb = usb_items(usb_devices)
    annotate_claimants(pci + usb, guests)

    print(paint("PCI — devir birimi IOMMU grubudur, cihaz değil", "1"))
    for item in pci:
        _print_item(item)
    print(f"      ({bridges} köprü/PCIe portu listelenmedi — topolojinin kendisi)")

    print()
    print(paint("USB — devir birimi cihazın kendisidir (vendor:product)", "1"))
    if usb:
        for item in usb:
            _print_item(item)
    else:
        print("      (cihaz yok)")

    print()
    counts = {v: len([i for i in pci + usb if i.verdict == v])
              for v in (CANDIDATE, WARN, REFUSE)}
    print(f"Aday {counts[CANDIDATE]} · uyarılı {counts[WARN]} · "
          f"devredilemez {counts[REFUSE]}")
    print()
    print("Bir USB denetleyicisini (PCI) devretmek üstündeki her cihazı "
          "birlikte götürür;\ntek bir USB cihazını devretmek götürmez. "
          "İkisi ayrı satırlar — karıştırılmaz.")
    print("\"misafir tanımında\" kalıcı tanımdan okunur ve domain kapalıyken de "
          "durur;\n\"şu an misafirde\" yalnız koşarken vardır — ödünç verilen "
          "aygıt misafir kapanınca geri gelir.")
    return 0
