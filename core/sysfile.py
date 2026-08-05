"""Writing root-owned files from a tool that does not run as root.

WHY NOT JUST REQUIRE ROOT. `doctor` has to run everywhere and write nothing,
and it is the subcommand someone porting the tool runs first. A tool that
demands root before it will even look at the machine teaches its user to run
the whole thing as root, and then the one subcommand that must never write is
running with the privileges to do it. So privilege is taken per write, at the
moment of writing, and nowhere else.

EVERY WRITE IS READ-COMPARE-WRITE. The caller gets back whether the bytes
actually moved, because the expensive follow-up -- udevadm reload, restarting
libvirtd -- is worth doing when they did and is pure noise when they did not.
Rewriting a file with identical content would also destroy the backup that
matters: the copy of what was there before this tool ever ran.

BACKUPS ARE SIBLINGS EXCEPT WHERE A DIRECTORY IS ENUMERATED. libvirt runs
every executable file in hooks/qemu.d/ whatever it is called, so a sibling
.bak there is a second, older hook that runs on every VM start -- measured
2026-08-04, and the old copy was the version that wedges the machine. Callers
writing into a consumed directory pass `backup=` pointing somewhere else.

THE SHELL IS NEVER INVOLVED. Content goes to `sudo tee` over a pipe rather
than through a redirect, so no filename or file body is ever parsed by a
shell. Paths here are module constants, not user input, and they stay that
way.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(cmd: list[str]) -> int:
    """Run a command, showing it first. Printed because these are the steps a
    reader would otherwise have to take on trust."""
    print(f"\033[1;36m$ {' '.join(cmd)}\033[0m")
    return subprocess.call(cmd)


def read_text(path: Path) -> str | None:
    """Current contents, or None when the file is absent or unreadable.

    Unreadable and absent are deliberately the same answer: both mean "we do
    not know what is there", and the only safe response to that is to write.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def sudo_write(path: Path, content: str) -> int:
    print(f"\033[1;36m$ sudo tee {path}\033[0m")
    proc = subprocess.run(
        ["sudo", "tee", str(path)],
        input=content,
        text=True,
        stdout=subprocess.DEVNULL,
    )
    return proc.returncode


def write_with_backup(
    path: Path, content: str, backup: Path | None = None
) -> tuple[int, bool]:
    """Write `content` to `path`, backing up a differing existing file.

    Returns (exit code, changed).
    """
    current = read_text(path)
    if current == content:
        print(f"  = {path} (değişmedi)")
        return 0, False

    rc = 0
    if current is not None:
        backup = backup or path.with_suffix(path.suffix + ".bak")
        print(f"  ~ {path} → yedek: {backup}")
        rc |= run(["sudo", "cp", str(path), str(backup)])

    rc |= sudo_write(path, content)
    return rc, True


def sudo_remove(path: Path) -> tuple[int, bool]:
    """Delete a file we installed. Returns (exit code, removed).

    No backup is taken here and that is on purpose: the content is generated,
    so `install` reproduces it exactly. A .bak left in a directory something
    enumerates is the hazard this whole module exists to remember.
    """
    if not path.exists():
        print(f"  = {path} (zaten yok)")
        return 0, False
    return run(["sudo", "rm", "-f", str(path)]), True
