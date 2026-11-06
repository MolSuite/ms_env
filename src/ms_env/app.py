"""The environment of an app on a host: the Python that runs here, with the same versions of
what this one has installed.

Nothing is copied but the code that runs here from a source checkout, which travels as wheels
built from it. Everything else the host downloads, pinned to what this environment holds, so
the two may be different systems.
"""
from __future__ import annotations

import hashlib
import importlib.util
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path
from typing import Iterable

from ms_env.recipes import Recipe, RecipeStep, _env_steps

_CODE = ".ms_env.d"  # inside the prefix: the wheels and the constraints of the last install


def _checkout(module: str) -> Path | None:
    """The project `module` is imported from, when that is a source checkout and not an install."""
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin:
        raise ModuleNotFoundError(f"{module} does not import here, so no host can be given the same.")
    origin = Path(spec.origin).resolve()
    if {"site-packages", "dist-packages"} & set(origin.parts):
        return None
    return next((parent for parent in origin.parents if (parent / "pyproject.toml").is_file()), None)


def _described(root: Path) -> str:
    """`git describe` of the checkout at `root`: the version of its wheel comes from the last tag,
    which may change while the code does not. Without git, nothing."""
    if not (root / ".git").exists():
        return ""
    try:
        return subprocess.run(["git", "-C", str(root), "describe", "--tags", "--long", "--always"],
                              capture_output=True, text=True, timeout=30, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _tree_hash(root: Path, module: str) -> str:
    """What the wheel of the checkout depends on: its code and the tag its version comes from."""
    digest = hashlib.sha256((root / "pyproject.toml").read_bytes() + _described(root).encode())
    package = Path(importlib.util.find_spec(module).origin).resolve().parent
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(package)).encode() + path.read_bytes())
    return digest.hexdigest()[:16]


def _wheel(root: Path, tag: str, cache: Path) -> Path:
    """The wheel of the checkout at `root` as it is now, built once for each state of its code."""
    out = cache / f"{root.name}-{tag}"
    if not list(out.glob("*.whl")):
        for old in cache.glob(f"{root.name}-*"):
            shutil.rmtree(old, ignore_errors=True)
        done = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "--wheel-dir", str(out), str(root)],
            capture_output=True, text=True, check=False,
        )
        if done.returncode or not list(out.glob("*.whl")):
            raise RuntimeError(f"no wheel could be built from {root}:\n{done.stderr.strip()[-2000:]}")
    return next(out.glob("*.whl"))


def _constraints() -> tuple[str, str]:
    """`name==version` of what this environment holds: all of it, and what pip took from an index.

    A local build is never pinned (no index has it), nor a name this environment has two
    records of (which one runs is anyone's guess). What conda put here is pinned only in the
    first: an index may lack that version, which is why the install has a second try.
    """
    seen: dict[str, set[tuple[str, bool]]] = {}
    for dist in metadata.distributions():  # one pass: reading every record is most of what this costs
        name, installer = dist.metadata.get("Name"), (dist.read_text("INSTALLER") or "").strip()
        if not name:  # a broken leftover in site-packages
            continue
        local = "+" in dist.version or (installer != "conda" and dist.read_text("direct_url.json"))
        seen.setdefault(re.sub(r"[-_.]+", "-", name).lower(), set()).add(
            ("" if local else dist.version, installer in ("pip", "uv")))

    def pins(everything: bool) -> str:
        versions = {name: {version if everything or by_pip else "" for version, by_pip in records}
                    for name, records in seen.items()}
        return "\n".join(sorted(f"{name}=={next(iter(found))}" for name, found in versions.items()
                                if len(found) == 1 and next(iter(found)))) + "\n"

    return pins(True), pins(False)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requires(source: Path | str) -> list[str]:
    """The `Requires-Dist` of a built wheel (a Path) or of an installed distribution (a name)."""
    if isinstance(source, str):
        return metadata.requires(source) or []
    with zipfile.ZipFile(source) as wheel:
        found = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
        return BytesParser().parsebytes(wheel.read(found)).get_all("Requires-Dist") or []


def _needs(shipped: dict[str, tuple[list[str], set[str]]], leave_out: set[str]) -> list[str]:
    """What the packages in `shipped` (canonical name: their `Requires-Dist`, the extras asked of
    them) need besides each other, without any name in `leave_out`: those are not installed there,
    and neither is what only they would have brought."""
    extras = {name: {_canonical(extra) for extra in asked} for name, (_, asked) in shipped.items()}
    while True:
        needs, grew = set(), False
        for name, (requires, _) in shipped.items():
            for line in requires:
                head, _, marker = line.partition(";")
                gated = {_canonical(extra) for extra in re.findall(r"extra\s*==\s*[\"']([^\"']+)", marker)}
                # ponytail: the rest of an extra's marker is dropped with it; none of ours has one
                if gated and not gated & extras[name]:
                    continue
                target = _canonical(re.match(r"\s*([\w.-]+)", head).group(1))
                if target in leave_out:
                    continue
                if target in shipped:
                    asked = {_canonical(extra) for extra in re.findall(r"[\w.-]+", head.partition("[")[2].partition("]")[0])}
                    grew |= not asked <= extras[target]
                    extras[target] |= asked
                    continue
                needs.add(head.strip() if gated else line.strip())
        if not grew:
            return sorted(needs)


