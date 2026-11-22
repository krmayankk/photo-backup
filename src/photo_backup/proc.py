"""Thin, injectable wrapper around subprocess so external tools can be faked in tests."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run(cmd: Sequence[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` capturing text output. Never raises for a missing binary or non-zero exit."""
    try:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(cmd), 127, "", f"{cmd[0]}: command not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(cmd), 124, "", f"{cmd[0]}: timed out")
