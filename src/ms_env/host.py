"""A host: where commands run. This machine, or another reached with the system's `ssh`.

No credential is handled here. `ssh` runs with `BatchMode=yes`: the keys and the agent of the
account log in or the command fails at once, and nothing is left listening on the other side.
"""
from __future__ import annotations

import subprocess


class HostError(RuntimeError):
    pass


class Host:
    """A machine, known by what running a command on it gives back: `address` as `ssh` knows it (its
    keys, users and jumps are in `~/.ssh/config`), or None for this one."""

    def __init__(self, address: str | None = None, ssh=("ssh", "-o", "BatchMode=yes")):
        self.address, self.ssh = address or None, tuple(ssh)

    def __repr__(self) -> str:
        return f"Host({self.address!r})"

    def argv(self, command: str) -> list[str]:
        """What to execute here for `sh` to run `command` there."""
        return ["sh", "-c", command] if self.address is None else [*self.ssh, self.address, command]

    def run(self, command: str, data=b"", timeout: float = 600) -> bytes:
        """What `command` printed there, given `data` (bytes, or an open file) to read."""
        feed = {"input": data} if isinstance(data, bytes) else {"stdin": data}
        done = subprocess.run(self.argv(command), **feed, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        if done.returncode:
            said = done.stderr.decode(errors="replace").strip()
            if "Permission denied" in said:  # BatchMode: ssh does not ask, and here nobody could answer
                said += f"\nssh has to log in to {self.address} with a key, without asking: see ssh-copy-id"
            raise HostError(f"`{command[:200]}` on {self.address or 'this machine'}: {said or done.returncode}")
        return done.stdout


LOCAL = Host()

__all__ = ["LOCAL", "Host", "HostError"]
