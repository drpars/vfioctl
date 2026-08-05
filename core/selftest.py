"""`vfioctl selftest` -- prove the handover works, with the desktop still alive.

WHY IT IS NOT CALLED `handover`. This command never touches the card. It starts
a guest and reads; the binding is done by the libvirt hook, running as root
under libvirtd, behind its own lsmod gate. A subcommand named after the thing
it does not do would invite a second writer to those sysfs paths one day, and a
second writer is how this machine got wedged three times. The hook is the only
one, deliberately.

WHY ROUNDS, PLURAL. One handover working proves almost nothing here. The step
that gives the card back used to poison the next attempt -- nvidia_drm came
back as a hotplug node the compositor grabbed, refcnt stuck at 1 -- so the
question is never "did it work" but "does it keep working without a reboot in
between". Five back-to-back rounds is the acceptance criterion this setup was
signed off with.

THE POLLER IS PART OF THE TEST, AND THE LOG HAS TO SAY SO. The first five
clean rounds proved only that the poisoning was gone: nothing in the run said
whether anything had been polling the card, and afterwards nobody could tell.
The two fixes that matter -- the hook waiting for the card, and the status-bar
module reading driver_override before it spawns nvidia-smi -- only have
anything to do while something is opening /dev/nvidia0 once a second. So this
records the poller instead of assuming it: it refuses to run when the poller is
absent or stopped, and prints its cpu delta each round. A stopped process burns
no cpu, and looks exactly like a working fix. "The bar was up" is a claim;
"the bar spent 1.4 s of cpu across five rounds" is a measurement.

WHICH POLLER IS AN ARGUMENT, NOT A CONSTANT. That it was waybar on this
machine is an accident. Anything short-lived that opens the card does the same
thing, so the name is a flag with a default, and --no-poller runs the
deliberately quiet baseline -- which answers the easy half of the question and
is not an acceptance run.

WHERE TO RUN IT. A shell that survives the graphics session dying, because that
is exactly the failure being hunted: a plain VT (Ctrl+Alt+F3) or ssh FROM
ANOTHER MACHINE. `ssh localhost` from a terminal inside the session is not one
-- the client dies with the terminal and the remote shell gets SIGHUP. The log
file is written for the same reason: the result has to survive the reader.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import hostfiles, install as install_mod, probe, session

LOG = Path("/tmp/vfioctl-selftest.log")
HOOK_LOG = Path("/var/log/vfio-hook.log")
PROC = Path("/proc")


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


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sh(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return out.returncode, (out.stdout + out.stderr).strip()


def _virsh(args: list[str], timeout: int = 60) -> tuple[int, str]:
    # Always -c qemu:///system: this user's default URI is the session daemon,
    # where the hooks, the default network and qemu.conf do not exist. A domain
    # reached over the wrong URI is silently a different machine.
    return _sh(["virsh", "-c", "qemu:///system", *args], timeout)


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def pid_of(name: str) -> str | None:
    rc, out = _sh(["pgrep", "-x", name], timeout=10)
    return out.splitlines()[0].strip() if rc == 0 and out.strip() else None


def cpu_jiffies(pid: str | None) -> int:
    """utime+stime straight from /proc, so a stopped process reads the same
    value twice. comm is stripped by cutting at the last ')' -- a process name
    containing a space would break a positional field split."""
    if not pid:
        return 0
    try:
        stat = (PROC / pid / "stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    rest = stat.rsplit(") ", 1)[-1].split()
    try:
        # After the state letter, utime and stime are fields 12 and 13 of the
        # original line, i.e. 11 and 12 counting from the state letter.
        return int(rest[11]) + int(rest[12])
    except (IndexError, ValueError):
        return 0


def proc_state(pid: str | None) -> str:
    """S/R while it runs, T once someone SIGSTOPs it, empty when gone."""
    if not pid:
        return ""
    try:
        stat = (PROC / pid / "stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    rest = stat.rsplit(") ", 1)[-1].split()
    return rest[0] if rest else ""


def own_card_holders() -> list[str]:
    """"comm(pid)" for processes of this user holding an nvidia char device.

    Root-owned holders are invisible here and that is correct: the hook stops
    nvidia-powerd itself before it unloads anything, so waiting for a process
    only the hook can end would never finish.

    The DRM node is deliberately not asked about: a compositor holding it does
    not let go by itself, so that is a refusal in preflight rather than
    something this waits for. Hence card_holders(None).
    """
    return [f"{h.comm}({h.pid})" for h in session.card_holders(None)]


# What the kernel and the session say when a handover goes wrong. NVRM lines are
# the honest signal the hook's own lsmod gate cannot give: "Attempting to remove
# device ... with non-zero usage count" means the card was still held, and it
# appears even on the runs where a later lsmod comes back clean -- because the
# kernel got there by killing the holder. The rest catch the failure this test
# exists for: the greeter's X server aborting and taking the session with it.
JOURNAL_SIGNALS = re.compile(
    r"NVRM|non-zero usage count|vfio|Xorg|caught signal|SIGABRT|libvirt",
    re.IGNORECASE,
)


def journal_since(since: str) -> list[str]:
    """Kernel and daemon lines worth reading after a round. Needs no root here:
    a member of wheel/adm reads the system journal."""
    rc, out = _sh(["journalctl", "--since", since, "--no-pager"], timeout=120)
    if rc != 0:
        return [f"(journalctl okunamadı: {out.splitlines()[0] if out else rc})"]
    return [line for line in out.splitlines() if JOURNAL_SIGNALS.search(line)]


def session_state() -> str:
    """The display manager and its X server -- the two that die together when a
    handover takes the greeter's card away."""
    sddm = _sh(["systemctl", "is-active", "sddm"], timeout=10)[1]
    xorg = _sh(["pgrep", "-c", "Xorg"], timeout=10)[1] or "0"
    return f"sddm={sddm} Xorg={xorg} süreç"


