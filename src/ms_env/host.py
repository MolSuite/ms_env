"""A host: where commands run. This machine, or another reached with the system's `ssh`.

No credential is handled here. `ssh` runs with `BatchMode=yes`: the keys and the agent of the
account log in or the command fails at once, and nothing is left listening on the other side.
"""
from __future__ import annotations

import platform as _platform
import subprocess
from pathlib import Path

# (system, machine) -> the platform as conda names it, which is also how a recipe names builds.
_PLATFORMS = {
    ("linux", "x86_64"): "linux-64", ("linux", "aarch64"): "linux-aarch64",
    ("darwin", "x86_64"): "osx-64", ("darwin", "arm64"): "osx-arm64",
    ("windows", "amd64"): "win-64", ("windows", "x86_64"): "win-64",
}


def conda_platform(system: str, machine: str) -> str:
    """`linux-64`, `osx-arm64`, `win-64`...; `system-machine` for one conda has no name for."""
    system, machine = system.lower(), machine.lower()
    return _PLATFORMS.get((system, machine), f"{system}-{machine}")


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
        """What to execute here for `sh` to run `command` there. This machine needs an `sh` for
        that, which Windows lacks: what ms_env does here it does in Python, not through this."""
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
            if "Host key verification failed" in said:  # the same: nobody here to say yes to a new key
                said += f"\nssh does not know {self.address} yet: log in to it once by hand (`ssh {self.address}`)"
            raise HostError(f"`{command[:200]}` on {self.address or 'this machine'}: {said or done.returncode}")
        return done.stdout

    def home(self) -> str:
        """The home of the account there: where a path of ours on that machine starts."""
        if self.address is None:
            return str(Path.home())
        return self.run('printf %s "$HOME"', timeout=60).decode()

    def platform(self) -> str:
        """The platform there, as conda names it (see `conda_platform`)."""
        if self.address is None:
            return conda_platform(_platform.system(), _platform.machine())
        return conda_platform(*self.run("uname -sm", timeout=60).decode().split()[:2])


LOCAL = Host()

__all__ = ["LOCAL", "Host", "HostError", "conda_platform"]
