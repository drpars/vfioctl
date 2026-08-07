"""Which copy of vfioctl is running, and how a printed command should spell it.

WHY THIS FILE EXISTS (K20). The day the tool became installable there were two
copies of it on this machine: the package under /usr/lib and the clone it was
built from. Nothing measured which one was running, and the one check that
sounds like it would -- `install --check` -- structurally cannot: it compares
/etc against what THE RUNNING CODE would write, so a stale copy reports
"identical" while the machine is behind whatever the clone now says. Measured
2026-08-06: package r22.g14b0c84, clone r24.g8fd02bf, --check clean.

TWO DIFFERENT QUESTIONS, TWO DIFFERENT SOURCES, AND THEY MUST NOT BE MIXED UP.
"Which tree is executing" is answered by this file's own location, because that
is the code that will actually be imported; argv[0] can be a symlink, a
relative path or a name typed at a shell. "How should a command be spelled back
to the reader" is answered by argv[0], because that is the string that ran.

THE VERSION FORMULA IS THE PKGBUILD'S, RE-DERIVED. `r<commit count>.g<short
sha>` is computed here from git in the running tree exactly as PKGBUILD
computes pkgver at parse time. The two are one fact written twice and they move
together; an installed package carries no .git, so there is no third place to
read it from and no way to compare without repeating the formula.

A DIRTY CLONE IS NOT AT ITS OWN SHA, AND THE VERSION STRINGS CANNOT SETTLE IT.
`git rev-parse HEAD` describes the last commit, not the files on disk. And
makepkg takes the file NAMES from git but the BYTES from the working tree (the
PKGBUILD header says so in as many words), so a package built from a dirty
clone carries those uncommitted bytes under a pkgver naming a commit that never
contained them. Equal shas plus a dirty tree therefore means one of two
opposite things -- built from these edits, or edited since the build -- and
nothing readable from here tells them apart. So it is reported as unanswerable
rather than as a mismatch. Calling it a mismatch was the first draft of this
file and it was wrong in the commonest case there is: the developer who just
ran `makepkg -si` on the tree they are looking at.

WHAT WOULD SETTLE IT, AND WHY IT IS NOT HERE. Comparing the bytes of the
running tree against /usr/lib/vfioctl would turn the whole answer from a claim
into proof, the way `install --check` does for /etc. It is a bigger question
than K20 decided -- which files the package ships is knowable only from the
package, and "in the clone but not installed" splits into a missing module and
a document that was never meant to ship. Left as an address, not written.

WHAT IS DELIBERATELY NOT ASKED. Nothing here reaches the network, so
origin/main is out of scope -- "your clone is behind upstream" is a different
question and would put a fetch inside `doctor`. And when the installed copy is
the one running, where the clone lives is unknown and stays unknown: this file
does not go looking for checkouts to compare against.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CMD = "vfioctl"

# Where the package puts the tree, and the entry point pacman owns. Written in
# PKGBUILD as /usr/lib/$pkgname; if that moves, this moves with it.
INSTALLED_TREE = Path("/usr/lib") / CMD
INSTALLED_ENTRY = INSTALLED_TREE / CMD

# The tree this code is being imported from -- core/provenance.py -> core -> .
RUNNING_TREE = Path(__file__).resolve().parent.parent

# pacman's version is epoch:pkgver-pkgrel; only pkgver carries the sha. The
# pkgrel moves when the packaging changes and the code does not, so comparing
# it would fire on a plain rebuild.
_PKGVER = re.compile(r"^(?:\d+:)?(?P<ver>.+?)-[^-]+$")


# --------------------------------------------------------------------------- #
# how to spell a command back to the reader
# --------------------------------------------------------------------------- #

def spelling() -> str:
    """The name this copy actually answers to, from where it was started.

    argv[0] ALWAYS RUNS; the bare name only sometimes does. Measured: started
    through PATH, argv[0] is /usr/bin/vfioctl; started from a clone it is
    ./vfioctl. So one comparison decides -- if the entry PATH would find is the
    entry that is running, print the bare name, otherwise print argv[0]
    unchanged. A hardcoded "./vfioctl" is wrong for everyone who installed the
    package and a hardcoded "vfioctl" is wrong for everyone who did not, and
    the string is printed where it is needed most: in a failure, as the next
    command to type.

    The comparison is on resolved paths because /usr/bin/vfioctl is a symlink
    into the tree; realpath makes both sides name the same file.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return CMD
    found = shutil.which(CMD)
    if found:
        try:
            if Path(found).resolve() == Path(argv0).resolve():
                return CMD
        except OSError:
            pass
    return argv0


def command(*words: str) -> str:
    """A copy-pasteable invocation, e.g. command("doctor")."""
    return " ".join((spelling(), *words))


def rewrite(text: str, placeholder: str = "./" + CMD) -> str:
    """Replace a written-out invocation with the one that runs here.

    For the header blocks that argparse prints as its epilog: they are aligned
    tables, so a shorter replacement is padded back to width. A longer one
    (argv[0] as an absolute path) shifts the comments and is accepted -- a
    command that runs beats a column that lines up.
    """
    name = spelling()
    if len(name) < len(placeholder):
        name = name.ljust(len(placeholder))
    return text.replace(placeholder, name)


# --------------------------------------------------------------------------- #
# which copy, and is it the installed one
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Report:
    same: bool | None        # None = unanswerable, which is not a mismatch
    detail: str
    remedy: str = ""
    running_version: str | None = None
    package_version: str | None = None


