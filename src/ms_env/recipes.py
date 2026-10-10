"""External tools: find one that already works, or install it -- driven by a TOML file.

The app ships one TOML per tool: where to look for it, how to tell it works, how to
install it. A recipe is that TOML bound to one host (see ``ms_env.host``): this
machine, or a host over ssh. ``status`` says whether the tool works there and ``ensure``
installs it when it does not, on a daemon thread that belongs to the app, not to a
project (none has to be open).

Lookup on a host, first candidate whose check exits 0 wins: paths the user
registered, the managed install under ``tools_home``, then ``PATH``.

Whose a prefix is decides what happens to it. ``ensure`` stamps what it installs: a
stamped prefix is created again when the recipe changes, and ``remove`` deletes it. A
prefix that holds something without a stamp is the user's own environment: ``verify``
says what it lacks of what the recipe asks for, and nothing ever changes or deletes it.

TOML layout (every section but ``[tool]`` and ``[locate]`` is optional)::

    [tool]
    name = "openbabel"
    label = "OpenBabel"
    version = "3.1.1"                       # pinned here; never a user option
    prefix = "{tools_home}/{name}/{version}"  # the default

    [locate]
    paths = ["{prefix}/bin/obabel"]         # where the install puts it
    commands = ["obabel"]                   # looked up on PATH
    check = ["{exe}", "-V"]                 # {exe} = the candidate

    [env]                                   # a conda prefix at {prefix}
    conda = ["openbabel={version}"]
    pip = []

    [[install]]                             # plain steps, after [env]
    name = "download the model"
    argv = ["curl", "-fsSL", "-o", "{prefix}/model.pt", "https://..."]
    timeout = 1800

Every command runs through ``sh`` on the host, so a host is a POSIX machine.
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import re
import shlex
import subprocess
import tarfile
import threading
import tomllib
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ms_env.steps import Step, StepFailed, run_step
from ms_env.host import LOCAL, Host

READY = "ready"
MISSING = "missing"
UNREACHABLE = "unreachable"
_SSH_CONNECT_FAILED = 255  # ssh's own exit code; anything else came from the remote command
STAMP = ".ms_env.json"  # inside a prefix ms_env installed
_INSTALLING = ".installing"  # beside a prefix while it is installed: what a cut install leaves is ours too
_USER = "user"


# ----------------------------------------------------------------------------
# Spec (the TOML, still templated) -> Recipe (concrete for one host)
# ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RecipeStep:
    name: str
    argv: tuple[str, ...]
    timeout: float = 1800.0
    # Files of this machine the command reads as a tar, each under its own name. Out of the
    # repr, which is what the stamp of a tool is made of.
    send: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class Recipe:
    name: str
    prefix: str
    candidates: tuple[str, ...]  # executables tried first, in order
    commands: tuple[str, ...]  # then these names on PATH
    check: tuple[str, ...]  # "{exe}" marks the candidate
    steps: tuple[RecipeStep, ...]
    conda: tuple[str, ...] = ()  # what the environment at `prefix` has to hold
    pip: tuple[str, ...] = ()
    stamp: str = ""  # hash of what the TOML asks for, the same on every host
    # What brings a prefix up to this recipe without making it again, for one of ours whose
    # stamp names the same `base`. Without a base a recipe that changed is installed from nothing.
    refresh: tuple[RecipeStep, ...] = ()
    base: str = ""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    version: str
    info: dict[str, Any]  # the whole [tool] table: label, purpose, footprint...
    prefix: str
    paths: tuple[str, ...]
    commands: tuple[str, ...]
    check: tuple[str, ...]
    conda: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    channels: tuple[str, ...] = ("conda-forge",)
    steps: tuple[RecipeStep, ...] = ()

    @classmethod
    def load(cls, path: Path | str) -> "ToolSpec":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        tool, locate, env = data["tool"], data["locate"], data.get("env", {})
        return cls(
            name=str(tool["name"]),
            version=str(tool.get("version", "")),
            info=dict(tool),
            prefix=str(tool.get("prefix", "{tools_home}/{name}/{version}")),
            paths=tuple(locate.get("paths", ())),
            commands=tuple(locate.get("commands", ())),
            check=tuple(locate.get("check", ())),
            conda=tuple(env.get("conda", ())),
            pip=tuple(env.get("pip", ())),
            channels=tuple(env.get("channels", ("conda-forge",))),
            steps=tuple(
                RecipeStep(str(step["name"]), tuple(step["argv"]), float(step.get("timeout", 1800)))
                for step in data.get("install", ())
            ),
        )

    def recipe(
        self,
        tools_home: str,
        *,
        prefix: str = "",
        extra_paths: Iterable[str] = (),
        manager: str = "",
    ) -> Recipe:
        """Bind the templates to one host. ``tools_home``/``prefix`` are paths *on that host*.

        ``prefix`` is a location the user chose instead of the managed one: an environment of
        their own (checked, never changed) or a place to install into. ``extra_paths`` are
        user-registered executables; ``manager`` is a conda-type program to use there instead
        of micromamba.
        """
        values = {"tools_home": str(tools_home).rstrip("/"), "name": self.name, "version": self.version, "exe": "{exe}"}
        values["prefix"] = str(prefix or self.prefix.format_map(values)).rstrip("/")
        fill = lambda items: tuple(str(item).format_map(values) for item in items)  # noqa: E731
        steps = _env_steps(self.name, fill(self.conda), fill(self.pip), fill(self.channels), values, manager)
        steps += [RecipeStep(step.name, fill(step.argv), step.timeout) for step in self.steps]
        return Recipe(
            name=self.name,
            prefix=values["prefix"],
            candidates=(*map(str, extra_paths), *fill(self.paths)),
            commands=self.commands,
            check=fill(self.check),
            steps=tuple(steps),
            conda=fill(self.conda),
            pip=fill(self.pip),
            stamp=hashlib.sha256(
                repr((self.name, self.version, self.conda, self.pip, self.channels, self.steps)).encode()
            ).hexdigest()[:16],
        )


def _env_steps(
    name: str, conda: tuple[str, ...], pip: tuple[str, ...], channels: tuple[str, ...], values: dict, manager: str
) -> list[RecipeStep]:
    if not conda and not pip:
        return []
    prefix = values["prefix"]
    channel_args = [part for channel in channels for part in ("--channel", channel)]
    create = shlex.join(["create", "--yes", "--quiet", "--prefix", prefix, *channel_args, *conda])
    steps = [
        RecipeStep(
            f"create the {name} environment",
            ("sh", "-c", f'{_find_or_bootstrap_manager(manager, values["tools_home"])}\n"$M" {create}'),
        )
    ]
    if pip:
        steps.append(
            RecipeStep(
                f"pip install into {name}",
                (f"{prefix}/bin/python", "-m", "pip", "install", "--progress-bar", "off", *pip),
                timeout=3600.0,
            )
        )
    return steps


def _find_or_bootstrap_manager(hint: str, tools_home: str) -> str:
    """sh that leaves the manager in $M: the hint, micromamba on PATH, or one it downloads."""
    root = shlex.quote(f"{tools_home}/micromamba")
    return f"""M={shlex.quote(str(hint or ""))}
