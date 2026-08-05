"""Hardware profiles: which machines this tool claims, and what must be true.

THE PROFILE IS A GATE, NOT A FINGERPRINT. Supporting a machine is a promise,
and a promise that cannot be kept is worse than a refusal -- a half-working
passthrough setup costs its owner a working desktop. So a profile carries the
conditions under which the writing subcommands are allowed to run, and the
tool refuses rather than guesses. There is deliberately no --force: the flag
would turn "we do not support this yet" into "you were warned", which is the
outcome the gate exists to prevent.

TWO SEVERITIES, BECAUSE ONE WOULD LOCK THE GATE ONTO ITSELF. Hard criteria are
the ones that make the design impossible when unmet -- an IOMMU group with a
stranger in it, no integrated GPU to keep the host alive. Soft criteria are
expectations, not requirements: the kernel flavour, the exact card revision. A
kernel upgrade must not stop the tool on the machine it was written for.

SELECTION IS BY DMI, MATCHING IS BY HARDWARE. Which profile applies is decided
by the machine's DMI strings; whether it applies is decided by probing. That
split is what makes `doctor` useful on an unknown machine: with no profile it
still reports what the hardware could do, which is exactly what someone
porting the tool needs to see.

FORMAT IS TOML, READ WITH THE STANDARD LIBRARY. tomllib has shipped with
Python since 3.11, so profiles need no dependency and stay declarative --
a profile is data, and data cannot have a bug that only fires on one machine.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass
class MuxRequirement:
    sysfs: str
    required: str
    meaning: str


@dataclass
class Profile:
    name: str
    title: str
    path: Path
    dmi_vendor: str | None
    dmi_products: list[str]
    dgpu_ids: list[str]
    dgpu_audio_ids: list[str]
    dgpu_subsystem: str | None
    igpu_ids: list[str]
    mux: MuxRequirement | None
    kernel_flavour: str | None
    notes: str

    def claims(self, dmi_vendor: str | None, dmi_product: str | None) -> bool:
        if self.dmi_vendor and dmi_vendor != self.dmi_vendor:
            return False
        return bool(dmi_product and dmi_product in self.dmi_products)


def load(path: Path) -> Profile:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    machine = data.get("machine", {})
    dgpu = data.get("dgpu", {})
    igpu = data.get("igpu", {})
    mux = data.get("mux")
    soft = data.get("soft", {})
    return Profile(
        name=data.get("name", path.stem),
        title=data.get("title", path.stem),
        path=path,
        dmi_vendor=machine.get("dmi_vendor"),
        dmi_products=machine.get("dmi_products", []),
        dgpu_ids=dgpu.get("ids", []),
        dgpu_audio_ids=dgpu.get("audio_ids", []),
        dgpu_subsystem=dgpu.get("subsystem"),
        igpu_ids=igpu.get("ids", []),
        mux=MuxRequirement(
            sysfs=mux["sysfs"],
            required=str(mux["required"]),
            meaning=mux.get("meaning", ""),
        ) if mux else None,
        kernel_flavour=soft.get("kernel_flavour"),
        notes=data.get("notes", "").strip(),
    )


def all_profiles(directory: Path | None = None) -> list[Profile]:
    directory = directory or PROFILE_DIR
    if not directory.is_dir():
        return []
    return [load(p) for p in sorted(directory.glob("*.toml"))]


def select(dmi_vendor: str | None, dmi_product: str | None,
           directory: Path | None = None) -> Profile | None:
    """The profile claiming this machine, or None.

    None is not a failure state, it is the normal state on every machine but
    the ones written down. What it forbids is writing, not looking.
    """
    for profile in all_profiles(directory):
        if profile.claims(dmi_vendor, dmi_product):
            return profile
    return None


def by_name(name: str, directory: Path | None = None) -> Profile | None:
    for profile in all_profiles(directory):
        if profile.name == name:
            return profile
    return None