def app_recipe(
    name: str,
    requirements: Iterable[str],
    imports: Iterable[str],
    *,
    prefix: str,
    tools_home: str,
    cache: Path | str,
    manager: str = "",
    leave_out: Iterable[str] = (),
) -> Recipe:
    """The recipe that gives a host the environment `requirements` run in here.

    `requirements` are the packages of the app, each `name` or `name[extras]`: one imported
    from a source checkout goes as a wheel of its code as it is now, one that is installed is
    asked for in the version it has. `imports` are the modules that have to import there for
    the environment to count as usable. `prefix` and `tools_home` are paths on the host (see
    `Host.home`); `cache` is a directory of this machine for the wheels.
    `leave_out` names what the host goes without, though the packages ask for it (a GUI where
    only their jobs run): what only those would bring is left out too.

    The environment is updated where it stands for as long as the Python is the same. Builds
    wheels the first time, and after the code changes: keep it off UI threads.
    """
    prefix, cache = str(prefix).rstrip("/"), Path(cache)
    code, wheels, wanted, tags, shipped = f"{prefix}/{_CODE}", [], [], [], {}
    for requirement in requirements:
        dist, extras = re.fullmatch(r"\s*([\w.-]+)\s*(\[[^]]*])?\s*", requirement).groups()
        module = re.sub(r"[-.]+", "_", dist).lower()
        root = _checkout(module)
        if root is None:
            wanted.append(f"{dist}=={metadata.version(dist)}")
        else:
            tags.append(_tree_hash(root, module))
            wheels.append(_wheel(root, tags[-1], cache))
            wanted.append(f"{code}/{wheels[-1].name}")
        shipped[_canonical(dist)] = (_requires(wheels[-1] if root else dist), set(re.findall(r"[\w.-]+", extras or "")))
    needs = _needs(shipped, {_canonical(name) for name in leave_out})
    cache.mkdir(parents=True, exist_ok=True)
    constraints, loose = cache / "constraints.txt", cache / "constraints-pip.txt"
    everything, by_pip = _constraints()
    constraints.write_text(everything, encoding="utf-8")
    loose.write_text(by_pip, encoding="utf-8")

    pip = (f"{prefix}/bin/python", "-m", "pip", "install", "--progress-bar", "off")
    # ponytail: one version the index lacks drops every pin of what conda installed here, not
    # just that one. Drop them one by one if a host ever ends up too far from this environment.
    pinned = (
        'pip() { "$0" -m pip install --progress-bar off --constraint "$1" "${@:2}"; }; d=$1; shift; '
        'pip "$d/constraints.txt" "$@" || '
        '{ echo "not every version here can be had there: pinning what pip installed only"; '
        'pip "$d/constraints-pip.txt" "$@"; }'
    )
    refresh = [
        RecipeStep(
            f"send the code of {name}",
            ("sh", "-c", 'rm -rf "$0" && mkdir -p "$0" && tar -x -C "$0"', code),
            600.0,
            send=(str(constraints), str(loose), *map(str, wheels)),
        ),
        RecipeStep(
            f"install what {name} needs",
            ("bash", "-c", pinned, f"{prefix}/bin/python", code, *needs),
            3600.0,
        ),
        # Its own packages without what they ask for: `needs` is that, less `leave_out`. Forced, as
        # the version of a checkout does not change with its code and pip would keep the old one.
        RecipeStep(f"install {name}", (*pip, "--no-deps", "--force-reinstall", *wanted), 600.0),
    ]
    conda = (f"python={platform.python_version()}", "pip")
    values = {"prefix": prefix, "tools_home": str(tools_home).rstrip("/")}
    short = lambda *parts: hashlib.sha256(repr(parts).encode()).hexdigest()[:16]  # noqa: E731
    return Recipe(
        name=name,
        prefix=prefix,
        candidates=(f"{prefix}/bin/python",),
        commands=(),
        check=("{exe}", "-c", "import " + ", ".join(imports)),
        steps=(*_env_steps(name, conda, (), ("conda-forge",), values, manager), *refresh),
        conda=conda,
        stamp=short(conda, wanted, needs, tags, constraints.read_text(encoding="utf-8")),
        refresh=tuple(refresh),
        base=short(conda),
    )


__all__ = ["app_recipe"]
