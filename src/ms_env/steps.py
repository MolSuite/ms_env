"""Steps: named units of work that run one after another, their output read line by line.

Plain standard library and Qt-free: a UI only renders `Step` lists and the lines they print.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_TIMEOUT = 30.0


class StepFailed(RuntimeError):
    pass


@dataclass(slots=True)
class Step:
    """One named unit of work: a command line, or an in-process call. `run` is a call that
    prints as it goes: it gets the function each line goes to."""

    name: str
    argv: Optional[list[str]] = None
    call: Optional[Callable[[], str]] = None
    timeout: float = DEFAULT_TIMEOUT
    run: Optional[Callable[[Callable[[str], None]], None]] = None


def run_step(step: Step, on_line: Callable[[str], None] | None = None) -> None:
    """Run one step, streaming its output. Raises StepFailed on failure."""
    emit = on_line or (lambda _line: None)
    if step.run is not None:
        step.run(emit)
        return
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
            encoding="utf-8",  # not the locale's: cp1252 on Windows
            errors="replace",
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
    """The program that creates environments here: micromamba, unless a variable names another.

    ``extra_variables`` are app-specific overrides, read before ``MOLSUITE_ENV_MANAGER``; any
    of them may name a conda or a mamba. None: the recipe downloads its own micromamba.
    """
    named = [str(os.environ.get(variable) or "").strip() for variable in (*extra_variables, "MOLSUITE_ENV_MANAGER")]
    for candidate in (*filter(None, named), shutil.which("micromamba")):
        path = Path(candidate or "").expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    return None


__all__ = ["DEFAULT_TIMEOUT", "Step", "StepFailed", "find_environment_manager", "run_step", "run_steps"]