def _pacman(*args: str) -> tuple[int, str]:
    """LC_ALL=C so the fields stay where they are; pacman translates its prose."""
    try:
        r = subprocess.run(["pacman", *args], capture_output=True, text=True,
                           timeout=20, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return r.returncode, r.stdout.strip()


def _git(tree: Path, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(tree), *args],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return r.returncode, r.stdout.strip()


def clone_version(tree: Path = RUNNING_TREE) -> tuple[str | None, str]:
    """`r<count>.g<sha>` for a git checkout, and how it was read.

    The same two commands PKGBUILD runs, in the same order. The reason comes
    back with the value because the three ways to fail here are different
    facts: there is no .git at all (the installed tree, and the expected
    answer), git is not on this machine, or git is there and refused. Folding
    them into a bare None made the report say "this is not a git clone" about a
    clone whose index had simply been corrupted.
    """
    if not (tree / ".git").exists():
        return None, f"{tree} bir git klonu değil"
    if not shutil.which("git"):
        return None, "git kurulu değil — klonun sürümü okunamadı"
    rc, _ = _git(tree, "rev-parse", "--git-dir")
    if rc != 0:
        return None, f"{tree}/.git var ama git onu okuyamadı"
    rc, count = _git(tree, "rev-list", "--count", "HEAD")
    if rc != 0 or not count:
        return None, f"{tree}: commit sayısı okunamadı (henüz commit yok mu?)"
    rc, sha = _git(tree, "rev-parse", "--short", "HEAD")
    if rc != 0 or not sha:
        return None, f"{tree}: HEAD sha'sı okunamadı"
    return f"r{count}.g{sha}", f"klon {tree}: r{count}.g{sha}"


def dirty_files(tree: Path = RUNNING_TREE) -> int | None:
    """How many paths differ from HEAD, or None when it cannot be asked.

    Untracked files are included: an untracked module the running code imports
    is code the package cannot possibly contain.
    """
    rc, out = _git(tree, "status", "--porcelain")
    if rc != 0:
        return None
    return len([line for line in out.splitlines() if line.strip()])


def installed_version() -> tuple[str | None, str]:
    """What pacman says about the copy under /usr/lib, and how it was read.

    The package is discovered by asking who owns the installed entry point
    rather than by querying a name, the same idiom core/lookingglass.py uses
    for the Looking Glass client: a fork that renamed the package still gets a
    true answer, and a machine whose package manager is not pacman gets an
    honest "unknown" instead of a wrong "missing".
    """
    if not shutil.which("pacman"):
        return None, "pacman yok — kurulu sürüm okunamadı"
    if not INSTALLED_ENTRY.exists():
        return None, f"{INSTALLED_ENTRY} yok — paket kurulu değil"

    rc, owner = _pacman("-Qoq", str(INSTALLED_ENTRY))
    if rc != 0 or not owner:
        return None, f"{INSTALLED_ENTRY} hiçbir pakete ait değil"
    package = owner.splitlines()[0].strip()

    rc, line = _pacman("-Q", package)
    if rc != 0 or len(line.split()) < 2:
        return None, f"{package} sürümü okunamadı"
    raw = line.split()[1]
    match = _PKGVER.match(raw)
    return (match.group("ver") if match else raw), f"{package} {raw}"


def describe() -> Report:
    """Is the code running here the code the machine has installed?

    Four answers, and three of them are "cannot say" on purpose -- an
    unanswerable question reported as a failure is a check the reader learns to
    scroll past.
    """
    package_version, package_detail = installed_version()

    if RUNNING_TREE == INSTALLED_TREE:
        # Running the package itself. There is nothing to compare it against:
        # a clone may exist anywhere on this disk or nowhere at all, and going
        # looking for one would be a guess with a version number attached.
        return Report(
            None,
            f"koşan kopya kurulu paketin kendisi ({package_detail}); "
            "klon varsa nerede olduğu buradan görülmez",
            package_version=package_version,
            running_version=package_version,
        )

    running, running_detail = clone_version()
    if running is None:
        return Report(
            None, f"{running_detail} | kurulu: {package_detail}",
            package_version=package_version,
        )

    dirty = dirty_files()
    if dirty:
        running_detail += f", {dirty} yol commit edilmemiş"

    if package_version is None:
        return Report(
            None, f"{running_detail} | {package_detail}",
            running_version=running,
        )

    # Measured on both sides and different: the one case a version string can
    # settle on its own.
    if package_version != running:
        return Report(
            False, f"{running_detail} | kurulu: {package_detail}",
            remedy=("Makinedeki kurulum klonun gerisinde ya da ilerisinde. "
                    f"Eşitlemek: cd {RUNNING_TREE} && makepkg -si"),
            running_version=running, package_version=package_version,
        )

    if dirty is None:
        return Report(
            None, f"{running_detail} | kurulu: {package_detail} — klonun temiz "
                  "olup olmadığı okunamadı, yani sha eşitliği tek başına "
                  "yeterli değil",
            running_version=running, package_version=package_version,
        )

    if dirty:
        return Report(
            None, f"{running_detail} | kurulu: {package_detail}",
            remedy=("Sha'lar aynı ama klon temiz değil, ve sürüm dizesi "
                    "HEAD'i adlandırır — diskteki baytları değil. makepkg "
                    "çalışma ağacının baytlarını kurduğu için bu iki durum "
                    "buradan ayırt edilemez: paket bu değişikliklerle "
                    "kurulmuş olabilir ya da onlardan önce. Ayırt edilebilir "
                    f"olması için commit edip cd {RUNNING_TREE} && makepkg -si."),
            running_version=running, package_version=package_version,
        )

    return Report(
        True, f"{running_detail} | kurulu: {package_detail}",
        running_version=running, package_version=package_version,
    )
