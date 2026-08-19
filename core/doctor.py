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

GATE() LOOKS AT THE MACHINE, session_checks() LOOKS AT THE MOMENT. "Is this
machine of the class the design works on" is permanent; "is the compositor
holding the card right now" is not, and changes at the next boot. The second
question is asked and reported, but never behind the gate -- a compositor
check inside gate() would lock the gate against the tool's own users, since
selftest is meant to be run from a plain VT where there is no compositor to
measure at all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from . import hostfiles, lookingglass, probe, provenance, session
from .profile import Profile
from .term import paint

HARD, SOFT = "sert", "yumuşak"


@dataclass
class Check:
    key: str
    severity: str
    ok: bool | None          # None = could not be measured, which is not a failure
    title: str
    detail: str = ""
    remedy: str = ""

    @property
    def blocking(self) -> bool:
        # `is False` rather than `not ok`: an unanswerable question must never
        # close the gate. Saying "failed" where the honest answer is "could not
        # measure" is how a check earns the habit of being ignored.
        return self.severity == HARD and self.ok is False


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


def _cards(machine: probe.Machine,
           p: Profile | None) -> tuple[probe.PciDevice | None, probe.PciDevice | None]:
    """(discrete, integrated) as this machine and this profile see them.

    One derivation, used by the checks, by the session section and by anything
    later that needs to name the two cards -- so a subcommand cannot end up
    measuring a different pair than the gate allowed.
    """
    if p:
        dgpus, igpus = machine.by_ids(p.dgpu_ids), machine.by_ids(p.igpu_ids)
        return (dgpus[0] if dgpus else None), (igpus[0] if igpus else None)
    gpus = machine.gpus()
    candidates = [g for g in gpus if not g.boot_vga]
    dgpu = candidates[0] if candidates else None
    others = [g for g in gpus if dgpu is None or g.address != dgpu.address]
    return dgpu, (others[0] if others else None)


def _checks_with_profile(machine: probe.Machine, p: Profile) -> list[Check]:
    checks = [_check_iommu(), _check_kvm(machine)]

    dgpu, igpu = _cards(machine, p)
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
    dgpu, _ = _cards(machine, None)

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


def _check_lg_release() -> Check:
    """Do the two Looking Glass halves name the same release?

    SOFT, AND NEVER BEHIND THE GATE. A version mismatch does not stop the
    handover -- the card moves, the guest boots, everything install() writes is
    correct. What it stops is the picture, and it stops it in the shape of a
    broken passthrough: the client attaches and no frames arrive. That is the
    reason this line exists at all, since a failure that looks like the tool's
    own would otherwise be diagnosed by taking the working parts apart.

    IT ASKS THE QUESTION WITHOUT A GUEST. The pin is what `setup` would install,
    so the answer is available on a machine where no domain has ever been
    defined -- which is where a client upgrade lands. What the guest actually
    carries is a different reading, and it belongs where a guest is reachable
    (`vfioctl guest`); doctor writes nothing and starts nothing.
    """
    client = lookingglass.client_release()
    pin = lookingglass.read_pin()

    if pin.release and not pin.coherent:
        return Check(
            "lg-surum", SOFT, False, "Looking Glass sürümleri eşleşiyor",
            f"pin kendi içinde tutarsız — $Version = {pin.release}, "
            f"$Url = {pin.url or '(yok)'}",
            remedy=f"{lookingglass.PS1.name}: $Version, $Url ve $Sha256 aynı "
                   "sürümü göstermeli.",
        )

    ok = lookingglass.compare(client.release, pin.release)
    detail = f"istemci: {client.detail} | misafir pin'i: {pin.detail}"
    return Check(
        "lg-surum", SOFT, ok, "Looking Glass sürümleri eşleşiyor",
        detail,
        remedy="" if ok is not False else lookingglass.remedy(client.release, None),
    )


def _check_provenance() -> Check:
    """Is the code running here the code this machine has installed? (K20)

    SOFT, AND NEVER BEHIND THE GATE, for the same reason as the Looking Glass
    line above: a version difference does not make the handover wrong. What it
    makes wrong is every other line on this page, since they describe what the
    RUNNING copy would do, and the reader is looking at them to decide what the
    machine will do next.

    IT IS HERE BECAUSE `install --check` STRUCTURALLY CANNOT ASK IT. That check
    compares /etc against what the running code generates, so a stale copy
    calls the machine "identical" precisely when it is behind. The two
    questions read the same way and are opposites: one asks whether /etc
    matches this code, this one asks whether this code is the code that was
    installed.
    """
    r = provenance.describe()
    return Check(
        "surum", SOFT, r.same, "koşan kod ile kurulu paket aynı sürümde",
        r.detail, remedy=r.remedy,
    )


