"""Small formatting helpers for terminal output."""

from __future__ import annotations

from collections.abc import Sequence

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_GATE_FAILED = 2
EXIT_USAGE = 3


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    """Left-align the first column, right-align the rest."""
    cells = [[str(h) for h in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = []
    for row in cells:
        parts = [row[0].ljust(widths[0])] + [
            c.rjust(w) for c, w in zip(row[1:], widths[1:], strict=True)
        ]
        lines.append("  ".join(parts).rstrip())
    return lines