def hook_log_lines() -> int:
    try:
        return sum(1 for _ in HOOK_LOG.open("rb"))
    except OSError:
        return 0


def hook_log_since(count: int) -> list[str]:
    try:
        lines = HOOK_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[count:]


def gate_decision(domain: str) -> tuple[bool, str]:
    """Ask the installed hook what it would do with this domain. Writes nothing.

    A gate that never fires looks exactly like a hook that was never installed,
    which is why the hook has a check mode at all.
    """
    rc, xml = _virsh(["dumpxml", domain])
    if rc != 0:
        return False, f"dumpxml başarısız: {xml}"
    try:
        out = subprocess.run(
            [str(hostfiles.HOOK)], input=xml, text=True,
            capture_output=True, timeout=30,
            env={**os.environ, "VFIO_HOOK_CHECK": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    text = (out.stdout + out.stderr).strip()
    return "result:     handover" in out.stdout, text


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #

@dataclass
class Target:
    domain: str
    dgpu: str
    dgpu_audio: str | None
    compositor: str
    poller: str | None
    # How long a round gives the guest to become usable, and then to go away.
    # Both are generous on purpose: too short turns a working setup into a
    # failed round, which is the more expensive mistake -- it sends the reader
    # hunting a handover bug that is not there.
    boot_timeout: int = 300
    shutdown_timeout: int = 300


def preflight(target: Target) -> bool:
    """Every reason not to start the VM, asked before starting it."""
    ok = True
    print("\n== Preflight")
    print(f"   dGPU  {target.dgpu} → {probe.driver_of(target.dgpu)}")
    if target.dgpu_audio:
        print(f"   ses   {target.dgpu_audio} → "
              f"{probe.driver_of(target.dgpu_audio)}")
    print(f"   domain: {_virsh(['domstate', target.domain])[1]}")

    comp = pid_of(target.compositor)
    print(f"   {target.compositor} pid: {comp or '(yok)'}")
    # This shell's own session, not seat0's: the question here is whether the
    # caller is somewhere that survives the compositor dying, which is a
    # different question from the one core/session.py asks.
    session_id = os.environ.get("XDG_SESSION_ID", "")
    if session_id:
        print("   oturum: " + _sh(
            ["loginctl", "show-session", session_id, "-p", "Type", "-p", "Seat",
             "-p", "Active"], timeout=10)[1].replace("\n", " "))

    # nvidia-smi finds only its own process list, so it cannot answer "who holds
    # the card". The reverse holds though: if it shows something, that something
    # really is holding it -- this is what caught the X greeter.
    rc, smi = _sh(["nvidia-smi", "-q", "-d", "PIDS"], timeout=30)
    if rc == 0 and "None" not in smi:
        print("   nvidia-smi süreçleri: VAR  <-- kartı bir şey tutuyor")
        ok = False
    else:
        print("   nvidia-smi süreçleri: yok")

    screens = hostfiles.gpu_screens()
    if screens:
        print(f"   Xorg NVIDIA GPU screen: {screens}  <-- AutoAddGPU düzeltmesi "
              "yürürlükte değil")
        ok = False
    else:
        print(f"   Xorg NVIDIA GPU screen: {screens if screens == 0 else 'okunamadı'}")

    nodes = session.open_dev_nodes(comp)
    print(f"   {target.compositor} açık dri/nvidia fd'leri: "
          f"{' '.join(sorted(set(nodes))) or '(yok)'}")
    if any(n.startswith("/dev/nvidia") for n in nodes):
        print("      <-- compositor'ün açık bir nvidia düğümü var")
        ok = False

    # The KMS node is the other way to hold the card, and the one that is easy
    # to miss: nvidia-smi says None, no /dev/nvidia* fd exists, and nvidia_drm
    # still sits at refcnt=1. Resolve card* through PCI every time -- the minor
    # numbers are unstable and change across a handover.
    dcard = probe.card_of(target.dgpu)
    if dcard is None:
        print("   dGPU KMS düğümü: yok")
    elif f"/dev/dri/{dcard}" in nodes:
        print(f"   dGPU KMS düğümü: {dcard}  <-- COMPOSITOR TUTUYOR, devir düşer")
        print("      72-vfio-dgpu-no-uaccess.rules onu uzak tutmamış")
        ok = False
    else:
        print(f"   dGPU KMS düğümü: {dcard}, compositor tutmuyor")

    would, text = gate_decision(target.domain)
    for line in text.splitlines():
        print(f"   gate: {line}")
    if not would:
        ok = False

    print("\n== Preflight " + ("OK" if ok else "BAŞARISIZ — VM başlatılmıyor"))
    return ok


def settle(seconds: int = 15) -> bool:
    """Wait for this user's last nvidia holder to let go before starting.

    A `doctor`-style read a few seconds earlier can leave an nvidia-smi holding
    /dev/nvidia0 while the hook checks, and the handover is refused. The hook
    has its own wait for exactly this; closing the window here as well costs
    one scan when nothing holds the card.
    """
    held = own_card_holders()
    if not held:
        return True
    print(f"   bekleniyor: {' '.join(held)}")
    for i in range(seconds):
        time.sleep(1)
        held = own_card_holders()
        if not held:
            print(f"   kart {i + 1}s sonra boşaldı")
            return True
    print(f"   {seconds}s sonra HÂLÂ TUTULUYOR: {' '.join(held)}  <-- devir düşer")
    return False


# --------------------------------------------------------------------------- #
# a round
# --------------------------------------------------------------------------- #

def _wait_state(domain: str, wanted: str, seconds: int) -> str:
    deadline = time.monotonic() + seconds
    state = ""
    while time.monotonic() < deadline:
        state = _virsh(["domstate", domain], timeout=30)[1]
        if state == wanted:
            return state
        time.sleep(2)
    return state


def wait_for_agent(domain: str, seconds: int) -> int | None:
    """Seconds until the guest's agent answers, or None if it never did.

    WHY A ROUND CANNOT SKIP THIS. "running" means qemu is executing, not that
    the guest can do anything -- and an ACPI shutdown request sent to Windows
    while it is still booting is simply ignored. The first real run of this
    command failed exactly there: the handover was clean, the guest was fine,
    and the round was scored a failure because shutdown had been asked for
    seventeen seconds after power-on and nothing answered it (2026-08-05).

    A fixed sleep is the wrong fix and this project has the rule written down:
    waiting a set number of seconds and then polling is not a test. The guest
    answering is the event; how long it takes is the measurement.
    """
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        rc, _ = _virsh(
            ["qemu-agent-command", domain, '{"execute":"guest-ping"}'], timeout=30)
        if rc == 0:
            return int(time.monotonic() - started)
        time.sleep(5)
    return None


def one_round(target: Target, number: int, comp_before: str | None) -> str:
    """Empty string when the round was clean, otherwise why it was not."""
    print(f"\n\n=== TUR {number}  {_now()}")
    hook_before = hook_log_lines()
    started_at = _now()

    if not preflight(target):
        return "preflight geçmedi"
    print("\n== Settle")
    if not settle():
        return "settle: kartı tutan bir şey bırakmadı"

    print(f"\n== {target.domain} başlatılıyor")
    rc, out = _virsh(["start", target.domain], timeout=180)
    print(f"   virsh start exit={rc} {out}")
    verdict = ""
    if rc != 0:
        # A valid outcome if the hook could not free the card: the VM must not
        # start against a card the host still drives. The card's driver below
        # is the honest answer, not this exit code.
        verdict = f"start reddedildi: {out}"
    else:
        state = _wait_state(target.domain, "running", 60)
        driver = probe.driver_of(target.dgpu)
        comp_now = pid_of(target.compositor)
        print(f"   kart={driver} domain={state} {target.compositor}={comp_now}")
        if driver != "vfio-pci":
            verdict = f"start: kart '{driver}', beklenen vfio-pci"
        elif state != "running":
            verdict = f"start: domain '{state}', beklenen running"
        elif comp_now != comp_before:
            verdict = (f"start: compositor öldü ya da yeniden başladı "
                       f"({comp_before} → {comp_now})")

        # The card has moved and the desktop survived, which is what the
        # handover half of the round was asking. The guest becoming usable is
        # the next question, and shutdown may not be requested before it.
        booted = wait_for_agent(target.domain, target.boot_timeout)
        if booted is None:
            print(f"   ajan {target.boot_timeout}s içinde cevap vermedi")
            if not verdict:
                verdict = (f"misafir {target.boot_timeout}s'de kullanılabilir "
                           "hâle gelmedi (devir değil, misafir sorunu)")
        else:
            print(f"   ajan {booted}s sonra cevap verdi — misafir ayakta")

    # Give the card back even if start failed -- leaving it on vfio-pci would
    # make every later round meaningless, and shutdown is harmless on a domain
    # that is already off.
    print(f"\n== {target.domain} kapatılıyor")
    _virsh(["shutdown", target.domain], timeout=60)
    state = _wait_state(target.domain, "shut off", target.shutdown_timeout)
    time.sleep(5)
    driver = probe.driver_of(target.dgpu)
    comp_now = pid_of(target.compositor)
    print(f"   kart={driver} domain={state} {target.compositor}={comp_now}")
    if state != "shut off":
        # Order matters in the verdict as much as in the code: the card being
        # on vfio-pci here is a consequence, not the fault. release/end never
        # ran because the domain never ended, and saying "release/end did not
        # run" would send the next reader to the hook, which is innocent.
        if not verdict:
            verdict = (f"geri: misafir {target.shutdown_timeout}s'de kapanmadı "
                       f"(domain '{state}') — kart hâlâ misafirde")
        print(f"   !! misafir hâlâ çalışıyor ve kart onda. Zorla kapatma "
              f"yapılmadı (bilerek: '{target.domain}' korunuyor).")
        print(f"   !! elle: virsh -c qemu:///system shutdown {target.domain}")
    elif not verdict:
        if driver != "nvidia":
            verdict = (f"geri: kart '{driver}', beklenen nvidia — release/end "
                       "koşmadı")
        elif comp_now != comp_before:
            verdict = (f"geri: compositor öldü ya da yeniden başladı "
                       f"({comp_before} → {comp_now})")

    print(f"   masaüstü: {session_state()}")

    new_lines = hook_log_since(hook_before)
    waits = sum(1 for line in new_lines if "waiting up to" in line)
    print(f"\n== Hook günlüğü ({len(new_lines)} yeni satır, {waits} bekleme)")
    for line in new_lines:
        print(f"   {line}")

    signals = journal_since(started_at)
    print(f"\n== Journal — önemli satırlar ({len(signals)})")
    for line in signals[-40:]:
        print(f"   {line}")
    if not signals:
        print("   (hiçbiri = temiz)")
    return verdict


def run(rounds: int = 5, domain: str = "win11", compositor: str = "Hyprland",
        poller: str | None = "waybar", profile_name: str | None = None,
        assume_yes: bool = False, preflight_only: bool = False,
        boot_timeout: int = 300, shutdown_timeout: int = 300) -> int:
    """Run the rounds, or -- with preflight_only -- just the reading half.

    The read-only mode is not a convenience. It answers "would a handover work
    right now" without starting a guest, which is the question someone asks
    after a kernel upgrade or when the card is behaving oddly, and asking it
    should never cost a VM boot or take the desktop's GPU away.
    """
    from . import doctor

    sys.stdout = Tee(LOG)  # type: ignore[assignment]

    open_gate, p, _ = doctor.gate(profile_name)
    if not open_gate or p is None:
        print("Kapı kapalı — selftest koşmaz. vfioctl doctor")
        return 1
    layout = install_mod.resolve(probe.read_machine(), p)
    if layout is None:
        print("Adresler çözülemedi. vfioctl doctor")
        return 1
    if not hostfiles.HOOK.exists():
        print(f"{hostfiles.HOOK} yok — önce `vfioctl install`.")
        return 1

    if preflight_only:
        target = Target(domain, layout.dgpu, layout.dgpu_audio, compositor, poller,
                        boot_timeout, shutdown_timeout)
        ok = preflight(target)
        print(f"\n   günlük: {LOG}")
        return 0 if ok else 1

    # The guard, not a nag: inside the session this test cannot report its own
    # failure, because the failure it hunts kills the session.
    if not assume_yes and sys.stdin.isatty():
        tty = os.ttyname(sys.stdin.fileno()) if sys.stdin.isatty() else ""
        if tty.startswith("/dev/pts/") and not os.environ.get("SSH_CONNECTION"):
            print("Bu kabuk grafik oturumunun içindeki bir terminale benziyor, "
                  "düz bir VT değil.")
            print("Compositor test sırasında ölürse bu kabuk da onunla ölür.")
            if input("Yine de devam? [e/H] ").strip().lower() not in ("e", "y"):
                return 2

    poller_pid = pid_of(poller) if poller else None
    if poller:
        if not poller_pid:
            print(f"'{poller}' koşmuyor, yani kartı yoklayan bir şey olmayacak. "
                  "Bu, sorunun zaten cevaplanmış olan kolay yarısı. "
                  "Yoklayıcıyı başlatın ya da --no-poller verin.")
            return 2
        if proc_state(poller_pid) == "T":
            print(f"'{poller}' (pid {poller_pid}) durdurulmuş (state T) — bu eski "
                  "elle kaçış yolu ve buradaki her sonucu anlamsız kılar. "
                  "kill -CONT edin ya da --no-poller verin.")
            return 2

    target = Target(domain, layout.dgpu, layout.dgpu_audio, compositor, poller,
                        boot_timeout, shutdown_timeout)
    comp_before = pid_of(compositor)
    poller_j0 = cpu_jiffies(poller_pid)

    # Printed before anything can go wrong, not only in the summary: if the
    # session dies mid-run this line is how the reader finds what happened.
    print(f"\n\n########## {_now()}  {rounds} tur  profil={p.name}")
    print(f"günlük (paylaşılacak tek dosya): {LOG}")
    print(f"kart: {probe.driver_of(layout.dgpu)}   "
          f"domain: {_virsh(['domstate', domain])[1]}   "
          f"{compositor}: {comp_before}")
    print(f"yoklayıcı: {poller or '(yok)'} pid={poller_pid or '-'} "
          f"state={proc_state(poller_pid) or '-'}")

    results: list[tuple[int, str, int]] = []
    failed = 0
    for number in range(1, rounds + 1):
        j0 = cpu_jiffies(poller_pid)
        verdict = one_round(target, number, comp_before)
        delta = cpu_jiffies(poller_pid) - j0
        state = proc_state(poller_pid)
        print(f"\n== tur {number} yoklayıcı: state={state or 'gitti'} "
              f"cpu=+{delta} jiffies")
        if not verdict and poller:
            # Zero cpu means it was not polling, so the round says nothing about
            # the case this test exists for -- even if everything else passed.
            if not state:
                verdict = "yoklayıcı tur sırasında öldü — sonuç geçersiz"
            elif delta == 0:
                verdict = "yoklayıcı hiç cpu harcamadı — yoklamıyordu, sonuç geçersiz"
        results.append((number, verdict, delta))
        if verdict:
            failed = number
            print(f"\nTUR {number} BAŞARISIZ — duruluyor. {verdict}")
            break

    print(f"\n\n=== ÖZET  {_now()}")
    for number, verdict, delta in results:
        print(f"   tur {number}: {verdict or 'ok':<52} yoklayıcı cpu: +{delta}")
    print(f"\n   kart: {probe.driver_of(layout.dgpu)}   "
          f"domain: {_virsh(['domstate', domain])[1]}   "
          f"{compositor}: {pid_of(compositor)} (baştaki {comp_before})")
    if poller:
        print(f"   {poller}: state={proc_state(poller_pid) or 'gitti'} "
              f"toplam cpu: +{cpu_jiffies(poller_pid) - poller_j0} jiffies")
    print(f"   günlük: {LOG}   hook: {HOOK_LOG}")

    if failed:
        print(f"\n   SONUÇ: {rounds} turun {failed}. turunda düştü.")
        return 1
    print(f"\n   SONUÇ: {rounds}/{rounds} tur temiz, yoklayıcı boyunca canlıydı."
          if poller else
          f"\n   SONUÇ: {rounds}/{rounds} tur temiz (yoklayıcısız taban çizgisi).")
    return 0
