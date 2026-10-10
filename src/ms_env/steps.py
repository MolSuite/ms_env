"""Steps: named units of work that run one after another, their output read line by line.

Plain standard library and Qt-free: a UI only renders `Step` lists and the lines they print.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_TIMEOUT = 30.0


class StepFailed(RuntimeError):
    pass


@dataclass(slots=True)
class Step:
    """One named unit of work: either a command line or an in-process call."""

    name: str
    argv: Optional[list[str]] = None
    call: Optional[Callable[[], str]] = None
    timeout: float = DEFAULT_TIMEOUT


def run_step(step: Step, on_line: Callable[[str], None] | None = None) -> None:
    """Run one step, streaming its output. Raises StepFailed on failure."""
    emit = on_line or (lambda _line: None)
    if step.call is not None:
        message = step.call()
        if message:
            emit(message)
        return
    if not step.argv:
        return
    emit("$ " + " ".join(step.argv))
    try:
        process = subprocess.Popen(
            step.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise StepFailed(f"{step.name}: command not found ({exc.filename}).") from exc
    deadline = time.monotonic() + step.timeout
    assert process.stdout is not None
    for line in process.stdout:
        emit(line.rstrip())
        if time.monotonic() > deadline:
            process.kill()
            raise StepFailed(f"{step.name}: timed out after {step.timeout:.0f}s.")
    try:
        code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        raise StepFailed(f"{step.name}: timed out after {step.timeout:.0f}s.") from None
    if code != 0:
        raise StepFailed(f"{step.name}: exited with code {code}.")


def run_steps(
    steps: Iterable[Step],
    on_line: Callable[[str], None] | None = None,
    on_step: Callable[[int, Step], None] | None = None,
) -> None:
    for index, step in enumerate(steps):
        if on_step is not None:
            on_step(index, step)
        run_step(step, on_line)


def find_environment_manager(*extra_variables: str) -> Path | None:
    """Local micromamba/mamba/conda. ``extra_variables`` are app-specific overrides, checked first."""
    candidates: list[Path] = []
    for variable in (*extra_variables, "MOLSUITE_ENV_MANAGER", "MAMBA_EXE", "CONDA_EXE"):
        configured = str(os.environ.get(variable) or "").strip()
        if configured:
            candidates.append(Path(configured).expanduser())
    prefix = Path(sys.prefix).expanduser().resolve()
    if prefix.parent.name == "envs":
        base = prefix.parents[1]
        for relative in ("bin/micromamba", "bin/mamba", "bin/conda", "condabin/conda"):
            candidates.append(base / relative)
    for name in ("micromamba", "mamba", "conda"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


__all__ = ["DEFAULT_TIMEOUT", "Step", "StepFailed", "find_environment_manager", "run_step", "run_steps"]
