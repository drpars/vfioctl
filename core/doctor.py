"""`vfioctl doctor` -- say what this machine is and whether the tool may write.

THIS IS THE ONLY SUBCOMMAND THAT RUNS EVERYWHERE. It writes nothing, binds
nothing, and never needs a profile to be useful: on an unrecognised machine it
falls back to discovery and reports what the hardware could support. That is
deliberate and it is the third way between a --force flag and a flat refusal.
A flat refusal gives someone porting the tool no information at all, and
--force gives them a broken setup; a report gives them the one thing they
actually need, which is the list of what does not match.

WHY THE FAILURES ARE SPLIT BY SEVERITY. A hard failure means the design cannot
work here -- a shared IOMMU group hands over a stranger's device, a machine
with no integrated GPU has nothing to keep the host alive. A soft failure is a
difference, not a defect: a kernel upgrade must never be able to stop the tool
on the machine it was written for. Only hard failures close the gate.

WHAT THE GATE PROTECTS. Everything that writes -- installing the handover
hook, defining a domain, moving the card -- calls gate() first. The check
lives here rather than in each subcommand so there is one answer to "may we
write on this machine", and so that adding a subcommand cannot accidentally
skip it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import probe
from .profile import Profile

HARD, SOFT = "sert", "yumuşak"


@dataclass
class Check:
    key: str
    severity: str
    ok: bool
    title: str
    detail: str = ""
    remedy: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == HARD and not self.ok


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #

def _check_iommu() -> Check:
    ok = probe.iommu_active()
    return Check(
        "iommu", HARD, ok, "IOMMU etkin",
        "gruplar var" if ok else "/sys/kernel/iommu_groups boş ya da yok",
        remedy="" if ok else (
            "AMD'de varsayılan açıktır; kapalıysa firmware'de IOMMU/SVM, "
            "gerekirse cmdline'da amd_iommu=on ya da intel_iommu=on."),
    )


def _check_kvm(machine: probe.Machine) -> Check:
    return Check(
        "kvm", HARD, machine.kvm, "/dev/kvm var",
        "" if machine.kvm else "düğüm yok",
        remedy="" if machine.kvm else
        "Sanallaştırma firmware'de kapalı olabilir (AMD-V / VT-x).",
    )


def _isolation(gpu: probe.PciDevice, expected: set[str]) -> tuple[bool, str]:
    """Is the card's IOMMU group exactly the card?

    Anything else in the group travels with it on handover, which is what ACS
    override exists to work around and what makes this a hard criterion: the
    tool cannot promise a clean handover it does not control.
    """
    if not gpu.iommu_group:
        return False, "cihazın IOMMU grubu yok"
    members = set(probe.iommu_group_members(gpu.iommu_group))
    strangers = sorted(members - expected)
    if strangers:
        return False, (f"grup {gpu.iommu_group} paylaşımlı; yabancı: "
                       + ", ".join(strangers))
    return True, f"grup {gpu.iommu_group} = yalnızca {', '.join(sorted(members))}"


def _checks_with_profile(machine: probe.Machine, p: Profile) -> list[Check]:
    checks = [_check_iommu(), _check_kvm(machine)]

    dgpus = machine.by_ids(p.dgpu_ids)
    dgpu = dgpus[0] if dgpus else None
    checks.append(Check(
        "dgpu", HARD, dgpu is not None, "dGPU bulundu",
        f"{dgpu.address} [{dgpu.ids}] sürücü={dgpu.driver or '-'}" if dgpu
        else f"profil {', '.join(p.dgpu_ids)} bekliyordu, bu makinede yok",
    ))

    if dgpu:
        audio = machine.audio_beside(dgpu)
        expected = {dgpu.address} | ({audio.address} if audio else set())
        ok, detail = _isolation(dgpu, expected)
        checks.append(Check("dgpu-izole", HARD, ok, "dGPU'nun IOMMU grubu izole",
                            detail,
                            remedy="" if ok else
                            "ACS override gerekir; v1 bunu desteklemiyor."))
        checks.append(Check(
            "dgpu-ses", HARD, audio is not None, "dGPU'nun ses fonksiyonu",
            f"{audio.address} [{audio.ids}]" if audio
            else "aynı slotta HDA fonksiyonu yok",
            remedy="" if audio else
            "Ses fonksiyonu grupla birlikte devredilir; yoksa misafirde ses yolu kurulamaz.",
        ))
        checks.append(Check(
            "dgpu-varyant", SOFT, dgpu.subsystem == p.dgpu_subsystem,
            "dGPU alt sistem kimliği profildekiyle aynı",
            f"bu makine {dgpu.subsystem or '-'}, profil {p.dgpu_subsystem or '-'}",
        ))

    igpus = machine.by_ids(p.igpu_ids)
    igpu = igpus[0] if igpus else None
    checks.append(Check(
        "igpu", HARD, igpu is not None, "iGPU var (host ekranını taşıyacak)",
        f"{igpu.address} [{igpu.ids}] sürücü={igpu.driver or '-'}" if igpu
        else f"profil {', '.join(p.igpu_ids)} bekliyordu, bu makinede yok",
        remedy="" if igpu else
        "Tek GPU'lu makineler kapsam dışı: host ekranını kaybeden başka bir tasarım.",
    ))

    if p.mux:
        value = probe.read_sysfs_value(p.mux.sysfs)
        ok = value == p.mux.required
        checks.append(Check(
            "mux", HARD, ok, "MUX doğru kipte",
            f"{p.mux.sysfs} = {value if value is not None else '(okunamadı)'}"
            + (f"; beklenen {p.mux.required} ({p.mux.meaning})" if not ok else
               f" ({p.mux.meaning})"),
            remedy="" if ok else
            "Diğer kipte dahili panel dGPU'ya bağlıdır; devir ekranı da götürür. "
            "Değişiklik yeniden başlatma ister.",
        ))

    if p.kernel_flavour:
        ok = p.kernel_flavour in machine.kernel
        checks.append(Check(
            "cekirdek", SOFT, ok, "çekirdek lezzeti profildekiyle aynı",
            f"{machine.kernel}" + ("" if ok else f"; profil '{p.kernel_flavour}' bekliyordu"),
        ))

    return checks


def _checks_without_profile(machine: probe.Machine) -> list[Check]:
    """Same hard questions, asked of the hardware instead of a profile.

    This is the porting path: no profile claims the machine, so nothing may be
    written, but the answers still say whether writing a profile would be
    worth anyone's time.
    """
    checks = [_check_iommu(), _check_kvm(machine)]

    gpus = machine.gpus()
    candidates = [g for g in gpus if not g.boot_vga]
    dgpu = candidates[0] if candidates else None

    checks.append(Check(
        "dgpu", HARD, dgpu is not None, "devredilebilecek bir dGPU var",
        f"{dgpu.address} [{dgpu.ids}] sürücü={dgpu.driver or '-'}" if dgpu
        else f"{len(gpus)} GPU görüldü, hiçbiri ayrık değil",
    ))
    if dgpu:
        audio = machine.audio_beside(dgpu)
        expected = {dgpu.address} | ({audio.address} if audio else set())
        ok, detail = _isolation(dgpu, expected)
        checks.append(Check("dgpu-izole", HARD, ok,
                            "dGPU'nun IOMMU grubu izole", detail))

    others = [g for g in gpus if dgpu is None or g.address != dgpu.address]
    checks.append(Check(
        "igpu", HARD, bool(others), "iGPU var (host ekranını taşıyacak)",
        ", ".join(f"{g.address} [{g.ids}]" for g in others) if others
        else "ikinci bir GPU yok",
    ))
    return checks


def run_checks(machine: probe.Machine, p: Profile | None) -> list[Check]:
    return _checks_with_profile(machine, p) if p else _checks_without_profile(machine)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

def gate(profile_name: str | None = None) -> tuple[bool, Profile | None, list[Check]]:
    """May a writing subcommand run here? One answer, one place.

    Callers in later phases (install, handover, domain definition) ask this and
    stop on False. They do not re-derive it, and there is no flag that turns a
    False into a True.
    """
    from . import profile as profile_mod

    machine = probe.read_machine()
    p = (profile_mod.by_name(profile_name) if profile_name
         else profile_mod.select(machine.dmi_vendor, machine.dmi_product))
    if p is None:
        return False, None, _checks_without_profile(machine)
    checks = _checks_with_profile(machine, p)
    return not any(c.blocking for c in checks), p, checks


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _line(c: Check) -> str:
    if c.ok:
        mark, colour = "✓", "32"
    elif c.severity == SOFT:
        mark, colour = "!", "33"
    else:
        mark, colour = "✗", "31"
    head = f"  {_paint(mark, colour)} {c.title}"
    return f"{head}\n      {c.detail}" if c.detail else head


def report(profile_name: str | None = None) -> int:
    machine = probe.read_machine()
    from . import profile as profile_mod

    if profile_name:
        p = profile_mod.by_name(profile_name)
        if p is None:
            print(f"HATA: '{profile_name}' diye bir profil yok.", file=sys.stderr)
            names = [x.name for x in profile_mod.all_profiles()]
            print(f"Var olanlar: {', '.join(names) or '(hiç)'}", file=sys.stderr)
            return 2
    else:
        p = profile_mod.select(machine.dmi_vendor, machine.dmi_product)

    print(f"Makine   : {machine.dmi_vendor or '?'} {machine.dmi_product or '?'}")
    print(f"Çekirdek : {machine.kernel}")
    if p:
        print(f"Profil   : {p.name} — {p.title}")
    else:
        print(f"Profil   : {_paint('bu makineyi üstlenen profil yok', '33')}")
    print()

    checks = run_checks(machine, p)
    for c in checks:
        print(_line(c))

    blocking = [c for c in checks if c.blocking]
    warnings = [c for c in checks if c.severity == SOFT and not c.ok]
    print()

    if p is None:
        print(_paint("Yazan alt komutlar koşmaz: profil eşleşmesi yok.", "33"))
        print("Yukarıdaki sert ölçütler geçiyorsa bu makine için profil "
              "yazmak anlamlı — profiles/ altına bir .toml.")
        if blocking:
            print()
            print("Önce şunlar çözülmeli:")
            for c in blocking:
                print(f"  - {c.title}: {c.detail}")
                if c.remedy:
                    print(f"    {c.remedy}")
        return 1

    if blocking:
        print(_paint(f"KAPI KAPALI — {len(blocking)} sert ölçüt geçmedi.", "1;31"))
        for c in blocking:
            print(f"  - {c.title}: {c.detail}")
            if c.remedy:
                print(f"    {c.remedy}")
        print()
        print("Yazan alt komutlar koşmaz. Bunu geçen bir bayrak yok: yarı "
              "çalışan bir passthrough kurulumu, hiç kurulmamış olmaktan kötü.")
        return 1

    print(_paint("KAPI AÇIK — sert ölçütlerin hepsi geçti.", "1;32"))
    if warnings:
        print(f"{len(warnings)} yumuşak uyarı var; engel değil:")
        for c in warnings:
            print(f"  - {c.title}: {c.detail}")
    if p.notes:
        print()
        print(p.notes)
    return 0