[ -x "$M" ] || M=$(command -v micromamba) || {{
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) p=linux-64 ;; Linux-aarch64) p=linux-aarch64 ;;
    Darwin-arm64) p=osx-arm64 ;; Darwin-x86_64) p=osx-64 ;;
    *) echo "no micromamba here and no build of it for $(uname -sm)" >&2; exit 1 ;;
  esac
  M={root}/bin/micromamba
  if [ ! -x "$M" ]; then
    echo "no micromamba found: installing it into {root}"
    mkdir -p {root} && curl -fsSL "https://micro.mamba.pm/api/micromamba/$p/latest" | tar -xj -C {root} bin/micromamba || exit 1
  fi
  export MAMBA_ROOT_PREFIX={root}
}}"""


# ----------------------------------------------------------------------------
# A recipe on a host
# ----------------------------------------------------------------------------

def _argv_on(argv: Iterable[str], host: Host) -> list[str]:
    return list(argv) if host.address is None else host.argv(shlex.join(argv))


def _prefix(recipe: Recipe) -> str:
    if not recipe.prefix.strip("/ "):
        raise ValueError(f"{recipe.name}: a recipe needs a prefix of its own, not {recipe.prefix!r}.")
    return shlex.quote(recipe.prefix)


def _stamp_text(recipe: Recipe) -> str:
    return json.dumps({"name": recipe.name, "stamp": recipe.stamp, **({"base": recipe.base} if recipe.base else {})})


def _base(owner: str) -> str:
    """The base a stamp names; "" for what is not a stamp, or the stamp of a recipe without one."""
    try:
        return json.loads(owner).get("base", "")
    except (ValueError, AttributeError):
        return ""


def _tar(paths: Iterable[str]) -> bytes:
    held = io.BytesIO()
    with tarfile.open(fileobj=held, mode="w") as tar:
        for path in paths:
            tar.add(path, arcname=Path(path).name)
    return held.getvalue()


def _step(step: RecipeStep, label: str, host: Host) -> Step:
    if not step.send:
        return Step(label + step.name, argv=_argv_on(step.argv, host), timeout=step.timeout)

    def send() -> str:
        host.run(shlex.join(step.argv), data=_tar(step.send), timeout=step.timeout)
        return "sent " + ", ".join(Path(path).name for path in step.send)

    return Step(label + step.name, call=send, timeout=step.timeout)


def recipe_steps(recipe: Recipe, host: Host = LOCAL, refresh: bool = False) -> list[Step]:
    """The whole install: clear what ms_env left at the prefix, run the recipe, stamp it.

    The first step fails on a prefix that holds something ms_env did not install. With
    `refresh` the prefix stays and only `recipe.refresh` runs before the stamp.
    """
    label, prefix = (f"{host.address}: " if host.address else ""), _prefix(recipe)
    clear = (
        f'p={prefix}\n'
        f'if [ -n "$(ls -A "$p" 2>/dev/null)" ] && [ ! -f "$p/{STAMP}" ] && [ ! -f "$p{_INSTALLING}" ]; then\n'
        '  echo "$p holds an environment ms_env did not install: it is checked, never changed." >&2; exit 1\n'
        "fi\n"
        f'mkdir -p "$(dirname "$p")" && : > "$p{_INSTALLING}" && rm -rf -- "$p"'
    )
    stamp = (
        f'p={prefix}\nmkdir -p "$p" && printf %s {shlex.quote(_stamp_text(recipe))} > "$p/{STAMP}" '
        f'&& rm -f "$p{_INSTALLING}"'
    )
    steps = (
        *(recipe.refresh if refresh else (RecipeStep(f"prepare {recipe.prefix}", ("sh", "-c", clear), 300.0), *recipe.steps)),
        RecipeStep(f"record the {recipe.name} install", ("sh", "-c", stamp), 60.0),
    )
    return [_step(step, label, host) for step in steps]


def _locate_script(recipe: Recipe) -> str:
    check = " ".join('"$1"' if part == "{exe}" else shlex.quote(part) for part in recipe.check) or "true"
    return (
        f'ok() {{ [ -x "$1" ] && {check} >/dev/null 2>&1; }}\n'
        f"for p in {' '.join(map(shlex.quote, recipe.candidates))}; do"
        ' ok "$p" && { echo "$p"; exit 0; }; done\n'
        f"for c in {' '.join(map(shlex.quote, recipe.commands))}; do"
        ' p=$(command -v "$c") && ok "$p" && { echo "$p"; exit 0; }; done\n'
        "exit 1"
    )


def locate(recipe: Recipe, host: Host = LOCAL, timeout: float = 60.0) -> tuple[str, str]:
    """(READY, executable) or (MISSING | UNREACHABLE, ""). Blocks on a subprocess: keep it off UI threads."""
    try:
        result = subprocess.run(
            host.argv(_locate_script(recipe)),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (UNREACHABLE if host.address else MISSING), ""
    found = result.stdout.strip().splitlines()
    if result.returncode == 0 and found:
        return READY, found[-1]
    return (UNREACHABLE if host.address and result.returncode == _SSH_CONNECT_FAILED else MISSING), ""


def status(recipe: Recipe, host: Host = LOCAL) -> str:
    """READY, MISSING or UNREACHABLE. Blocks like `locate`, which also says where the tool is."""
    return locate(recipe, host)[0]


# ----------------------------------------------------------------------------
# Whose the prefix is, and whether it holds what the recipe asks for
# ----------------------------------------------------------------------------

def _owner(recipe: Recipe, host: Host) -> str:
    """The stamp of a prefix ms_env installed, `_USER` for one that holds something else, "" for none."""
    script = (
        f'p={_prefix(recipe)}\n'
        f'[ -f "$p/{STAMP}" ] && {{ cat "$p/{STAMP}"; exit 0; }}\n'
        f'[ -f "$p{_INSTALLING}" ] && {{ echo installing; exit 0; }}\n'
        f'[ -n "$(ls -A "$p" 2>/dev/null)" ] && echo {_USER}\nexit 0'
    )
    return host.run(script, timeout=60).decode().strip()


def _pin(spec: str) -> tuple[str, str]:
    """(name, version) of `name=1.2`, `name==1.2` or `name[extra]==1.2`. A range pins no version."""
    name, version = re.match(r"\s*([\w.-]*)(?:\[[^]]*])?\s*(?:==?\s*([\w.!+-]+))?", spec).groups()
    return name, (version or "").rstrip(".")


# Run by the python of the prefix. ponytail: versions compare as text, so 1.0 is not 1.0.0;
# `packaging` on this side if a recipe ever pins that loosely.
_PIP_PINS = """import sys
from importlib.metadata import PackageNotFoundError, version
for pin in sys.argv[1:]:
    name, _, wanted = pin.partition("==")
    try:
        found = version(name)
    except PackageNotFoundError:
        found = "nothing"
    if found == "nothing" or (wanted and found != wanted):
        print(f"pip: {pin} is asked for, {found} is there")
