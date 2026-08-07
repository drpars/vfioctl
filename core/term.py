r"""Colour, and the single question that decides whether there is any.

WHY THIS FILE EXISTS. The same escape codes were written two ways: guarded by
sys.stdout.isatty() in doctor.py and inventory.py, and unguarded in install.py
and sysfile.py. So one run could paint its doctor report correctly and, in the
same breath, put raw \033[1;36m into whatever `vfioctl install > kurulum.log`
was pointed at -- and the file that gets pasted into a report is exactly the
artifact the codes ruin. Two copies of a rule are how a third place ends up
with none, so the rule lives here and nowhere else.

WHY A Tee ANSWERS "NOT A TTY". selftest and `guest setup` replace sys.stdout
with a Tee that writes to a log file as well as to the terminal. Painting for
the terminal would put the escape codes into that log, and the log is the whole
point of those two commands: it is the file that survives a graphics session
dying mid-round, read afterwards from somewhere else. So a Tee reports itself
as not a tty and both halves come out plain. The terminal loses colour for the
duration; the artifact stays readable, which is the trade worth making.

WHY getattr AND NOT stream.isatty(). Anything can be assigned to sys.stdout,
and a file-like object is not required to implement isatty() -- both Tee
classes in this tool went years without it. A missing method must mean "no
colour", never an AttributeError raised from inside a print.
"""

from __future__ import annotations

import sys


def can_paint(stream: object | None = None) -> bool:
    """Would colour reach a terminal, and only a terminal?

    Asked at call time rather than cached, because sys.stdout is replaced
    part-way through a run (see the Tee note above) and a value read at import
    time would answer for the wrong stream.
    """
    stream = sys.stdout if stream is None else stream
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        # A closed or detached stream is not a terminal; raising here would
        # turn a cosmetic question into a crash in the middle of output.
        return False


def paint(text: str, code: str) -> str:
    """`text` wrapped in an SGR code, or returned untouched where it would be
    noise in a file."""
    return f"\033[{code}m{text}\033[0m" if can_paint() else text
