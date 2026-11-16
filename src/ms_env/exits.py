"""What a tool's exit code means: its recipe's `[exit_codes]`, then what any program's means
(a signal, a command not found). A tool that failed is a `ToolFailed`, which says it in words."""
from __future__ import annotations

import signal

# What a signal that ended a program most often means, said for whoever runs it.
_SIGNALS = {
    signal.SIGKILL: "killed (SIGKILL): most often the machine ran out of memory, or a limit of its queue",
    signal.SIGSEGV: "crashed (SIGSEGV): a bug of the program, or an input it cannot take",
    signal.SIGABRT: "aborted (SIGABRT): the program stopped itself on an error",
    signal.SIGBUS: "crashed (SIGBUS): a file it read changed or its disk failed",
    signal.SIGTERM: "stopped (SIGTERM): something asked it to end",
    signal.SIGXCPU: "stopped (SIGXCPU): its CPU time limit ran out",
}
_SHELL = {126: "it could not be run (not executable)", 127: "it was not found"}


def meaning(code: int, codes: dict | None = None) -> str:
    """What exit `code` means: `codes` (a recipe's `[exit_codes]`, keyed by the code as text or
    number) first; a signal (Python's negative code, or the shell's 128 + n); "" when unknown."""
    codes = {int(key): str(value) for key, value in (codes or {}).items()}
    if code in codes:
        return codes[code]
    number = -code if code < 0 else code - 128 if 128 < code < 160 else 0
    if number:
        with_name = _SIGNALS.get(number)
        if with_name:
            return with_name
        try:
            return f"ended by {signal.Signals(number).name}"
        except ValueError:
            pass
    return _SHELL.get(code, "")


class ToolFailed(RuntimeError):
    """`tool` exited with `code`: what that means, and the last of what it said (`output`)."""

    def __init__(self, tool: str, code: int, output: str = "", codes: dict | None = None):
        self.tool, self.code, self.output, self.meaning = tool, code, output, meaning(code, codes)
        said = f" ({self.meaning})" if self.meaning else ""
        tail = output.strip()[-1000:]
        super().__init__(f"{tool} exited with {code}{said}" + (f": {tail}" if tail else ""))

    def __reduce__(self):  # across processes: the words, already made
        return _again, (self.tool, self.code, self.output, self.meaning, str(self))


def _again(tool, code, output, meaning_, message):
    error = ToolFailed.__new__(ToolFailed)
    RuntimeError.__init__(error, message)
    error.tool, error.code, error.output, error.meaning = tool, code, output, meaning_
    return error

