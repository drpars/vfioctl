"""`vfioctl install`, `install --check` and `uninstall` -- the host side.

ONE LIST, THREE VERBS. install writes the files in hostfiles.managed_files(),
--check compares them without writing, uninstall removes them. All three walk
the same list, so the tool cannot claim a file in one verb and forget it in
another -- a file uninstall leaves behind is a file that makes the next
install look like it succeeded.

WHY --check EXISTS AT ALL. This code was moved here from a distro installer
that had been writing these files for weeks, and the criterion for the move
was that the new version reproduces the working ones byte for byte. That
comparison is worth keeping afterwards rather than doing once: /etc drifts, a
package update drops a file back in, someone edits a rule from a VT at three
in the morning. --check is the question "is this machine still what we
installed", and it writes nothing, so it can be asked at any time.

PRECONDITIONS ARE MEASURED ON ARTIFACTS, NOT ON PACKAGE NAMES. "Is
looking-glass installed" is asked as "is there a client binary and a kvmfr
module", because that is the thing that has to be true; a package query
answers a different question and only on one distro. Missing pieces are named
with the Arch packages that carry them, as a hint rather than as the test.

THIS TOOL DOES NOT INSTALL PACKAGES. The Looking Glass halves come from the
AUR, and pulling them in would mean reproducing an AUR helper's discipline
here -- above all never passing --noconfirm, because the PKGBUILD diff prompt
is the one thing between a poisoned package and this machine. So install
refuses and prints the command instead. The alternative, writing config for a
module that does not exist, is the textbook silent failure: every file lands,
nothing works, and nothing says why.

WHAT INSTALL DOES NOT DO. It never binds, unbinds or probes a PCI device, and
it never loads or unloads the nvidia stack. Moving the card is the hook's job,
under libvirtd, behind its own lsmod gate; a second writer to those paths is
how this machine got wedged three times.
"""

from __future__ import annotations

import fcntl
import getpass
import os
import shutil
import stat
import subprocess
from difflib import unified_diff
from pathlib import Path

from . import doctor, hostfiles, probe, provenance, sysfile
from .hostfiles import Layout, Managed
from .profile import Profile
from .term import paint

# _IO('u', 0x44) from the kvmfr module's own header; it answers with the byte
# size the device was created with. A wrong number here would not raise -- it
# would quietly report "size unknown" for ever.
KVMFR_GETSIZE = ord("u") << 8 | 0x44

DRM_CLASS = Path("/sys/class/drm")
PCI_DEVICES = Path("/sys/bus/pci/devices")


def _say(text: str) -> None:
    print(f"\n{paint(text, '1')}")


def _ok(text: str) -> None:
    print(f"  {paint('✓', '32')} {text}")


def _warn(text: str) -> None:
    print(f"  {paint('!', '33')} {text}")


def _bad(text: str) -> None:
    print(f"  {paint('✗', '31')} {text}")


# --------------------------------------------------------------------------- #
# resolving what the templates need
# --------------------------------------------------------------------------- #

def resolve(machine: probe.Machine, p: Profile) -> Layout | None:
    """Turn "which card" (profile) into "which address" (this machine).

    Returns None only when the gate should already have refused; it is checked
    again rather than assumed, because this is the value that decides what is
    handed to VFIO.
    """
    dgpus = machine.by_ids(p.dgpu_ids)
    igpus = machine.by_ids(p.igpu_ids)
    if not dgpus or not igpus:
        return None
    dgpu, igpu = dgpus[0], igpus[0]
    if not dgpu.iommu_group:
        return None
    audio = machine.audio_beside(dgpu)
    members = probe.iommu_group_members(dgpu.iommu_group)
    expected = {dgpu.address} | ({audio.address} if audio else set())
    if set(members) - expected:
        return None
    return Layout(
        dgpu=dgpu.address,
        dgpu_audio=audio.address if audio else None,
        igpu=igpu.address,
        group=dgpu.iommu_group,
        group_members=members,
        profile=p.name,
        dgpu_ids=dgpu.ids,
        igpu_ids=igpu.ids,
    )


# --------------------------------------------------------------------------- #
# reading the results back
# --------------------------------------------------------------------------- #

