"""Read what this machine actually is. Nothing here writes, binds or unbinds.

WHY A SEPARATE READING LAYER. The gate has to be trustworthy on a machine the
tool has never seen, which means the reading half must not depend on any
assumption the profile makes. So this module discovers -- every GPU, every
IOMMU group, whichever MUX switch exists -- and the profile only gets to say
what the answer should be. A probe that looked for the card it expected would
report "no discrete GPU" on a machine that has one in a different slot.

EVERYTHING IS READ FROM SYSFS, NOT FROM TOOLS. lspci, nvidia-smi and asusctl
are all absent or lying somewhere: a live ISO, a machine without the vendor
driver, a kernel that never loaded nvidia. sysfs is there whenever the kernel
is, and it is the same source the handover hook writes to.

PCI ADDRESSES ARE STABLE, CARD NUMBERS ARE NOT. /dev/dri/card* and connector
names shuffle between boots on this hardware (measured), so nothing here keys
off them; every DRM node is resolved back to its PCI address before it is
named. That trap cost three sessions once.

USB IS READ HERE TOO, BECAUSE IT IS A SECOND KIND OF HANDOVER UNIT. A PCI
device moves as its whole IOMMU group; a USB device moves on its own, by
vendor:product, with libvirt doing the detaching. Inventory needs both, and
the difference between them is the difference between handing over a
controller and handing over one thing plugged into it -- on this machine the
Bluetooth radio and the laptop's own keyboard sit on the same controller.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PCI_DEVICES = Path("/sys/bus/pci/devices")
IOMMU_GROUPS = Path("/sys/kernel/iommu_groups")
DMI = Path("/sys/class/dmi/id")
DRM_CLASS = Path("/sys/class/drm")
USB_DEVICES = Path("/sys/bus/usb/devices")

PCI_ADDRESS = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$")

# PCI class prefixes. 0x0300 is a VGA controller, 0x0403 the HDA audio function
# that shares an IOMMU group with a discrete GPU -- handing over one without the
# other leaves the guest with a card whose sound device is still on the host.
CLASS_VGA = "0x0300"
CLASS_AUDIO = "0x0403"


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


@dataclass
class PciDevice:
    address: str                 # 0000:01:00.0
    vendor: str                  # 10de
    device: str                  # 2520
    pci_class: str               # 0x030000
    driver: str | None           # nvidia, amdgpu, vfio-pci, None
    subsystem: str | None        # 1043:1b7c
    boot_vga: bool
    iommu_group: str | None      # "13"

    @property
    def ids(self) -> str:
        return f"{self.vendor}:{self.device}"

    @property
    def is_vga(self) -> bool:
        return self.pci_class.startswith(CLASS_VGA)

    @property
    def is_audio(self) -> bool:
        return self.pci_class.startswith(CLASS_AUDIO)

    @property
    def slot(self) -> str:
        """Everything but the function digit: 0000:01:00.0 -> 0000:01:00."""
        return self.address.rsplit(".", 1)[0]


def pci_devices() -> list[PciDevice]:
    out: list[PciDevice] = []
    if not PCI_DEVICES.is_dir():
        return out
    for entry in sorted(PCI_DEVICES.iterdir()):
        vendor = _read(entry / "vendor")
        device = _read(entry / "device")
        pci_class = _read(entry / "class")
        if not (vendor and device and pci_class):
            continue
        sub_v, sub_d = _read(entry / "subsystem_vendor"), _read(entry / "subsystem_device")
        driver = None
        link = entry / "driver"
        if link.is_symlink():
            driver = os.path.basename(os.path.realpath(link))
        group = None
        glink = entry / "iommu_group"
        if glink.exists():
            group = os.path.basename(os.path.realpath(glink))
        out.append(PciDevice(
            address=entry.name,
            vendor=vendor.removeprefix("0x"),
            device=device.removeprefix("0x"),
            pci_class=pci_class,
            driver=driver,
            subsystem=(f"{sub_v.removeprefix('0x')}:{sub_d.removeprefix('0x')}"
                       if sub_v and sub_d else None),
            boot_vga=_read(entry / "boot_vga") == "1",
            iommu_group=group,
        ))
    return out


def iommu_group_members(group: str) -> list[str]:
    """PCI addresses sharing one IOMMU group -- the unit a handover moves.

    A group with anything else in it is the whole reason ACS override exists,
    and the reason this is a hard criterion rather than a warning: handing over
    a group means handing over every device in it.
    """
    d = IOMMU_GROUPS / group / "devices"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def iommu_active() -> bool:
    return IOMMU_GROUPS.is_dir() and any(IOMMU_GROUPS.iterdir())


# --------------------------------------------------------------------------- #
# usb
# --------------------------------------------------------------------------- #

@dataclass
class UsbInterface:
    number: str              # 1-4:1.0
    usb_class: str           # e0
    driver: str | None       # btusb, usbhid, None
    inputs: list[str] = field(default_factory=list)   # "Asus Keyboard"


@dataclass
class UsbDevice:
    name: str                # 1-4 -- the sysfs name, and the bus path
    vendor: str              # 8087
    product: str             # 0032
    description: str | None  # what the device calls itself
    manufacturer: str | None
    busnum: int | None
    devnum: int | None
    controller: str | None   # PCI address of the xHCI it hangs off
    interfaces: list[UsbInterface] = field(default_factory=list)

    @property
    def ids(self) -> str:
        return f"{self.vendor}:{self.product}"

    @property
    def drivers(self) -> list[str]:
        return sorted({i.driver for i in self.interfaces if i.driver})

    @property
    def inputs(self) -> list[str]:
        return [name for i in self.interfaces for name in i.inputs]


def _pci_parent(path: Path) -> str | None:
    """The PCI address a sysfs node hangs off, by walking its real path up.

    A USB device's controller is not recorded anywhere as a field; it is the
    nearest PCI component of the path it lives at. Reading it this way means a
    machine with the radio on a different controller answers correctly without
    the tool being told where to look.
    """
    for part in reversed(os.path.realpath(path).split("/")):
        if PCI_ADDRESS.match(part):
            return part
    return None


def _usb_interfaces(device: Path) -> list[UsbInterface]:
    out: list[UsbInterface] = []
    for entry in sorted(device.glob(f"{device.name}:*")):
        driver = None
        link = entry / "driver"
        if link.is_symlink():
            driver = os.path.basename(os.path.realpath(link))
        # HID devices nest their input nodes under a hid child rather than
        # directly under the interface, so this looks at any depth. The names
        # are what makes a warning readable: "Asus Keyboard" says more about
        # what the host would lose than the interface class 03 does.
        inputs = sorted({n for n in (_read(p) for p in entry.glob("**/input/input*/name")) if n})
        out.append(UsbInterface(
            number=entry.name,
            usb_class=(_read(entry / "bInterfaceClass") or "?"),
            driver=driver,
            inputs=list(inputs),
        ))
    return out


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def usb_devices() -> list[UsbDevice]:
    """Every USB device that could be handed over, root hubs excluded.

    ROOT HUBS ARE NOT CANDIDATES AND ARE LEFT OUT. usbN is the controller's own
    hub -- the thing a PCI handover of the xHCI would move, not something
    libvirt can attach by vendor:product. Listing it beside real devices would
    invite exactly the confusion this module exists to prevent.
    """
    out: list[UsbDevice] = []
    if not USB_DEVICES.is_dir():
        return out
    for entry in sorted(USB_DEVICES.iterdir()):
        # Interfaces carry a colon; root hubs are named usb1, usb2, ...
        if ":" in entry.name or entry.name.startswith("usb"):
            continue
        vendor, product = _read(entry / "idVendor"), _read(entry / "idProduct")
        if not (vendor and product):
            continue
        out.append(UsbDevice(
            name=entry.name,
            vendor=vendor,
            product=product,
            description=_read(entry / "product"),
            manufacturer=_read(entry / "manufacturer"),
            busnum=_int(_read(entry / "busnum")),
            devnum=_int(_read(entry / "devnum")),
            controller=_pci_parent(entry),
            interfaces=_usb_interfaces(entry),
        ))
    return out


@dataclass
class Machine:
    dmi_vendor: str | None
    dmi_product: str | None
    kernel: str
    kvm: bool
    devices: list[PciDevice] = field(default_factory=list)

    def by_ids(self, wanted: list[str]) -> list[PciDevice]:
        return [d for d in self.devices if d.ids in wanted]

    def gpus(self) -> list[PciDevice]:
        return [d for d in self.devices if d.is_vga]

    def audio_beside(self, gpu: PciDevice) -> PciDevice | None:
        """The HDA function on the same PCI slot as a GPU, if it has one."""
        for d in self.devices:
            if d.is_audio and d.slot == gpu.slot and d.address != gpu.address:
                return d
        return None


def read_machine() -> Machine:
    return Machine(
        dmi_vendor=_read(DMI / "sys_vendor"),
        dmi_product=_read(DMI / "product_name"),
        kernel=os.uname().release,
        kvm=Path("/dev/kvm").exists(),
        devices=pci_devices(),
    )


def card_of(address: str) -> str | None:
    """The DRM card node of a PCI device, if it has one at this moment.

    The discrete card has none while a guest owns it, which is a normal state
    rather than a failure -- and the reason nothing here keys off card numbers.
    Connector directories (card0-DP-6) match the same glob but resolve to their
    card, not to a PCI address, so they cannot be mistaken for one.
    """
    for card in sorted(DRM_CLASS.glob("card[0-9]*")):
        device = card / "device"
        if device.exists() and os.path.basename(os.path.realpath(device)) == address:
            return card.name
    return None


def driver_of(address: str) -> str:
    """The driver bound to a PCI device right now, or "(none)"."""
    link = PCI_DEVICES / address / "driver"
    if link.is_symlink():
        return os.path.basename(os.path.realpath(link))
    return "(none)"


def read_sysfs_value(path: str) -> str | None:
    """For profile-declared switches such as the ASUS MUX. Read only, and a
    missing file is an answer rather than an error: not every machine has one."""
    return _read(Path(path))
