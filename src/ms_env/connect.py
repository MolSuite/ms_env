"""Log in to a machine once with its password, so that from then on `ssh` gets in alone.

`connect` makes a key for the machine (`~/.ssh/molsuite/<name>`), names the machine for `ssh` in
`~/.ssh/molsuite/config`, which `~/.ssh/config` includes (one line, at its top), and logs in once
to put the key there. What ssh asks on that login (whether to trust the machine, the password, the
code of a second factor) goes to `ask` in ssh's own words; the answers go to ssh and are kept
nowhere. A site that wants more than a key keeps that login open instead, for `persist`: the
commands of `Host` go through it until it ends, and then `connect` is to be done again.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable

from ms_env.host import _SHARED, Host, HostError

KEY = "key"  # the key gets in alone
SESSION = "session"  # the site wants more than a key: the login is kept open

# What ssh runs to ask, with its question as the one argument: the question goes to the socket
# named in MS_ENV_ASKPASS, whoever listens there answers, and ssh reads the answer printed.
# Nothing back is "no answer": ssh then takes the login as refused.
_ASKPASS = r"""
import os, socket, sys
with socket.socket(socket.AF_UNIX) as asked:
    asked.connect(os.environ["MS_ENV_ASKPASS"])
    asked.sendall((sys.argv[1] if len(sys.argv) > 1 else "").encode())
    asked.shutdown(socket.SHUT_WR)
    answer = b"".join(iter(lambda: asked.recv(65536), b""))
if not answer:
    sys.exit(1)
sys.stdout.write(answer[1:].decode() + "\n")
"""

# Puts the key read from stdin in the account's authorized keys, once.
_AUTHORIZE = ('umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && key=$(cat) && '
              '{ grep -qxF "$key" ~/.ssh/authorized_keys || printf "%s\\n" "$key" >> ~/.ssh/authorized_keys; }')


@contextlib.contextmanager
def asking(ask: Callable[[str], str | None]):
    """The environment for an `ssh` whose questions go to `ask(question)`, which answers with the
    text to give, or None for no answer. `ask` runs on a thread of its own, one question at a time."""
    with tempfile.TemporaryDirectory(prefix="ms_env-") as there:  # only this account reads it
        script, address = Path(there, "askpass"), str(Path(there, "s"))
        script.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -c {shlex.quote(_ASKPASS)} "$@"\n')
        script.chmod(0o700)
        server = socket.socket(socket.AF_UNIX)
        server.bind(address)
        server.listen()

        def serve() -> None:
            while True:
                try:
                    talk, _ = server.accept()
                except OSError:  # closed: the login is over
                    return
                with talk:
                    question = b"".join(iter(lambda: talk.recv(65536), b"")).decode(errors="replace")
                    answer = ask(question.strip())
                    if answer is not None:
                        talk.sendall(b"\1" + answer.encode())

        threading.Thread(target=serve, name="ms_env-askpass", daemon=True).start()
        try:
            # SSH_ASKPASS_REQUIRE is OpenSSH 8.4; an older one asks it when it has no terminal and a DISPLAY
            yield {**os.environ, "SSH_ASKPASS": str(script), "SSH_ASKPASS_REQUIRE": "force",
                   "DISPLAY": os.environ.get("DISPLAY") or ":0", "MS_ENV_ASKPASS": address}
        finally:
            with contextlib.suppress(OSError):
                server.shutdown(socket.SHUT_RDWR)
            server.close()


def name_host(name: str, hostname: str, user: str = "", port: str = "", ssh_dir: Path | None = None) -> Path:
    """Make `name` a machine `ssh` knows, logging in with a key of its own; the key, made if there
    is none, and the config it is named in are under `ssh_dir/molsuite`. Its path."""
    ssh_dir = Path(ssh_dir or Path.home() / ".ssh")
    folder = ssh_dir / "molsuite"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = folder / name
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"molsuite {name}", "-f", str(key)],
                       check=True, capture_output=True)
    config = folder / "config"
    blocks = re.split(r"\n(?=Host )", config.read_text().strip()) if config.exists() else []
    blocks = [block for block in blocks if block and block.split(None, 2)[1:2] != [name]]
    blocks.append("\n".join([f"Host {name}", f"    HostName {hostname}", *([f"    User {user}"] if user else []),
                             *([f"    Port {port}"] if port else []), f'    IdentityFile "{key}"',
                             "    IdentitiesOnly yes"]))
    config.write_text("\n\n".join(blocks) + "\n")
    main = ssh_dir / "config"
    line = f'Include "{config}"'
    said = main.read_text() if main.exists() else ""
    if line not in said.splitlines():  # at the top: an Include under a `Host` would count only for that one
        main.write_text(f"{line}\n{said}")
        main.chmod(0o600)
    return key


def connect(name: str, target: str, ask: Callable[[str], str | None], persist: str = "8h",
            ssh_dir: Path | None = None) -> str:
    """Make `name` the machine `target` (`user@machine` or `user@machine:port`) for `ssh`, with a
    key of its own put there by logging in once; `ask` answers what ssh asks on that login (see
    `asking`). `KEY` when the key gets in alone from then on, `SESSION` when the site wants more
    and the login stays open for `persist` instead. Raises `HostError` when it did not get in.
    Blocks, for as long as the answers take: not on the GUI thread."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"`{name}`: a name of letters, digits, _ and -")
    if not _SHARED:  # ponytail: Windows' OpenSSH keeps no login open; the key is put there by hand
        raise HostError("connecting from the app needs an ssh that can keep a login open, which Windows' cannot")
    user, _, place = target.strip().rpartition("@")
    hostname, _, port = place.partition(":")
    if not hostname:
        raise ValueError(f"`{target}`: user@machine, or user@machine:port")
    key = name_host(name, hostname, user, port, ssh_dir)
    shared = ["-o", next(option for option in _SHARED if option.startswith("ControlPath"))]
    login = ["ssh", "-o", "StrictHostKeyChecking=ask", "-o", "ControlMaster=yes",
             "-o", f"ControlPersist={persist}", *shared, name, _AUTHORIZE]
    refused = []

    def once(question: str) -> str | None:  # ssh asks again after a no; the one who said it is not asked twice
        answer = None if refused else ask(question)
        refused.extend([] if answer is not None else [question])
        return answer

    with asking(once) as env:
        try:
            done = subprocess.run(login, input=key.with_name(f"{name}.pub").read_bytes(), env=env,
                                  capture_output=True, start_new_session=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise HostError(f"could not log in to {target}: no answer in 10 minutes") from None
    if done.returncode:
        said = done.stderr.decode(errors="replace").strip()
        methods = re.findall(r"Permission denied \(([^)]*)\)", said)
        if methods and not re.search(r"password|keyboard-interactive", methods[-1]):  # it takes no password
            said += f"\n{target} takes only keys given to it another way: give it {key}.pub, then connect again"
        raise HostError(f"could not log in to {target}: {said or done.returncode}")
    try:  # the key alone, not through the login just kept open
        Host(name, ssh=("ssh", "-o", "BatchMode=yes", "-o", "ControlPath=none")).run("true", timeout=60)
    except HostError:
        Host(name).run("true", timeout=60)  # through it, then: what a job will do until it ends
        return SESSION
    subprocess.run(["ssh", *shared, "-O", "exit", name], capture_output=True, timeout=60)
    return KEY


__all__ = ["KEY", "SESSION", "asking", "connect", "name_host"]