def current_tags(card: str) -> list[str] | None:
    """Tags udev has on a DRM card right now; None if they cannot be read.

    CURRENT_TAGS, not TAGS: the first is what the device carries after the last
    event, the second includes tags from rules that no longer apply. Only the
    first can answer "did the rule take".
    """
    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property",
             "--property=CURRENT_TAGS", "--value", f"--path={DRM_CLASS / card}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # The value is colon-delimited on both ends: ":seat:master-of-seat:".
    return [tag for tag in out.stdout.strip().split(":") if tag]


def kvmfr_is_device() -> bool:
    """True when /dev/kvmfr0 is the character device the module created.

    QEMU creates it as an ordinary file when a VM starts before the module is
    loaded, and from then on the module cannot own that name. The symptom
    otherwise is a client failing to open a device that appears to be right
    there.
    """
    try:
        return stat.S_ISCHR(os.stat(hostfiles.KVMFR_NODE).st_mode)
    except OSError:
        return False


def kvmfr_loaded_mb() -> int | None:
    """MiB the running module gave the device, None when unknown.

    Not read from /sys/module/kvmfr/parameters: the module declares
    static_size_mb with permission 0000, so the kernel publishes no sysfs entry
    and that directory does not exist at all. The device answers instead, which
    is better anyway -- it reports the size in force rather than the size
    someone wrote in a file.
    """
    try:
        fd = os.open(hostfiles.KVMFR_NODE, os.O_RDWR)
    except OSError:
        return None
    try:
        return fcntl.ioctl(fd, KVMFR_GETSIZE) // 1024 // 1024
    except OSError:
        return None
    finally:
        os.close(fd)


def qemu_user_group() -> tuple[str | None, str | None]:
    """qemu.conf's user/group -- reported, never written.

    They belong to whatever made this machine a virtualisation host; passthrough
    is not the reason they exist. But when QEMU runs as root the guest's disk
    and ISOs have to live somewhere root can read, so a reader of this tool's
    output should be told which world they are in.
    """
    text = sysfile.read_text(hostfiles.QEMU_CONF) or ""
    user = group = None
    for line in text.splitlines():
        if line.startswith("user =") and user is None:
            user = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("group =") and group is None:
            group = line.split("=", 1)[1].strip().strip('"')
    return user, group


# --------------------------------------------------------------------------- #
# preconditions
# --------------------------------------------------------------------------- #

