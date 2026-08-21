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

WHICH GUEST IS AN ARGUMENT WITH NO DEFAULT, AND THE ASYMMETRY IS THE POINT.
--rounds, --compositor and --poller default because they say HOW to test.
--domain says WHAT, and a tool that picks its own target can end up measuring
something other than what the reader believes it measured. The default used to
be `win11`, the guest this was written against; when the working guest moved to
`win11-nvme` the old name kept resolving, so a bare `selftest` would have run
five rounds against a guest nobody uses and reported it as the acceptance
criterion, with nothing in the output naming which one it took. A name in the
source protects the machine it was written on and nothing else. So a missing
--domain is a refusal that prints the defined domains, on the same rule as the
missing tty below: a question nobody answered is not a yes.

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

from . import (domains, hostfiles, install as install_mod, probe,
               provenance, session)

# Where this goes and why is hostfiles.state_log; it is one rule for every
# round that writes a transcript, and this file is where it was measured.
LOG = hostfiles.state_log("selftest.log")
HOOK_LOG = hostfiles.HOOK_LOG
PROC = Path("/proc")

# TASK_COMM_LEN - 1: the kernel stores at most 15 bytes of a process name, and
# every name-based matcher compares against that truncation, /proc/<pid>/comm
# and `pgrep -x` alike. Measured (procps-ng 4.0.6): `pgrep -x systemd-journald`
# warns and exits 1, `pgrep -x systemd-journal` finds it.
COMM_MAX = 15

# What timeout(1) reports, reused here so a command that never answered is
# distinguishable from one that answered badly. The difference decides a
# verdict: `virsh domstate` printing "running" is a guest that is still up,
# `virsh domstate` never printing anything is a libvirtd blocked somewhere --
# and in this command's worst case it is blocked in its own hook, waiting on
# the rebind. Collapsing the two made the tool report a wedged rebind as "the
# guest did not shut down" (measured 2026-08-18).
TIMED_OUT = 124

# Names -- kernel-truncated, see COMM_MAX -- of the processes that carry an
# nvidia device through its init. A task of any of them in state D is what a
# wedged rebind looks like from userspace.
NVIDIA_INIT_COMMS = frozenset({
    "modprobe", "insmod", "rmmod", "nvidia-smi", "nvidia-modprobe",
    "nvidia-persiste", "nvidia-powerd",
})


