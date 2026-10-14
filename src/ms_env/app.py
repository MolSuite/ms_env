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


def _tree_hash(root: Path, module: str) -> str:
    digest = hashlib.sha256((root / "pyproject.toml").read_bytes())
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


def _constraints(everything: bool) -> str:
    """`name==version` of what this environment holds: all of it, or what pip took from an index.

    A local build is never pinned (no index has it), nor a name this environment has two
    records of (which one runs is anyone's guess). What conda put here is pinned only when
    `everything`: an index may lack that version, which is why the install has a second try.
    """
    seen: dict[str, set[str]] = {}
    for dist in metadata.distributions():
        name, installer = dist.metadata.get("Name"), (dist.read_text("INSTALLER") or "").strip()
        if not name:  # a broken leftover in site-packages
            continue
        local = "+" in dist.version or (installer != "conda" and dist.read_text("direct_url.json"))
        pin = not local and (everything or installer in ("pip", "uv"))
        seen.setdefault(re.sub(r"[-_.]+", "-", name).lower(), set()).add(dist.version if pin else "")
    pins = (f"{name}=={next(iter(versions))}" for name, versions in seen.items() if len(versions) == 1)
    return "\n".join(sorted(pin for pin in pins if not pin.endswith("=="))) + "\n"


def app_recipe(
    name: str,
    requirements: Iterable[str],
    imports: Iterable[str],
    *,
    prefix: str,
    tools_home: str,
    cache: Path | str,
    manager: str = "",
) -> Recipe:
    """The recipe that gives a host the environment `requirements` run in here.

    `requirements` are the packages of the app, each `name` or `name[extras]`: one imported
    from a source checkout goes as a wheel of its code as it is now, one that is installed is
    asked for in the version it has. `imports` are the modules that have to import there for
    the environment to count as usable. `prefix` and `tools_home` are paths on the host (see
    `Host.home`); `cache` is a directory of this machine for the wheels.

    The environment is updated where it stands for as long as the Python is the same. Builds
    wheels the first time, and after the code changes: keep it off UI threads.
    """
    prefix, cache = str(prefix).rstrip("/"), Path(cache)
    code, wheels, wanted, tags = f"{prefix}/{_CODE}", [], [], []
    for requirement in requirements:
        dist, extras = re.fullmatch(r"\s*([\w.-]+)\s*(\[[^]]*])?\s*", requirement).groups()
        module = re.sub(r"[-.]+", "_", dist).lower()
        root = _checkout(module)
        if root is None:
            wanted.append(f"{dist}{extras or ''}=={metadata.version(dist)}")
            continue
        tags.append(_tree_hash(root, module))
        wheels.append(_wheel(root, tags[-1], cache))
        wanted.append(f"{code}/{wheels[-1].name}{extras or ''}")
    cache.mkdir(parents=True, exist_ok=True)
    constraints, loose = cache / "constraints.txt", cache / "constraints-pip.txt"
    constraints.write_text(_constraints(True), encoding="utf-8")
    loose.write_text(_constraints(False), encoding="utf-8")

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
            f"install {name} and what it needs",
            ("bash", "-c", pinned, f"{prefix}/bin/python", code, *wanted),
            3600.0,
        ),
    ]
    if wheels:  # the version of a checkout does not change with its code: pip would keep the old one
        refresh.append(
            RecipeStep(
                f"install the code of {name} as it is here",
                (*pip, "--no-deps", "--force-reinstall", *(f"{code}/{wheel.name}" for wheel in wheels)),
                600.0,
            )
        )
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
        stamp=short(conda, wanted, tags, constraints.read_text(encoding="utf-8")),
        refresh=tuple(refresh),
        base=short(conda),
    )


__all__ = ["app_recipe"]
