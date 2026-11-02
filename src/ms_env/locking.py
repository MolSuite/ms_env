"""A short filesystem lock on an environment prefix, for the installs and removals of every client."""
from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from ms_env.host import LOCAL, Host

log = logging.getLogger(__name__)

# The lock of the prefix `$p`, in `$lock`, with the parent's symlinks resolved as `Path.resolve` does here.
_LOCK_SH = ('p={prefix}; mkdir -p "$(dirname "$p")" || exit 1; '
            'parent=$(cd "$(dirname "$p")" && pwd -P) || exit 1; lock="$parent/$(basename "$p").ms-env-lock"; ')


class Locked(RuntimeError):
    """The prefix is another client's now: `owner` is what it wrote (`client`, `pid`, `since`)."""

    def __init__(self, lock: str, owner: dict):
        self.lock, self.owner = lock, owner
        who = f"{owner.get('client', '?')} (pid {owner.get('pid', '?')}) since {owner.get('since', '?')}"
        super().__init__(f"environment is locked by {who}: {lock}; once that client stopped, "
                         "`ms_env.unlock` removes the lock")


def _started(pid: int) -> str | None:
    """When the process `pid` of this machine started, as `ps` says; "" when it does not run,
    None when that cannot be told."""
    try:
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gone(owner: dict) -> bool:
    """Whether the client that took a lock stopped. Only a client of this machine can be told:
    by its pid, and by when that pid started, for a pid the system gave out again."""
    if owner.get("client") != socket.gethostname() or not str(owner.get("pid", "")).isdigit():
        return False
    started = _started(int(owner["pid"]))
    return started == "" or started is not None and owner.get("started", started) != started


def _owner(text: str) -> dict:
    try:
        owner = json.loads(text)
    except ValueError:
        return {}  # killed between taking the lock and saying whose it is: not told apart
    return owner if isinstance(owner, dict) else {}


def _local_lock(prefix: str) -> Path:
    return Path(str(Path(prefix).expanduser().resolve()) + ".ms-env-lock")


def _drop(lock: str, host: Host, owner_text: str | None = None) -> None:
    """Remove the lock, or, given `owner_text`, only while it is still the one with that owner."""
    if host.address is None:
        path = Path(lock, "owner.json")
        if owner_text is None or (path.read_text() if path.is_file() else "") == owner_text:
            path.unlink(missing_ok=True)
            Path(lock).rmdir()
        return
    quoted = shlex.quote(lock)
    drop = f"rm -f -- {quoted}/owner.json && rmdir -- {quoted}"
    if owner_text is not None:
        drop = f'if [ "$(cat {quoted}/owner.json 2>/dev/null)" = {shlex.quote(owner_text)} ]; then {drop}; fi'
    host.run(drop, timeout=60)


def _take(prefix: str, host: Host, owner: str) -> tuple[str, str | None]:
    """Take the lock: its path, and nothing; or, when it is somebody's, its path and what its owner wrote."""
    if host.address is None:
        lock = _local_lock(prefix)
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError:
            path = lock / "owner.json"
            return str(lock), path.read_text() if path.is_file() else ""
        try:
            (lock / "owner.json").write_text(owner)
        except BaseException:
            lock.rmdir()
            raise
        return str(lock), None
    said = host.run(_LOCK_SH.format(prefix=shlex.quote(prefix))
                    + 'if ! mkdir -m 700 "$lock" 2>/dev/null; then printf "locked\\n%s\\n" "$lock"; '
                      'cat "$lock/owner.json" 2>/dev/null; exit 0; fi; '
                    + 'printf "%s" ' + shlex.quote(owner) + ' > "$lock/owner.json" || { rmdir "$lock"; exit 1; }; '
                      'printf "free\\n%s\\n" "$lock"', timeout=60).decode()
    state, lock, *rest = said.split("\n", 2)
    return lock, ((rest or [""])[0] if state == "locked" else None)


@contextmanager
def prefix_lock(prefix: str, host: Host = LOCAL):
    """Fail closed (`Locked`) when another client owns this prefix; no background service.

    A lock left by a client of this machine that stopped (killed, crashed) is removed and taken
    again. One of another machine is never guessed at, by age or else, as that is unsafe on a
    cluster: `unlock` removes it once its owner is known to have stopped.
    """
    if not prefix or Path(prefix) == Path(prefix).parent:
        raise ValueError("a lock requires a dedicated environment prefix")
    owner = json.dumps({"client": socket.gethostname(), "pid": os.getpid(), "started": _started(os.getpid()) or "",
                        "since": time.strftime("%Y-%m-%d %H:%M:%S")})
    lock, held = _take(prefix, host, owner)
    if held is not None and _gone(_owner(held)):
        # ponytail: two clients of this machine that find one stale lock at once can both drop it, and
        # the second then drops the lock the first just took; the owner is checked again right before.
        log.warning("removing the lock of %s left by a client that stopped: %s", prefix, held)
        _drop(lock, host, held)
        lock, held = _take(prefix, host, owner)
    if held is not None:
        raise Locked(lock, _owner(held))
    try:
        yield
    finally:
        _drop(lock, host)


def unlock(prefix: str, host: Host = LOCAL) -> dict:
    """Remove the lock of `prefix`, whoever took it, and return what its owner wrote ({} for no lock).
    Only once that owner is known to have stopped: else two clients change one environment."""
    if host.address is None:
        lock = _local_lock(prefix)
        if not lock.is_dir():
            return {}
        path = lock / "owner.json"
        held = path.read_text() if path.is_file() else ""
    else:
        said = host.run(_LOCK_SH.format(prefix=shlex.quote(prefix))
                        + '[ -d "$lock" ] || exit 0; printf "%s\\n" "$lock"; cat "$lock/owner.json" 2>/dev/null',
                        timeout=60).decode()
        if not said:
            return {}
        lock, _, held = said.partition("\n")
    _drop(str(lock), host)
    return _owner(held)