def _check_nvme_audit() -> Check:
    """Did the handover hook ever start a guest whose NVMe record went stale?

    THIS IS THE HALF THAT CLOSES THE LOOP. The hook audits every start -- every
    `virsh start`, virt-manager included -- and only logs, because a refusing
    check in that file that is wrong means no guest starts on the machine at
    all. Logging is only worth something if somebody reads it, and the only
    reader until now was `selftest`, i.e. the mismatch was visible exactly to
    the people who were already running a full round. Here it costs a line.

    SOFT, AND ON PURPOSE. What it reports is history, not a property of this
    machine: the address may well have been correct since. A hard check would
    close the gate over an entry that a later `nvme --detach`/`--attach` pair
    already fixed.

    None, NOT False, WHEN THE LOG CANNOT BE READ. An uninstalled hook, a fresh
    machine and a log this user cannot open are all "no answer" -- and one of
    them is the ordinary state of a card-less setup, where `install` never ran
    and the hook does not exist. Reporting that as a failure would teach the
    reader to skip the line.
    """
    log = hostfiles.HOOK_LOG
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return Check("nvme-denetimi", SOFT, None,
                     "hook günlüğünde NVMe kimlik uyuşmazlığı",
                     f"{log} okunamadı ({exc.strerror or exc})")

    faults = [l for l in lines
              if "nvme audit:" in l and ("MISMATCH" in l or "ABSENT" in l)]
    if not faults:
        audited = sum(1 for l in lines if "nvme audit:" in l)
        return Check("nvme-denetimi", SOFT, True,
                     "hook günlüğünde NVMe kimlik uyuşmazlığı",
                     f"{audited} denetim satırı, uyuşmazlık yok"
                     if audited else "hook henüz NVMe denetimi yazmamış")

    return Check(
        "nvme-denetimi", SOFT, False,
        "hook günlüğünde NVMe kimlik uyuşmazlığı",
        f"{len(faults)} satır; sonuncusu: {faults[-1].strip()}",
        remedy=f"kaydı tazele (guest --name <ad> nvme --detach/--attach) ya da "
               f"tamamını oku: grep 'nvme audit' {log}",
    )


def run_checks(machine: probe.Machine, p: Profile | None) -> list[Check]:
    checks = (_checks_with_profile(machine, p) if p
              else _checks_without_profile(machine))
    # Appended rather than folded into either list: neither is a question about
    # what this machine is -- one is two pieces of software agreeing, the other
    # is which copy of this tool is talking -- and gate() deliberately never
    # sees either.
    return checks + [_check_lg_release(), _check_provenance(),
                     _check_nvme_audit()]


# --------------------------------------------------------------------------- #
# the session half -- reported, never installed, never behind the gate
# --------------------------------------------------------------------------- #

SESSION_TITLE = "Seans — compositor iGPU'ya sabitlenmiş mi (yumuşak: kapıyı etkilemez)"


