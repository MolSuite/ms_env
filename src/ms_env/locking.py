"""A short filesystem lock on an environment prefix, for the installs and removals of every client."""
from __future__ import annotations

import json
import os
import shlex
import socket
from contextlib import contextmanager
from pathlib import Path

from ms_env.host import LOCAL, Host


@contextmanager
def prefix_lock(prefix: str, host: Host = LOCAL):
    """Fail closed when another client owns this prefix; no background service.

    A killed installer can leave a lock. It must be removed only after confirming
    that its owner has stopped; guessing from its age is unsafe on a cluster.
    """
    if not prefix or Path(prefix) == Path(prefix).parent:
        raise ValueError("a lock requires a dedicated environment prefix")
    owner = json.dumps({"client": socket.gethostname(), "pid": os.getpid()})
    if host.address is None:
        lock = Path(str(Path(prefix).expanduser().resolve()) + ".ms-env-lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimeError(f"environment is locked: {lock}; verify its owner before removing a stale lock") from None
        try:
            (lock / "owner.json").write_text(owner)
            yield
        finally:
            (lock / "owner.json").unlink(missing_ok=True)
            lock.rmdir()
    else:
        prefix = shlex.quote(prefix)
        command = (f"p={prefix}; mkdir -p \"$(dirname \"$p\")\" || exit 1; "
                   'parent=$(cd "$(dirname "$p")" && pwd -P) || exit 1; '
                   'lock="$parent/$(basename "$p").ms-env-lock"; '
                   'if ! mkdir -m 700 "$lock"; then echo "environment is locked: $lock" >&2; exit 1; fi; '
                   'printf "%s" ' + shlex.quote(owner) + ' > "$lock/owner.json" || { rmdir "$lock"; exit 1; }; '
                   'printf "%s" "$lock"')
        lock = host.run(command, timeout=60).decode()
        try:
            yield
        finally:
            quoted = shlex.quote(lock)
            host.run(f"rm -- {quoted}/owner.json && rmdir -- {quoted}", timeout=60)
