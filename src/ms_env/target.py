"""A target: where commands run. This machine, or a host reached with the system's `ssh`.

No credential is handled here. `ssh` runs with `BatchMode=yes`: the keys and the agent of the
account log in or the command fails at once, and nothing is left listening on the other side.
"""
from __future__ import annotations

import subprocess


class TargetError(RuntimeError):
    pass


class Target:
    """A machine, known by what running a command on it gives back: `host` as `ssh` knows it (its
    keys, users and jumps are in `~/.ssh/config`), or None for this one."""

    def __init__(self, host: str | None = None, ssh=("ssh", "-o", "BatchMode=yes")):
        self.host, self.ssh = host or None, tuple(ssh)

    def __repr__(self) -> str:
        return f"Target({self.host!r})"

    def argv(self, command: str) -> list[str]:
        """What to execute here for `sh` to run `command` there."""
        return ["sh", "-c", command] if self.host is None else [*self.ssh, self.host, command]

    def run(self, command: str, data=b"", timeout: float = 600) -> bytes:
        """What `command` printed there, given `data` (bytes, or an open file) to read."""
        feed = {"input": data} if isinstance(data, bytes) else {"stdin": data}
        done = subprocess.run(self.argv(command), **feed, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        if done.returncode:
            said = done.stderr.decode(errors="replace").strip()
            if "Permission denied" in said:  # BatchMode: ssh does not ask, and here nobody could answer
                said += f"\nssh has to log in to {self.host} with a key, without asking: see ssh-copy-id"
            raise TargetError(f"`{command[:200]}` on {self.host or 'this machine'}: {said or done.returncode}")
        return done.stdout


LOCAL = Target()

__all__ = ["LOCAL", "Target", "TargetError"]