class Tee:
    """stdout that also lands in a file, so a dying session cannot eat the run."""

    def __init__(self, path: Path):
        self.stream = sys.stdout
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
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

    def isatty(self) -> bool:
        """False even when the terminal half is one, so nothing paints.

        Half of this stream is the log file, and that file is the artifact the
        whole command exists to leave behind: the copy that survives the
        graphics session dying mid-round and gets read from somewhere else.
        Escape codes written for the terminal land in it as well.
        """
        return False


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sh(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Caught before SubprocessError, which it inherits from, because the
        # two mean opposite things here -- see TIMED_OUT.
        return TIMED_OUT, f"{cmd[0]}: {timeout}s içinde cevap vermedi"
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
    """Lowest pid whose name matches, compared the way the kernel stores it.

    Read from /proc instead of shelling out to `pgrep -x`, because the name has
    to be compared against its own truncation and pgrep cannot be asked to do
    that. The kernel keeps at most COMM_MAX bytes of a process name, so a
    longer name matched NOTHING: pgrep printed a warning and exited 1, and this
    function handed that back as a plain None.

    That None is the expensive kind of wrong. one_round() decides the
    compositor survived by comparing comp_before with comp_now, so two Nones
    compare equal and the check disappears instead of firing -- a wrong "alive"
    verdict, which is worse than no verdict. It does not fire on this machine
    (Hyprland is 8 characters); it would fire silently on a longer name.

    Matching ourselves is not a risk the way `pgrep -f <name>` would be: comm
    holds this process's own name, never the pattern it is looking for.
    """
    wanted = name[:COMM_MAX]
    for pid in sorted(int(entry.name) for entry in PROC.glob("[0-9]*")):
        try:
            comm = (PROC / str(pid) / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue  # exited between the glob and the read
        if comm == wanted:
            return str(pid)
    return None


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


def d_state_procs() -> list[tuple[str, str]]:
    """(comm, pid) for every task the kernel has in uninterruptible sleep.

    D is the state worth naming because it is the one nothing can undo: the
    task takes no signal, so kill -9, timeout(1) and libvirt cancelling its own
    hook all pass over it, and the machine leaves the state only by rebooting.
    Read straight from /proc rather than through ps: by the time this is asked
    the host is already sick, and a fork is one more thing that can block.
    """
    out: list[tuple[str, str]] = []
    try:
        entries = list(PROC.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue  # exited between the listing and the read
        # comm is the only field that can contain spaces or parentheses, and it
        # is wrapped in the last pair of them -- so split from the right.
        head, _, rest = stat.rpartition(") ")
        fields = rest.split()
        if fields and fields[0] == "D":
            out.append((head.partition("(")[2], entry.name))
    return out


def rebind_wedged(dgpu: str) -> str:
    """Why the round failed, when what failed is the rebind and not the guest.

    THE FAILURE THIS EXISTS FOR. Measured 2026-08-18, fifth round: the guest
    shut down, vfio-pci reset the card and let go, `modprobe nvidia_drm` took
    it back -- and never returned, stuck in the driver's GSP init
    (nv_drm_dev_load -> nvkms_open_gpu -> nv_start_device -> RmInitAdapter ->
    kgspInitRm_IMPL -> rmapiLockAcquire, blocked on an rw-semaphore). The hook
    runs synchronously under libvirtd, so the daemon went down with it and
    every virsh after that timed out.

    WHY THE DOMAIN STATE CANNOT DECIDE THIS. Both failures -- a guest that
    ignores its shutdown, and a rebind that hangs -- end as the same timeout on
    the same `virsh domstate`. The round used to read that timeout as the first
    one and print "the guest did not shut down, the card is still in it", while
    its own summary line one row above said the card was on nvidia. So the
    second failure needs evidence the domain state cannot produce, and a task
    in D is that evidence: nothing else on this host puts modprobe there.
    """
    stuck = [f"{comm}({pid})" for comm, pid in d_state_procs()
             if comm in NVIDIA_INIT_COMMS]
    if not stuck:
        return ""
    return (f"geri: geri-bağlama asıldı — nvidia sürücü init'i D durumunda "
            f"({' '.join(stuck)}). Misafir kapandı ve kart "
            f"'{probe.driver_of(dgpu)}' üzerinde görünüyor, ama init bitmedi; "
            f"libvirtd kendi hook'unda bloke olduğu için virsh de cevap "
            # NOT `-k -b`: that form implies the CURRENT boot, and the line
            # right above says recovery is a reboot -- so the reader runs the
            # hint after the reboot and gets `-- No entries --`, which reads as
            # "there is no evidence" for a question that has 40 lines of it.
            f"vermiyor. Yığın: journalctl _TRANSPORT=kernel -g "
            f"'{DEADLOCK_SIGNATURE.pattern}'")


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
#
# THE LAST THREE ALTERNATES ARE THE DEAD LOCK, missing until 2026-08-21.
# Measured against the recorded 2026-08-18 window: of its 1505 lines this
# pattern caught 68 and not one of them was the failure. The round read its own
# journal every time and stayed blind to the only class whose recovery is a
# reboot.
JOURNAL_SIGNALS = re.compile(
    r"NVRM|non-zero usage count|vfio|Xorg|caught signal|SIGABRT|libvirt"
    r"|rw-semaphore|rmapiLockAcquire|kgspInitRm",
    re.IGNORECASE,
)

# WHY A SECOND, NARROWER PATTERN: the one above selects what is worth reading,
# and most of what it selects is normal -- 68 lines in that same window. This
# one decides a verdict, so it may hold only strings measured to appear when
# the GSP init race actually fired. Across all 93 boots of this journal, every
# transport, it matches 40 lines in exactly the two known events and nowhere
# else. What did not qualify, and why:
#   - `INFO: task`, `blocked for more than`, `hung_task` each fire on a third
#     boot too, an exfat unmount blocked on writeback (2026-08-21) with no
#     nvidia frame in its stack. Generic hung-task wording cannot name a
#     subsystem.
#   - `rmapiLockIsOwner` appears ONCE in the whole journal and only in the
#     08-12 event; the 08-18 one -- the round this tool actually lost -- never
#     printed it. Keying on the assertion would have missed the failure it
#     exists for.
# Case-sensitive on purpose: these are the kernel's own spellings.
DEADLOCK_SIGNATURE = re.compile(r"rw-semaphore|rmapiLockAcquire|kgspInitRm")


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
    which is why the hook has a check mode at all -- and why the hook answers
    with three codes rather than two: 0 handover, 1 no handover, 2 nothing was
    decided because there is no usable vfio.conf.
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
    if out.returncode == 2:
        return False, (text + "\n-> hook yapılandırılmamış: vfio.conf yok, "
                              "okunamıyor, cihaz listelemiyor ya da PCI adresi "
                              "olmayan bir şey taşıyor")
    # THE EXIT CODE IS THE CONTRACT AND THE PRINTED LINE IS WHAT A HUMAN READS,
    # so this reads both and lets neither answer alone. Matching the line's
    # exact spacing tied this to the hook's alignment column -- widening a
    # label there would turn a working handover into a preflight refusal, and a
    # wrong refusal costs more than a wrong warning. Anchored on purpose: an
    # nvme audit line that merely quotes the word must not be able to answer
    # for the gate.
    return (out.returncode == 0
            and re.search(r"^result:\s+handover$", out.stdout, re.M) is not None), text


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
    print(f"   domain: {_virsh(['domstate', target.domain], timeout=20)[1]}")

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
    # Every DRM node is printed with the card it belongs to. The name alone is
    # not a fact about the hardware -- see probe.render_of() -- so a reader
    # comparing this line against an earlier run would otherwise be comparing
    # two different cards without being told.
    owners = probe.drm_owners()
    print(f"   {target.compositor} açık dri/nvidia fd'leri: "
          + (" ".join(f"{n}[{owners[n]}]" if n in owners else n
                      for n in sorted(set(nodes))) or "(yok)"))
    if any(n.startswith("/dev/nvidia") for n in nodes):
        print("      <-- compositor'ün açık bir nvidia düğümü var")
        ok = False

    # The DRM nodes are the other way to hold the card, and the one that is easy
    # to miss: nvidia-smi says None, no /dev/nvidia* fd exists, and nvidia_drm
    # still sits at refcnt=1. Both are resolved through PCI every time -- the
    # minor numbers are unstable and change across a handover -- and they are
    # resolved SEPARATELY, because the card and render numberings are handed out
    # independently of each other (probe.render_of()).
    #
    # The render node is asked about at all because the seat rule deliberately
    # does not cover it: 72-vfio-dgpu-no-uaccess.rules matches card* only, and
    # says so, to leave render offload working. So an fd on renderD* is not a
    # rule that failed, it is a client that has to let go -- but it stops the
    # handover just the same, because it holds nvidia_drm at refcnt=1.
    for label, node, hint in (
            ("KMS", probe.card_of(target.dgpu),
             "      72-vfio-dgpu-no-uaccess.rules onu uzak tutmamış"),
            ("render", probe.render_of(target.dgpu),
             "      bu düğümü 72-... kuralı bilerek kapsamıyor — tutan istemci "
             "kapanmalı (oturumun EGL/GLX/Vulkan pinleri)")):
        if node is None:
            print(f"   dGPU {label} düğümü: yok")
        elif f"/dev/dri/{node}" in nodes:
            print(f"   dGPU {label} düğümü: {node}  <-- COMPOSITOR TUTUYOR, "
                  "devir düşer")
            print(hint)
            ok = False
        else:
            print(f"   dGPU {label} düğümü: {node}, compositor tutmuyor")

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

def _wait_state(domain: str, wanted: str, seconds: int) -> tuple[str, bool]:
    """(state, unreachable). The second half is the whole point: see TIMED_OUT.

    Reported from the last poll rather than sticky, because a daemon that
    answered on the way out is a daemon that is running -- the failure being
    separated out here is one libvirtd never comes back from without a reboot.
    """
    deadline = time.monotonic() + seconds
    state, unreachable = "", False
    while time.monotonic() < deadline:
        rc, out = _virsh(["domstate", domain], timeout=30)
        unreachable = rc == TIMED_OUT
        state = "" if unreachable else out
        if state == wanted:
            return state, False
        time.sleep(2)
    return state, unreachable


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
        state, unreachable = _wait_state(target.domain, "running", 60)
        driver = probe.driver_of(target.dgpu)
        comp_now = pid_of(target.compositor)
        print(f"   kart={driver} domain={state or '(cevap yok)'} "
              f"{target.compositor}={comp_now}")
        if driver != "vfio-pci":
            verdict = f"start: kart '{driver}', beklenen vfio-pci"
        elif unreachable:
            wedged = rebind_wedged(target.dgpu)
            verdict = wedged or (
                f"start: libvirt cevap vermiyor (domstate zaman aşımı) — "
                f"kart '{driver}'")
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
    state, unreachable = _wait_state(target.domain, "shut off",
                                     target.shutdown_timeout)
    time.sleep(5)
    driver = probe.driver_of(target.dgpu)
    comp_now = pid_of(target.compositor)
    print(f"   kart={driver} domain={state or '(cevap yok)'} "
          f"{target.compositor}={comp_now}")
    wedged = rebind_wedged(target.dgpu) if unreachable else ""
    if wedged:
        if not verdict:
            verdict = wedged
        print("   !! Bu bir misafir sorunu DEĞİL: misafir kapandı, asılan şey "
              "geri-bağlama.")
        print("   !! D durumundaki görev sinyal almaz — kurtarma yalnız reboot.")
    elif unreachable:
        # No stuck task and no answer either: say both halves rather than
        # guessing which one is the fault. libvirtd can also be down for
        # reasons that have nothing to do with the card.
        if not verdict:
            verdict = (f"geri: libvirt cevap vermiyor (domstate zaman aşımı), "
                       f"ama D durumunda nvidia görevi de yok — kart '{driver}'")
    elif state != "shut off":
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

    # A ROUND THAT PRINTED THIS IS NOT A CLEAN ROUND, even when every other
    # check passed. `rebind_wedged` can only speak while a task is still in D,
    # and that is one sample taken as the round ends; the kernel's own report
    # arrives after 122 seconds of blockage and lands here instead. So this is
    # the wider net in time, and the case it exists for is the round that
    # finished looking clean -- which nothing recorded until now. That gap is
    # the whole problem: two events in 75 re-inits means a clean series is
    # almost no evidence, so a round that came close has to leave a mark.
    #
    # `if not verdict` on purpose: when the machine really is wedged the
    # verdict above already names the cause, and this must not overwrite it.
    wedge = [line for line in signals if DEADLOCK_SIGNATURE.search(line)]
    if wedge:
        print(f"\n   !! ÖLÜ KİLİT İMZASI — {len(wedge)} satır. Bu tur temiz "
              f"sayılmaz.")
        print(f"   !! Tüm geçmiş (boot'tan bağımsız): journalctl "
              f"_TRANSPORT=kernel -g '{DEADLOCK_SIGNATURE.pattern}'")
        if not verdict:
            verdict = (f"journal: ölü kilit imzası basıldı ({len(wedge)} "
                       f"satır) — devir tamamlandı ama tur temiz sayılmaz")
    return verdict


def _defined_domains_block() -> str:
    """The names to choose from, or an honest line about why there are none.

    IT DOES NOT CLAIM TO TELL "none defined" FROM "could not ask". Those come
    back as the same empty list -- defined_domains() returns names, not rc --
    so this prints both readings instead of picking one and being wrong half
    the time.

    THE CALL IS BOUNDED, unlike the guards. core/domains.py's rule is that a
    guard passes None because a timeout must not make it fall open; this is a
    reporting path, where the opposite holds -- a refusal that hangs on a
    wedged libvirtd is worse than one that says the list went unread.
    """
    if not domains.available():
        return "\n   (virsh yok — liste sorulamadı)"
    names = domains.defined_domains(timeout=domains.CONNECT_TIMEOUT)
    if not names:
        return ("\n   (tanımlı domain yok, ya da virsh cevap vermedi — bu iki "
                "cevap ayırt edilmiyor)")
    return "".join(f"\n   {n}" for n in names)


def run(rounds: int = 5, domain: str | None = None, compositor: str = "Hyprland",
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

    # BEFORE THE GATE AND THE LAYOUT, because this one is not about the
    # machine: nothing here can be answered by looking at hardware, and a
    # reader who forgot the flag should not first be told what is wrong with
    # their host.
    if domain is None:
        print("`--domain` verilmedi — ve varsayılanı yok: hedefi bu araç "
              "seçmez.")
        print("Eskiden `win11` idi. Bu makinenin misafiri değişince eski ad "
              "çözünmeye devam etti, yani çağrı hata vermeden beş turu başka "
              "bir misafirde koşar ve sonucu bu farkı hiç söylemeden "
              "raporlardı.")
        print(f"\n== Tanımlı domain'ler{_defined_domains_block()}")
        print(f"\nHedefi seçip yeniden çağır: "
              f"{provenance.command(*sys.argv[1:], '--domain', '<ad>')}")
        return 2

    open_gate, p, _ = doctor.gate(profile_name)
    if not open_gate or p is None:
        print(f"Kapı kapalı — selftest koşmaz. {provenance.command('doctor')}")
        return 1
    layout = install_mod.resolve(probe.read_machine(), p)
    if layout is None:
        print(f"Adresler çözülemedi. {provenance.command('doctor')}")
        return 1
    if not hostfiles.HOOK.exists():
        print(f"{hostfiles.HOOK} yok — önce `{provenance.command('install')}`.")
        return 1

    if preflight_only:
        target = Target(domain, layout.dgpu, layout.dgpu_audio, compositor, poller,
                        boot_timeout, shutdown_timeout)
        ok = preflight(target)
        print(f"\n   günlük: {LOG}")
        return 0 if ok else 1

    # The guard, not a nag: inside the session this test cannot report its own
    # failure, because the failure it hunts kills the session.
    #
    # A MISSING TTY IS A REFUSAL, NOT A YES. This used to skip the whole block
    # when stdin was not a terminal, which reads as caution and is the
    # opposite: `vfioctl selftest < /dev/null`, or the same line run by any
    # tool that does not allocate a pty, went straight into five handover
    # rounds with nobody asked. Consent is given by --yes and cannot be
    # inferred from the absence of somebody to ask.
    if not assume_yes:
        # The suggested line is this invocation plus --yes, not a rebuilt one:
        # a hint spelled `selftest --yes` from a run that said `--domain
        # win11-test --rounds 1` drops both flags. Since --domain lost its
        # default that second one now refuses rather than silently retargeting
        # -- which is exactly what it used to do -- but the reason to append is
        # unchanged: the rounds somebody consents to have to be the rounds they
        # asked for. --yes is valid last, after the subcommand's own flags, so
        # appending is enough.
        deliberate = provenance.command(*sys.argv[1:], "--yes")
        if not sys.stdin.isatty():
            print("Bu çağrının tty'si yok, yani düz VT sorusu sorulamıyor — ve "
                  "cevabı varsayılmıyor.")
            print(f"{rounds} devir turu kartı taşır. Bilerek isteniyorsa: "
                  f"{deliberate}")
            return 2
        try:
            tty = os.ttyname(sys.stdin.fileno())
        except OSError as exc:
            print(f"Bu kabuğun tty adı okunamadı ({exc.strerror}), yani düz VT "
                  "olup olmadığı ölçülemiyor — ve varsayılmıyor.")
            print(f"Bilerek isteniyorsa: {deliberate}")
            return 2
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
    if comp_before is None:
        # Not a refusal: a machine with no compositor can still prove the
        # handover, and that is a smaller claim, not a false one. But every
        # round decides "the desktop survived" by comparing this value with
        # itself, so an absent one makes that check empty -- and an empty check
        # reads exactly like a passing one. Say it once, in the log.
        print(f"\n!! '{compositor}' koşmuyor (ya da adı yanlış). Compositor'ün "
              "sağ kaldığı denetimi bu koşuda BOŞ: hüküm vermez ama geçmiş "
              "gibi görünür.")
        print(f"   Doğru adı bulmak: ps -eo comm= | sort -u | grep -i <parça> "
              f"— çekirdek adın ilk {COMM_MAX} baytını saklar, daha uzunu "
              "eşleşmez.")
    poller_j0 = cpu_jiffies(poller_pid)

    # Printed before anything can go wrong, not only in the summary: if the
    # session dies mid-run this line is how the reader finds what happened.
    print(f"\n\n########## {_now()}  {rounds} tur  profil={p.name}")
    print(f"günlük (paylaşılacak tek dosya): {LOG}")
    print(f"kart: {probe.driver_of(layout.dgpu)}   "
          f"domain: {_virsh(['domstate', domain], timeout=20)[1]}   "
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
          f"domain: {_virsh(['domstate', domain], timeout=20)[1]}   "
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