def _module_available(name: str) -> bool:
    try:
        return subprocess.run(
            ["modinfo", name], capture_output=True, timeout=20
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def preconditions() -> list[str]:
    """What must already be true before writing. Empty list means go ahead."""
    problems: list[str] = []

    if not (shutil.which("virsh") and hostfiles.QEMU_CONF.exists()):
        problems.append(
            "libvirt kurulu değil (virsh ya da /etc/libvirt/qemu.conf yok). "
            "Kurulum: sudo pacman -S libvirt qemu-desktop edk2-ovmf swtpm dnsmasq"
        )

    missing = []
    if not shutil.which("looking-glass-client"):
        missing.append(hostfiles.CLIENT_PKG)
    if not _module_available("kvmfr"):
        missing.append(hostfiles.MODULE_PKG)
    if missing:
        problems.append(
            "Looking Glass eksik: " + ", ".join(missing) + ". Bu araç paket "
            "kurmaz — kurulum: <aur-helper> -S --needed " + " ".join(missing)
            + "  (--noconfirm verilmez: PKGBUILD diff'i okunur)"
        )

    if hostfiles.KVMFR_NODE.exists() and not kvmfr_is_device():
        problems.append(
            f"{hostfiles.KVMFR_NODE} var ama karakter aygıtı değil — modül "
            "yüklenmeden bir VM başlamış ve QEMU onu düz dosya olarak yaratmış. "
            "Dosyayı silip modülü yükleyin."
        )

    sessions = hostfiles.x11_sessions()
    if sessions:
        problems.append(
            f"{hostfiles.XSESSIONS} boş değil ({' '.join(sessions)}). "
            "AutoAddGPU anahtarı global: X11 masaüstü sunan bir makinede o "
            "oturumların PRIME offload çıkışlarını da kapatır."
        )

    return problems


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

def _write_all(files: list[Managed]) -> tuple[int, set[str]]:
    rc = 0
    changed: set[str] = set()
    for m in files:
        if not m.path.parent.is_dir():
            rc |= sysfile.run(["sudo", "mkdir", "-p", str(m.path.parent)])
        write_rc, moved = sysfile.write_with_backup(m.path, m.content, m.backup)
        rc |= write_rc
        if moved:
            changed.add(m.key)
            if m.reload:
                changed.add(f"reload:{m.reload}")
        # libvirt silently skips a hook without the execute bit -- no error, no
        # log line, indistinguishable from not having installed it. Re-assert
        # the mode whenever it is missing, not only when the content changed.
        if m.mode and (moved or not os.access(m.path, os.X_OK)):
            rc |= sysfile.run(["sudo", "chmod", m.mode, str(m.path)])
    return rc, changed


def _allow_kvmfr_in_libvirt() -> tuple[int, bool]:
    text = sysfile.read_text(hostfiles.QEMU_CONF)
    if text is None:
        _bad(f"{hostfiles.QEMU_CONF} okunamadı")
        return 1, False
    if hostfiles.acl_allows_kvmfr(text):
        _ok(f"cgroup_device_acl zaten {hostfiles.ACL_DEVICE} içeriyor")
        return 0, False
    updated = hostfiles.acl_with_kvmfr(text)
    if updated is None:
        _bad(f"{hostfiles.QEMU_CONF}: düzenlenebilir bir {hostfiles.ACL_KEY} "
             "bloğu yok — elle eklenmeli, tahmin edilmiyor")
        return 1, False
    rc, changed = sysfile.write_with_backup(hostfiles.QEMU_CONF, updated)
    if changed:
        _ok(f"{hostfiles.ACL_DEVICE} → cgroup_device_acl")
    return rc, changed


def install(profile_name: str | None = None, kvmfr_mb: int | None = None) -> int:
    open_gate, p, checks = doctor.gate(profile_name)
    if not open_gate or p is None:
        print("Kapı kapalı — kurulum koşmaz. Ayrıntı için: "
              f"{provenance.command('doctor')}")
        for c in checks:
            if c.blocking:
                print(f"  - {c.title}: {c.detail}")
        return 1

    machine = probe.read_machine()
    layout = resolve(machine, p)
    if layout is None:
        print("HATA: profil geçti ama adresler çözülemedi. "
              f"{provenance.command('doctor')}")
        return 1

    problems = preconditions()
    if problems:
        _say("Önkoşullar karşılanmadı — hiçbir şey yazılmadı")
        for problem in problems:
            _bad(problem)
        return 1

    size, basis = hostfiles.kvmfr_size(kvmfr_mb)
    if size is None:
        _bad("kvmfr boyutu hesaplanamadı: bağlı ekran yok. --kvmfr-mb ile verin.")
        return 1

    files = hostfiles.managed_files(layout, getpass.getuser(), size, basis)

    _say(f"Profil {p.name} — dGPU {layout.dgpu}"
         f"{' + ' + layout.dgpu_audio if layout.dgpu_audio else ''}, "
         f"iGPU {layout.igpu}, IOMMU grubu {layout.group}")

    _say("Dosyalar")
    rc, changed = _write_all(files)

    _say("libvirt cgroup ACL")
    acl_rc, acl_changed = _allow_kvmfr_in_libvirt()
    rc |= acl_rc

    if "reload:udev" in changed:
        _say("udev")
        rc |= sysfile.run(["sudo", "udevadm", "control", "--reload-rules"])
        # --settle so the checks below read the result, not the race.
        rc |= sysfile.run(
            ["sudo", "udevadm", "trigger", "--settle", "--subsystem-match=drm"])
        rc |= sysfile.run(["sudo", "udevadm", "trigger", "--subsystem-match=kvmfr"])

    # The daemon reads its hook directory when it starts; a file dropped next to
    # a running libvirtd is ignored until then. try-restart rather than restart
    # because libvirtd is socket-activated here and usually is not running --
    # and vfio.conf needs no restart at all, the hook reads it on every call.
    if "hook" in changed or acl_changed:
        _say("libvirtd")
        rc |= sysfile.run(["sudo", "systemctl", "try-restart", "libvirtd.service"])

    rc |= verify(layout, size)
    return rc


# --------------------------------------------------------------------------- #
# verification -- what was read back, not what was written
# --------------------------------------------------------------------------- #

def verify(layout: Layout, wanted_mb: int | None = None) -> int:
    """Read the machine back. A successful write is a claim; this is evidence."""
    rc = 0
    _say("Ölçüm")

    if hostfiles.DEV_LINK.is_symlink():
        _ok(f"{hostfiles.DEV_LINK} → {os.path.realpath(hostfiles.DEV_LINK)}")
    else:
        _bad(f"{hostfiles.DEV_LINK} yok — kural diskte ama udev uygulamadı")
        rc |= 1

    dcard = probe.card_of(layout.dgpu)
    if dcard is None:
        # No KMS node to measure: the card is on vfio-pci, or nvidia_drm is not
        # loaded. The rule is on disk and applies when the node next appears.
        _warn(f"dGPU'nun KMS düğümü şu an yok (sürücü: {probe.driver_of(layout.dgpu)}) "
              "— kural diskte, düğüm doğduğunda uygulanır")
    else:
        tags = current_tags(dcard)
        if tags is None:
            _warn(f"{dcard}: CURRENT_TAGS okunamadı")
        elif [tag for tag in (*hostfiles.SEAT_TAGS, hostfiles.UACCESS_TAG)
              if tag in tags]:
            _bad(f"{dcard} hâlâ seat etiketli ({' '.join(tags)}) — kural "
                 "uygulanmıyor; dosya adına ve adrese bakın")
            rc |= 1
        else:
            _ok(f"{dcard} seat envanterinin dışında")

    # The other half of the claim, and the one that costs a login screen if it
    # is wrong: seat0 still has a card carrying master-of-seat.
    icard = probe.card_of(layout.igpu)
    itags = current_tags(icard) if icard else None
    if itags is None:
        _warn("iGPU'nun etiketleri okunamadı")
    elif hostfiles.SEAT_TAGS[1] in itags:
        _ok(f"{icard} master-of-seat taşıyor — seat0 ayakta")
    else:
        _bad(f"{icard} master-of-seat taşımıyor ({' '.join(itags)})")
        rc |= 1

    screens = hostfiles.gpu_screens()
    if screens is None:
        _warn("Xorg günlüğü okunamadı — dosya yerinde, etkisi sonraki boot'ta")
    elif screens == 0:
        _ok("Xorg'un NVIDIA GPU screen'i yok")
    else:
        _warn(f"koşan Xorg'da {screens} NVIDIA GPU screen var — dosya yerinde "
              "ama bu sunucu ondan önce başlamış; yeniden başlatmada düşer")

    # THE SESSION HALF: WARNED ABOUT, NOT REFUSED. It is not this tool's to
    # write (core/session.py says why), so it is measured here the same way
    # the running X server above is. Refusing would be backwards: a compositor
    # holding the card right now is usually what this very install fixes --
    # until a moment ago the seat rule was not on disk. The symlink check is
    # skipped because the block at the top of this function already read that
    # file, which is one vfioctl writes.
    session_results = [c for c in doctor.session_checks(layout.dgpu, layout.igpu)
                       if c.key != "igpu-symlink"]
    for c in session_results:
        text = f"{c.title}: {c.detail}" if c.detail else c.title
        (_ok if c.ok is True else _warn)(text)
    if any(c.ok is not True for c in session_results):
        print("      Ölçüt ve nereye kurulacağı: "
              f"`{provenance.command('doctor')}`")

    if kvmfr_is_device():
        loaded = kvmfr_loaded_mb()
        if loaded is None:
            _warn(f"{hostfiles.KVMFR_NODE} var, boyutu okunamadı")
        elif wanted_mb is not None and loaded != wanted_mb:
            # An already-loaded module keeps the size it was loaded with;
            # modprobe will not reapply the parameter and will not complain.
            _bad(f"kvmfr {loaded} MiB ile yüklü, istenen {wanted_mb} MiB — "
                 "yeniden başlatma (ya da modül boşaltma) gerekiyor")
            rc |= 1
        else:
            _ok(f"{hostfiles.KVMFR_NODE} = {loaded} MiB")
            if wanted_mb:
                print(f"      domain XML'inde size={wanted_mb * 1024 * 1024} "
                      "olmalı; farklıysa misafir açılır ve hiç kare gelmez")
    else:
        _warn(f"{hostfiles.KVMFR_NODE} yok — `sudo modprobe kvmfr` ya da "
              "yeniden başlatma")

    user, group = qemu_user_group()
    print(f"      qemu.conf user/group = {user or '?'}/{group or '?'} "
          "(bu araç yazmaz, yalnızca bildirir)")

    for address in layout.group_members:
        print(f"      {address} → {probe.driver_of(address)}")
    return rc


# --------------------------------------------------------------------------- #
# check -- the same list, read only
# --------------------------------------------------------------------------- #

def _has_code_change(diff_body: list[str]) -> bool:
    """Does a unified diff touch anything but comments and blank lines?

    Worth distinguishing, because the move from the old owner to this tool
    rewrote the provenance headers of every file: a diff that is all comments
    means the behaviour on this machine is byte-identical to the one that was
    measured working, which is exactly the criterion the move had to meet.
    """
    for line in diff_body:
        if line[:1] not in "+-" or line.startswith(("+++", "---")):
            continue
        body = line[1:].strip()
        if body and not body.startswith("#"):
            return True
    return False


def check(profile_name: str | None = None, kvmfr_mb: int | None = None) -> int:
    """Compare what is on disk with what install would write. Writes nothing."""
    open_gate, p, _ = doctor.gate(profile_name)
    if p is None:
        print("Bu makineyi üstlenen profil yok — karşılaştıracak bir hedef yok.")
        return 1

    machine = probe.read_machine()
    layout = resolve(machine, p)
    if layout is None:
        print("Adresler çözülemedi. "
              f"{provenance.command('doctor')}")
        return 1

    size, basis = hostfiles.kvmfr_size(kvmfr_mb)
    if size is None:
        print("kvmfr boyutu hesaplanamadı (bağlı ekran yok); --kvmfr-mb verin.")
        return 1

    files = hostfiles.managed_files(layout, getpass.getuser(), size, basis)

    _say(f"Kurulu dosyalar vs. vfioctl {p.name} (yazılmadı)")
    drift = 0
    for m in files:
        current = sysfile.read_text(m.path)
        if current is None:
            _bad(f"{m.path} — yok")
            print(f"      {m.what}")
            drift += 1
        elif current == m.content:
            _ok(f"{m.path} — birebir aynı")
        else:
            _warn(f"{m.path} — farklı")
            body = list(unified_diff(
                current.splitlines(keepends=True),
                m.content.splitlines(keepends=True),
                fromfile="kurulu", tofile="vfioctl", n=1,
            ))
            for line in body:
                print("      " + line.rstrip("\n"))
            drift += 1
            if not _has_code_change(body):
                print("      (yalnızca yorum satırları — davranış aynı)")

    text = sysfile.read_text(hostfiles.QEMU_CONF) or ""
    if hostfiles.acl_allows_kvmfr(text):
        _ok(f"{hostfiles.QEMU_CONF}: cgroup_device_acl {hostfiles.ACL_DEVICE} içeriyor")
    else:
        _bad(f"{hostfiles.QEMU_CONF}: cgroup_device_acl {hostfiles.ACL_DEVICE} içermiyor")
        drift += 1

    if not open_gate:
        _warn("kapı kapalı: bu makinede `install` koşmaz "
              f"({provenance.command('doctor')})")

    print()
    if drift == 0:
        print(paint("Makine kurulu hâliyle birebir aynı.", "1;32"))
        return 0
    print(f"{paint(f'{drift} fark var.', '33')} "
          f"`{provenance.command('install')}` bunları yazar.")
    return 1


# --------------------------------------------------------------------------- #
# uninstall
# --------------------------------------------------------------------------- #

def uninstall(profile_name: str | None = None, kvmfr_mb: int | None = None) -> int:
    """Remove what install wrote. Does not touch modules, and says so.

    WHAT IT DELIBERATELY DOES NOT DO. It does not unload vfio-pci, reload the
    nvidia stack or write to unbind/drivers_probe. Those writes are the ones
    that spun the kernel's remove() path three times on this machine, and a
    reboot puts the card back for free -- so this clears driver_override, which
    is a plain attribute write, and lets the next boot do the rest.

    WHY IT REFUSES WHILE THE CARD IS ON vfio-pci. That state means a guest may
    own the device right now. Deleting the hook underneath a running handover
    leaves nothing to give the card back at release/end.

    AND THE WARNING THAT OUTLIVES IT: removing these files without putting
    something in their place does not return the machine to "before" -- it
    returns it to a machine whose passthrough setup is no longer reproducible.
    """
    open_gate, p, _ = doctor.gate(profile_name)
    if p is None:
        print("Profil yok — bu makinede kurulmuş bir şey iddia edilmiyor.")
        return 1

    machine = probe.read_machine()
    layout = resolve(machine, p)
    if layout is None:
        print("Adresler çözülemedi. "
              f"{provenance.command('doctor')}")
        return 1

    on_vfio = [a for a in layout.group_members if probe.driver_of(a) == "vfio-pci"]
    if on_vfio:
        _bad("kart şu an vfio-pci'de (" + " ".join(on_vfio) + ") — misafir "
             "çalışıyor olabilir. Önce misafiri kapatın.")
        return 1

    size, basis = hostfiles.kvmfr_size(kvmfr_mb)
    files = hostfiles.managed_files(
        layout, getpass.getuser(), size or 0, basis)

    _say("Silinen dosyalar")
    rc = 0
    removed_any = False
    for m in files:
        remove_rc, removed = sysfile.sudo_remove(m.path)
        rc |= remove_rc
        removed_any |= removed

    # BACKUPS ARE LISTED, NEVER DELETED, AND THE RULE IS ONE SENTENCE: a .bak
    # is not ours. It is a snapshot of whatever was at that path before this
    # tool first wrote there -- on a machine that had a setup from something
    # else, it is the only remaining copy of it, and the file it belonged to
    # has just been deleted. So removing them would be the one destructive
    # thing in a command whose whole job is to undo.
    #
    # Naming them is not optional either. An unexplained .bak in /etc/udev is
    # exactly the leftover that makes the next reader think the tool half-ran.
    # Measured 2026-08-05: an earlier version deleted the hook's backup and
    # silently left six others.
    leftovers = [
        path for path in (
            [m.backup or m.path.with_suffix(m.path.suffix + ".bak") for m in files]
            + [hostfiles.HOOK_BACKUP,
               hostfiles.QEMU_CONF.with_suffix(
                   hostfiles.QEMU_CONF.suffix + ".bak")]
        ) if path.exists()
    ]

    _say("libvirt cgroup ACL")
    text = sysfile.read_text(hostfiles.QEMU_CONF)
    if text is None:
        _warn(f"{hostfiles.QEMU_CONF} okunamadı")
    else:
        stripped = hostfiles.acl_without_kvmfr(text)
        if stripped is None:
            _ok(f"{hostfiles.ACL_DEVICE} zaten listede değil")
        else:
            write_rc, _ = sysfile.write_with_backup(hostfiles.QEMU_CONF, stripped)
            rc |= write_rc
            _ok(f"{hostfiles.ACL_DEVICE} listeden çıkarıldı "
                "(bloğun kalanına dokunulmadı)")

    _say("driver_override")
    for address in layout.group_members:
        override = PCI_DEVICES / address / "driver_override"
        if override.exists():
            # A pure attribute write: it detaches nothing and probes nothing.
            # Left behind it outlives this tool -- nvidia could never bind the
            # card again and nothing on the machine would say why. Written
            # through tee rather than a shell redirect so no path is ever
            # handed to a shell.
            rc |= sysfile.sudo_write(override, "\n")
        print(f"      {address} → {probe.driver_of(address)}")

    if removed_any:
        _say("udev")
        rc |= sysfile.run(["sudo", "udevadm", "control", "--reload-rules"])
        rc |= sysfile.run(
            ["sudo", "udevadm", "trigger", "--settle", "--subsystem-match=drm"])
        _say("libvirtd")
        rc |= sysfile.run(["sudo", "systemctl", "try-restart", "libvirtd.service"])

    if leftovers:
        _say("Bırakılan yedekler — silinmedi, bilerek")
        for path in leftovers:
            print(f"      {path}")
        print("      Bunlar vfioctl'in değil: her biri, o yola vfioctl ilk kez "
              "yazmadan önce orada ne varsa onun kopyası.")
        print("      İşlevsel etkileri yok (udev yalnızca *.rules, Xorg *.conf, "
              "modprobe.d *.conf okur). Elle silinebilirler.")

    print()
    print("Kart bu oturumda vfio-pci'ye bağlı değilse zaten nvidia'da; kvmfr "
          "modülü yüklü kalır. Tam temizlik için yeniden başlatın.")
    print("Not: yerine bir şey konmadan kaldırmak makineyi 'eski hâline' değil, "
          "passthrough kurulumu artık yeniden üretilemeyen bir hâle getirir.")
    return rc
