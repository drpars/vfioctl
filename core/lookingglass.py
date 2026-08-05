"""Which Looking Glass release is on each side, and do the two agree.

WHY THIS FILE EXISTS. The host client and the guest host application must be
the SAME release: the shared memory segment carries a version and a client that
does not recognise it refuses the segment instead of degrading. The failure
reaches the operator as "the client connects and no frames arrive" -- the exact
shape of a broken passthrough, which is why an unmeasured mismatch gets blamed
on this tool. Nothing else here asks the question, so nothing else could say
that the two halves simply do not match.

THE PIN IS THE GUEST SIDE'S SOURCE OF TRUTH. guest/windows/looking-glass.ps1
carries $Version, $Url and $Sha256 together, because the checksum is only valid
for one release; that trio may not be split, and this module does not rewrite
it. It reads it, so the comparison has something to compare against on a
machine with no guest running at all.

THREE READINGS, NOT TWO, AND THEY ANSWER DIFFERENT QUESTIONS:

    client_release()  what the host will run           (the machine, now)
    read_pin()        what `setup` would install       (the repository)
    release_from_log()what the guest is running        (the guest, now)

The first pair can disagree before a guest exists -- that is the cheap warning,
available to `doctor` without touching libvirt. The first and third disagreeing
is the expensive one: a guest already built against a client that has since
been upgraded. Only the third is evidence; the pin is an intention.

WHY THE HOST SIDE IS READ FROM THE PACKAGE DATABASE. The client has no
--version flag (B7: only --help and --rst-help), and the release string it
compiles in is not reachable without starting it -- and starting it means
opening /dev/kvmfr0, which is the one thing a measurement must not do while a
guest is up. So the product is resolved first (the binary that would actually
run, via PATH) and the database is asked who owns it, rather than asking after
a package by name: on a machine where the client came from looking-glass-git,
or from source, the honest answer is "unknown" and this returns it.

AN UNKNOWN IS NOT A MISMATCH. compare() returns None when either side could not
be read, and callers pass that through rather than turning it into a failure --
the tool must stay usable on a distribution whose package manager is not
pacman, where it simply has nothing to say about versions.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CLIENT_BIN = "looking-glass-client"

# The guest side's download address is derived, never typed twice: the release
# in the URL and the release in $Version are the same fact.
ARTIFACT = "https://looking-glass.io/artifact/{release}/host"

PS1 = Path(__file__).resolve().parent.parent / "guest" / "windows" / "looking-glass.ps1"

# The pin, read out of the PowerShell param block. Keep these two lines in the
# script in the shape below; a pin this cannot parse is reported as unknown
# rather than guessed at.
_PIN_VERSION = re.compile(r"\$Version\s*=\s*'([^']+)'")
_PIN_URL = re.compile(r"\$Url\s*=\s*'([^']+)'")

# What the guest's host application says about itself on its first lines:
#   00:00:00.023 [I] app.c:867 | app_main | Looking Glass Host (B7)
_LOG_RELEASE = re.compile(r"Looking Glass Host \(([^)]+)\)")

# pacman's version is epoch:pkgver-pkgrel; only pkgver is upstream's release.
# "2:B7-7" -> "B7". The epoch and the pkgrel move when the packaging changes
# and upstream does not, so comparing them would fire on a rebuild.
_PKGVER = re.compile(r"^(?:\d+:)?(?P<ver>.+?)-[^-]+$")

# The releases upstream publishes under /artifact/: B1, B2 ... B7. A client
# whose version does not have this shape (a git build, a release candidate) is
# not comparable to a pin, and saying so is more useful than a false alarm.
_RELEASE_SHAPE = re.compile(r"^B\d+$")


def artifact_url(release: str) -> str:
    return ARTIFACT.format(release=release)


# --------------------------------------------------------------------------- #
# the pin -- what setup would install
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Pin:
    release: str | None
    url: str | None
    detail: str

    @property
    def coherent(self) -> bool:
        """Does the URL point at the release $Version names?

        A pin whose halves disagree installs one release while announcing
        another, and every reading downstream of it is then wrong about which
        version the guest carries -- including the checksum, which is what
        would actually stop the download. Cheap to ask, so it is asked.
        """
        if not (self.release and self.url):
            return False
        return f"/artifact/{self.release}/" in self.url


def read_pin(path: Path = PS1) -> Pin:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Pin(None, None, f"{path} okunamadı ({exc.strerror})")

    version = _PIN_VERSION.search(text)
    url = _PIN_URL.search(text)
    if not version:
        return Pin(None, url.group(1) if url else None,
                   f"{path.name} içinde $Version satırı bulunamadı")
    return Pin(version.group(1), url.group(1) if url else None,
               f"{path.name}: $Version = {version.group(1)}")


# --------------------------------------------------------------------------- #
# the client -- what this host will run
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Client:
    release: str | None
    detail: str                      # how it was measured, or why it could not be
    path: str | None = None
    package: str | None = None
    raw_version: str | None = None   # "2:B7-7", kept for the report


def _pacman(*args: str) -> tuple[int, str]:
    """LC_ALL=C so the fields stay where they are; pacman translates its prose."""
    try:
        r = subprocess.run(["pacman", *args], capture_output=True, text=True,
                           timeout=20, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return r.returncode, r.stdout.strip()


def client_release() -> Client:
    path = shutil.which(CLIENT_BIN)
    if not path:
        return Client(None, f"{CLIENT_BIN} PATH'te yok — istemci kurulu değil")
    if not shutil.which("pacman"):
        return Client(None, f"{path} var ama sürümü okunamadı (pacman yok)",
                      path=path)

    rc, owner = _pacman("-Qoq", path)
    if rc != 0 or not owner:
        return Client(None, f"{path} hiçbir pakete ait değil — kaynaktan "
                            "kurulmuş olabilir, sürümü buradan okunamıyor",
                      path=path)
    package = owner.splitlines()[0].strip()

    rc, line = _pacman("-Q", package)
    if rc != 0 or len(line.split()) < 2:
        return Client(None, f"{package} sürümü okunamadı", path=path,
                      package=package)
    raw = line.split()[1]

    match = _PKGVER.match(raw)
    version = match.group("ver") if match else raw
    if not _RELEASE_SHAPE.match(version):
        return Client(None, f"{package} {raw} — sürümü B-serisi biçiminde değil, "
                            "pin ile karşılaştırılmadı",
                      path=path, package=package, raw_version=raw)
    return Client(version, f"{package} {raw} → {version}",
                  path=path, package=package, raw_version=raw)


# --------------------------------------------------------------------------- #
# the guest -- what it is actually running
# --------------------------------------------------------------------------- #

def release_from_log(lines: list[str] | str) -> str | None:
    """The release the guest's host application announced at startup.

    THIS IS THE ONLY ONE OF THE THREE THAT IS EVIDENCE. The pin says what would
    be installed and the package database says what the client would run; the
    guest's own log says what is there. A guest built months ago against an
    older pin answers correctly here and nowhere else.
    """
    text = lines if isinstance(lines, str) else "\n".join(lines)
    match = _LOG_RELEASE.search(text)
    return match.group(1).strip() if match else None


# --------------------------------------------------------------------------- #
# the comparison
# --------------------------------------------------------------------------- #

def compare(a: str | None, b: str | None) -> bool | None:
    """True when both sides are known and equal, False when known and different.

    None means unanswerable, and callers keep it that way: an unread version is
    not a mismatch, and a check that reports one as a failure teaches the
    reader to skip the line.
    """
    if not (a and b):
        return None
    return a == b


def remedy(client: str | None, guest: str | None) -> str:
    """What to do about a mismatch, with the address the guest side needs.

    The address is derived from the client's release rather than written down,
    because a version number typed into a document goes stale the moment the
    package is upgraded and the one derived from the installed client does not.
    """
    if not client:
        return ("İstemcinin sürümü okunamadı; misafir tarafının sürümü elle "
                f"doğrulanmalı. Adres kalıbı: {ARTIFACT.format(release='<sürüm>')}")
    return (
        f"Misafir tarafı istemcinin sürümüne çekilir: {artifact_url(client)}\n"
        f"  {PS1.name} içinde $Version, $Url ve $Sha256 ÜÇÜ BİRLİKTE "
        f"güncellenir (sha256 yalnızca tek bir sürüm için geçerli), sonra\n"
        f"  ./vfioctl guest --name <domain> setup"
        + (f"\n  Misafirde şu an {guest} var." if guest else "")
    )