def session_checks(dgpu: str | None, igpu: str | None) -> list[Check]:
    """What the graphics session is doing with the card at this moment.

    DELIBERATELY NOT PART OF run_checks(), SO NOT PART OF gate(). See the
    module docstring: the gate answers a permanent question and this one does
    not. Every check here is soft, and an unanswerable one returns None rather
    than False -- from a plain VT, with a guest holding the card, there is
    simply nothing to read.

    WHY THE TOOL MEASURES THIS AT ALL RATHER THAN INSTALLING IT. The session
    configuration has a different owner (see core/session.py). Before this
    existed, install could write all eight files, print "Ölçüm ✓", and the
    first handover would still fail -- with nothing anywhere saying why until
    someone ran selftest. That is the project's own "a silent failure must not
    look like the default" rule, applied to the one condition it was missing.
    """
    checks: list[Check] = []

    s = session.active_session()
    if s is None:
        checks.append(Check(
            "seans", SOFT, None, "seat0'ta etkin bir grafik oturumu",
            "yok — bu makinede ölçülecek bir seans yarısı bulunamadı",
        ))
    else:
        checks.append(Check(
            "seans", SOFT, True, "seat0'ta etkin bir grafik oturumu",
            f"oturum {s.id}: {s.desktop or 'masaüstü adı bildirilmemiş'} "
            f"({s.type or 'tür bilinmiyor'}), lider pid {s.leader or '?'}",
        ))

    # The pin itself. Everything above is discovery; this is the criterion.
    dcard = probe.card_of(dgpu) if dgpu else None
    driver = probe.driver_of(dgpu) if dgpu else "(none)"
    screens = hostfiles.gpu_screens()

    if dgpu is None:
        checks.append(Check(
            "seans-pin", SOFT, None, "dGPU'yu tutan bir şey yok",
            "dGPU bulunamadı — tutulacak bir kart yok",
        ))
    elif driver == "vfio-pci":
        # The guest owns the device, so of course the session is not holding
        # it. Reading that as a pass would report the criterion as met on the
        # one occasion it cannot be tested.
        checks.append(Check(
            "seans-pin", SOFT, None, "dGPU'yu tutan bir şey yok",
            f"{dgpu} şu an vfio-pci'de (misafirde olabilir) — bu durumda "
            "seans zaten tutamaz, ölçüm anlamlı değil",
        ))
    else:
        held = session.card_holders(dcard)
        where = f"/dev/dri/{dcard} ya da /dev/nvidia*" if dcard else "/dev/nvidia*"
        if held:
            checks.append(Check(
                "seans-pin", SOFT, False, "dGPU'yu tutan bir şey yok",
                "TUTULUYOR: " + "; ".join(str(h) for h in held),
            ))
        elif screens:
            # The holder that an unprivileged fd scan cannot see: the display
            # manager's greeter runs as root. It says so in its own log.
            checks.append(Check(
                "seans-pin", SOFT, False, "dGPU'yu tutan bir şey yok",
                f"koşan Xorg'da {screens} NVIDIA GPU screen var — greeter'ın "
                "X sunucusu kartı açık tutuyor (20-vfio-no-autoaddgpu.conf "
                "yerinde değil ya da bu sunucu ondan önce başlamış)",
            ))
        else:
            # No Xorg clause here: a passing result already covers it (a
            # non-zero screen count is one of the two ways this check fails,
            # and says so in its own words), and install prints the X server's
            # own line just above this block.
            checks.append(Check(
                "seans-pin", SOFT, True, "dGPU'yu tutan bir şey yok",
                f"hiçbir süreç {where} tutmuyor",
            ))

    # The name the session half binds to. vfioctl writes the udev rule; the
    # compositor config that points at it does not, which is why a missing
    # link here is a warning rather than a failure -- install writes it.
    link = hostfiles.DEV_LINK
    icard = probe.card_of(igpu) if igpu else None
    if not link.is_symlink():
        checks.append(Check(
            "igpu-symlink", SOFT, False, f"{link} iGPU'yu gösteriyor",
            f"yok — `{provenance.command('install')}` yazar; seans yarısı bu "
            "ada bağlanır",
        ))
    else:
        target = os.path.basename(os.path.realpath(link))
        if icard is None:
            checks.append(Check(
                "igpu-symlink", SOFT, None, f"{link} iGPU'yu gösteriyor",
                f"→ {target}; iGPU'nun kartı okunamadı, karşılaştırılamıyor",
            ))
        else:
            checks.append(Check(
                "igpu-symlink", SOFT, target == icard,
                f"{link} iGPU'yu gösteriyor",
                f"→ {target}" + ("" if target == icard
                                 else f", oysa iGPU {igpu} = {icard}"),
            ))

    return checks


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

def _line(c: Check) -> str:
    if c.ok is None:
        mark, colour = "?", "36"
    elif c.ok:
        mark, colour = "✓", "32"
    elif c.severity == SOFT:
        mark, colour = "!", "33"
    else:
        mark, colour = "✗", "31"
    head = f"  {paint(mark, colour)} {c.title}"
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
        print(f"Profil   : {paint('bu makineyi üstlenen profil yok', '33')}")
    print()

    checks = run_checks(machine, p)
    for c in checks:
        print(_line(c))

    # The session half gets its own section rather than a line in the list
    # above, because it answers a different kind of question -- what is true
    # right now, not what this machine is -- and because what it prints when it
    # does not pass is the point of it.
    dgpu, igpu = _cards(machine, p)
    print()
    print(paint(SESSION_TITLE, "1"))
    session_results = session_checks(
        dgpu.address if dgpu else None, igpu.address if igpu else None)
    for c in session_results:
        print(_line(c))
    if any(c.ok is not True for c in session_results):
        print()
        print(session.CRITERION)
        print()
        print(session.ADDRESS)

    blocking = [c for c in checks if c.blocking]
    warnings = [c for c in checks if c.severity == SOFT and c.ok is False]
    print()

    if p is None:
        print(paint("Yazan alt komutlar koşmaz: profil eşleşmesi yok.", "33"))
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
        print(paint(f"KAPI KAPALI — {len(blocking)} sert ölçüt geçmedi.", "1;31"))
        for c in blocking:
            print(f"  - {c.title}: {c.detail}")
            if c.remedy:
                print(f"    {c.remedy}")
        print()
        print("Yazan alt komutlar koşmaz. Bunu geçen bir bayrak yok: yarı "
              "çalışan bir passthrough kurulumu, hiç kurulmamış olmaktan kötü.")
        return 1

    print(paint("KAPI AÇIK — sert ölçütlerin hepsi geçti.", "1;32"))
    if warnings:
        print(f"{len(warnings)} yumuşak uyarı var; engel değil:")
        for c in warnings:
            print(f"  - {c.title}: {c.detail}")
            # The remedy is printed here as well as on the blocking path: a
            # soft failure the reader cannot act on is a soft failure the
            # reader learns to scroll past.
            if c.remedy:
                for line in c.remedy.splitlines():
                    print(f"    {line}")
    if p.notes:
        print()
        print(p.notes)
    return 0