"""


def verify(recipe: Recipe, host: Host = LOCAL) -> list[str]:
    """What the environment at the recipe's prefix lacks of what the recipe asks for; [] when it meets it.

    The way to take an environment the user already has instead of installing one: every
    pinned conda package (looked up in `conda-meta`), every pinned pip package (asked to the
    python of the prefix) and the recipe's check on the executables of that prefix. Reads
    only. ponytail: a pip URL or path is not looked up, and `>=` counts as "is there".
    """
    lines = [f'p={_prefix(recipe)}', '[ -d "$p" ] || { echo "there is no directory $p"; exit 0; }',
             'has() { ls "$p"/conda-meta/$1 >/dev/null 2>&1; }']
    for name, version in map(_pin, recipe.conda):
        # conda's `=1.2` takes 1.2 and 1.2.x; a file there is name-version-build.json
        globs = [f"{name}-{version}-*.json", f"{name}-{version}.*-*.json"] if version else [f"{name}-[0-9]*-*.json"]
        wanted = shlex.quote(f"conda: {name}{'=' + version if version else ''} is asked for and is not there")
        lines.append(" || ".join([*(f"has {shlex.quote(glob)}" for glob in globs), f"echo {wanted}"]))
    pins = ["==".join(filter(None, _pin(spec))) for spec in recipe.pip if "/" not in spec]
    if pins:
        lines.append(
            f'"$p/bin/python" -c {shlex.quote(_PIP_PINS)} {shlex.join(pins)} 2>/dev/null'
            ' || echo "pip: no working python at $p/bin/python"'
        )
    lacks = host.run("\n".join(lines), timeout=120).decode().splitlines()
    inside = tuple(path for path in recipe.candidates if path.startswith(recipe.prefix + "/"))
    if recipe.check and status(dataclasses.replace(recipe, candidates=inside, commands=()), host) != READY:
        lacks.append(f"check: `{shlex.join(recipe.check)}` passes for none of {', '.join(inside) or 'its executables'}")
    return lacks


def remove(recipe: Recipe, host: Host = LOCAL) -> bool:
    """Delete the prefix when ms_env installed it. False: nothing of ours there, nothing deleted."""
    key = f"{recipe.name}@{host.address or 'local'}"
    with _LOCK:
        current = _INSTALLS.get(key)
        if current is not None and not current.future.done():
            raise RuntimeError(f"{recipe.name} is being installed on {host.address or 'this machine'}.")
    script = (
        f'p={_prefix(recipe)}\n[ -f "$p/{STAMP}" ] || [ -f "$p{_INSTALLING}" ] || {{ echo no; exit 0; }}\n'
        f'rm -rf -- "$p" "$p{_INSTALLING}"'
    )
    return host.run(script, timeout=600).decode().strip() != "no"


# ----------------------------------------------------------------------------
# Installs: app-level background threads
# ----------------------------------------------------------------------------

@dataclass
class Install:
    key: str
    future: Future = field(default_factory=Future)
    lines: list[str] = field(default_factory=list)  # live log, appended from the install thread


_INSTALLS: dict[str, Install] = {}
_LOCK = threading.Lock()


def ensure(recipe: Recipe, host: Host = LOCAL, busy: Callable[[], str] | None = None) -> Install:
    """Make sure the tool is there: start installing it, or join the install already running.

    Returns at once: wait on ``future`` and read ``lines`` for the log. Nothing is installed
    where the tool is already usable. An install of ours made from another recipe is made
    again, or brought up to this one where it stands when the recipe says how (that is the
    update); an environment of the user is verified and left as it is.

    ``busy`` is asked only before an install of ours is changed: what it returns is why that
    cannot be done now (something is running from it), and the install fails saying so.
    """
    key = f"{recipe.name}@{host.address or 'local'}"
    with _LOCK:
        current = _INSTALLS.get(key)
        if current is not None and not current.future.done():
            return current
        install = _INSTALLS[key] = Install(key)
    # ponytail: one daemon thread per install; a pool if installs pile up.
    # Daemon on purpose: quitting the app must not wait 30 min for a conda solve.
    threading.Thread(target=_run_install, args=(install, recipe, host, busy), name=f"ms_env-{key}", daemon=True).start()
    return install


def _run_install(install: Install, recipe: Recipe, host: Host, busy: Callable[[], str] | None = None) -> None:
    install.future.set_running_or_notify_cancel()
    try:
        where = host.address or "this machine"
        owner = _owner(recipe, host)
        if owner == _USER:
            lacks = verify(recipe, host)
            if lacks:
                raise StepFailed(
                    f"{recipe.prefix} on {where} does not meet the {recipe.name} recipe:\n" + "\n".join(lacks)
                )
            install.lines.append(f"{recipe.prefix} on {where} meets the {recipe.name} recipe: used as it is.")
        elif owner in ("", _stamp_text(recipe)) and status(recipe, host) == READY:
            install.lines.append(f"{recipe.name} is already usable on {where}.")
        else:
            why = busy() if busy and owner.startswith("{") else ""  # a stamp: an install of ours that ended
            if why:
                raise StepFailed(f"{recipe.name} on {where} is not as this recipe asks, and is left as it is: {why}")
            for step in recipe_steps(recipe, host, refresh=bool(recipe.base) and _base(owner) == recipe.base):
                run_step(step, install.lines.append)
            state = status(recipe, host)
            if state != READY:
                raise StepFailed(f"{recipe.name}: every step ran but the tool is still {state} on {where}.")
    except Exception as exc:  # noqa: BLE001 -- whatever broke, dependents must see a failure
        install.lines.append(str(exc))
        install.future.set_exception(exc)
    else:
        install.future.set_result(None)


__all__ = [
    "Install",
    "MISSING",
    "READY",
    "Recipe",
    "RecipeStep",
    "STAMP",
    "ToolSpec",
    "UNREACHABLE",
    "ensure",
    "locate",
    "recipe_steps",
    "remove",
    "status",
    "verify",
]
