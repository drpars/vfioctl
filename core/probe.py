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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PCI_DEVICES = Path("/sys/bus/pci/devices")
IOMMU_GROUPS = Path("/sys/kernel/iommu_groups")
DMI = Path("/sys/class/dmi/id")

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


def read_sysfs_value(path: str) -> str | None:
    """For profile-declared switches such as the ASUS MUX. Read only, and a
    missing file is an answer rather than an error: not every machine has one."""
    return _read(Path(path))
