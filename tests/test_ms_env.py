"""Tool recipes: TOML binding, lookup order, env bootstrap script, project-less installs, whose a prefix is."""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

import ms_env as tools
from ms_env import Target, run_step

_TOML = """
[tool]
name = "faketool"
label = "Fake tool"
version = "1.0"

[locate]
paths = ["{prefix}/bin/faketool"]
commands = ["faketool"]
check = ["{exe}", "--ok"]

[env]
conda = ["faketool={version}"]
pip = ["extra pkg"]
"""


def _spec(tmp_path: Path, steps: str = "", pip: str = '"extra pkg"') -> tools.ToolSpec:
    path = tmp_path / "faketool.toml"
    path.write_text(_TOML.replace('"extra pkg"', pip) + steps)
    return tools.ToolSpec.load(path)


def _fake_exe(path: Path, *, works: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nexit {0 if works else 1}\n")
    path.chmod(0o755)
    return path


def test_spec_binds_per_target(tmp_path):
    spec = _spec(tmp_path)
    assert spec.info["label"] == "Fake tool"

    local = spec.recipe(str(tmp_path / "home"))
    assert local.prefix == f"{tmp_path}/home/faketool/1.0"
    assert local.candidates == (f"{local.prefix}/bin/faketool",)
    assert local.check == ("{exe}", "--ok")  # {exe} survives binding

    remote = spec.recipe("/scratch/tools", prefix="/opt/mine/", extra_paths=["/usr/local/bin/faketool"])
    assert remote.candidates == ("/usr/local/bin/faketool", "/opt/mine/bin/faketool")
    steps = tools.recipe_steps(remote, Target("mario@login.hpc"))
    assert [step.name.removeprefix("mario@login.hpc: ") for step in steps] == [
        "clear /opt/mine", "create the faketool environment", "pip install into faketool", "stamp the faketool install",
    ]
    assert steps[1].argv[:4] == ["ssh", "-o", "BatchMode=yes", "mario@login.hpc"]
    assert "/scratch/tools/micromamba" in steps[1].argv[-1]  # bootstrap lands under the remote home
    assert steps[2].argv[-1].endswith("'extra pkg'")  # one quoted remote command line
    assert remote.stamp == local.stamp  # what the TOML asks for, wherever it is applied


def test_locate_order_and_check(tmp_path, monkeypatch):
    recipe = _spec(tmp_path).recipe(str(tmp_path / "home"), extra_paths=[str(tmp_path / "user/faketool")])
    monkeypatch.setenv("PATH", f"{tmp_path / 'onpath'}:/usr/bin:/bin")
    assert tools.locate(recipe) == (tools.MISSING, "")

    on_path = _fake_exe(tmp_path / "onpath/faketool")
    assert tools.locate(recipe) == (tools.READY, str(on_path))
    managed = _fake_exe(Path(recipe.candidates[1]))
    assert tools.locate(recipe) == (tools.READY, str(managed))
    _fake_exe(tmp_path / "user/faketool", works=False)  # registered but broken: skipped
    assert tools.locate(recipe) == (tools.READY, str(managed))
    user = _fake_exe(tmp_path / "user/faketool")
    assert tools.locate(recipe) == (tools.READY, str(user))
    assert tools.status(recipe) == tools.READY


def test_env_step_uses_the_manager_it_finds(tmp_path, monkeypatch):
    log = tmp_path / "calls"
    manager = tmp_path / "fake-conda"
    manager.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    manager.chmod(0o755)
    recipe = _spec(tmp_path).recipe(str(tmp_path / "home"), manager=str(manager))
    run_step(tools.recipe_steps(recipe)[1])
    assert log.read_text().split() == [
        "create", "--yes", "--quiet", "--prefix", recipe.prefix, "--channel", "conda-forge", "faketool=1.0",
    ]

    # No hint: micromamba on PATH, before any download is attempted. A conda there is not taken.
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    _fake_exe(tmp_path / "bin/conda").write_text(f'#!/bin/sh\necho conda "$@" >> {log}\n')
    _fake_exe(tmp_path / "bin/micromamba").write_text(f'#!/bin/sh\necho micromamba "$@" >> {log}\n')
    assert tools.find_environment_manager() == tmp_path / "bin/micromamba"
    run_step(tools.recipe_steps(_spec(tmp_path).recipe(str(tmp_path / "home")))[1])
    assert log.read_text().splitlines()[-1].startswith("micromamba create")


def _plain_spec(tmp_path: Path, *, fail: bool = False, slow: bool = False) -> tools.Recipe:
    """No [env]: the install step just drops a working executable at the managed path."""
    script = "{prefix}/bin/faketool"
    steps = '\n[[install]]\nname = "wait"\nargv = ["sleep", "1"]\n' if slow else ""
    if fail:
        steps += '\n[[install]]\nname = "blow up"\nargv = ["sh", "-c", "echo boom detail; exit 3"]\n'
    steps += (
        '\n[[install]]\nname = "install"\n'
        f'argv = ["sh", "-c", "mkdir -p $(dirname {script}) && printf \'#!/bin/sh\\\\nexit 0\\\\n\' > {script} && chmod +x {script}"]\n'
    )
    path = tmp_path / "plain.toml"
    path.write_text(_TOML.replace('[env]\nconda = ["faketool={version}"]\npip = ["extra pkg"]\n', "") + steps)
    return tools.ToolSpec.load(path).recipe(str(tmp_path / f"home-{fail}-{slow}"))


def test_install_needs_no_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    recipe = _plain_spec(tmp_path, slow=True)
    install = tools.ensure(recipe)
    assert tools.ensure(recipe) is install  # joins the running install
    install.future.result(timeout=30)
    assert tools.status(recipe) == tools.READY
    again = tools.ensure(recipe)  # nothing to do: the tool is there
    again.future.result(timeout=30)
    assert again.lines == ["faketool is already usable on this machine."]

    broken = tools.ensure(_plain_spec(tmp_path, fail=True))
    assert broken.future.exception(timeout=30) is not None
    assert "boom detail" in broken.lines


def test_ms_env_updates_and_removes_what_it_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    recipe = _plain_spec(tmp_path)
    tools.ensure(recipe).future.result(timeout=30)
    stamp = Path(recipe.prefix, tools.STAMP)
    assert json.loads(stamp.read_text()) == {"name": "faketool", "stamp": recipe.stamp}

    Path(recipe.prefix, "left-over").write_text("of the install before")
    newer = dataclasses.replace(recipe, stamp="another recipe")  # the TOML changed
    tools.ensure(newer).future.result(timeout=30)
    assert not Path(recipe.prefix, "left-over").exists()  # made again, not patched
    assert json.loads(stamp.read_text())["stamp"] == "another recipe"

    assert tools.remove(newer) and not Path(recipe.prefix).exists()
    assert not tools.remove(newer)  # nothing of ours left

    cut = _plain_spec(tmp_path, fail=True)  # an install that stops halfway is still ours
    tools.ensure(cut).future.exception(timeout=30)
    Path(cut.prefix).mkdir(parents=True)
    Path(cut.prefix, "half").write_text("")
    assert tools.remove(cut) and not Path(cut.prefix).exists()


def test_the_environment_of_the_user_is_verified_and_never_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    mine = tmp_path / "mine"
    (mine / "conda-meta").mkdir(parents=True)
    (mine / "conda-meta/faketool-1.10-h0.json").write_text("{}")  # 1.10 is not 1.0
    _fake_exe(mine / "bin/faketool")
    _fake_exe(mine / "bin/python").write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    asks = _spec(tmp_path, pip='"pytest", "nosuchpkg[extra]==9.9"').recipe(str(tmp_path / "home"), prefix=str(mine))

    assert tools.verify(asks) == [
        "conda: faketool=1.0 is asked for and is not there",
        "pip: nosuchpkg==9.9 is asked for, nothing is there",
    ]
    refused = tools.ensure(asks).future.exception(timeout=30)
    assert "does not meet the faketool recipe" in str(refused) and "nosuchpkg" in str(refused)
    with pytest.raises(tools.StepFailed):  # the install itself refuses it too
        run_step(tools.recipe_steps(asks)[0])
    assert not tools.remove(asks) and (mine / "bin/faketool").exists()

    (mine / "conda-meta/faketool-1.0.2-h0.json").write_text("{}")
    meets = _spec(tmp_path, pip='"pytest"').recipe(str(tmp_path / "home"), prefix=str(mine))
    assert tools.verify(meets) == []
    used = tools.ensure(meets)
    used.future.result(timeout=30)
    assert used.lines == [f"{mine} on this machine meets the faketool recipe: used as it is."]
    assert not (mine / tools.STAMP).exists()

    _fake_exe(mine / "bin/faketool", works=False)
    assert tools.verify(meets) == [f"check: `'{{exe}}' --ok` passes for none of {mine}/bin/faketool"]


def test_a_target_runs_commands_and_says_why_one_failed():
    assert Target().run("cat; echo there", b"here ") == b"here there\n"
    assert Target("login.hpc").argv("ls") == ["ssh", "-o", "BatchMode=yes", "login.hpc", "ls"]
    with pytest.raises(tools.TargetError, match="this machine: no such thing"):
        Target().run("echo no such thing >&2; exit 3")


def test_run_steps_stops_at_the_first_failure():
    seen: list[str] = []
    steps = [
        tools.Step("ok", call=lambda: "fine"),
        tools.Step("boom", argv=["false"]),
        tools.Step("never", call=lambda: "unreachable"),
    ]
    with pytest.raises(tools.StepFailed):
        tools.run_steps(steps, on_line=seen.append)
    assert "unreachable" not in seen


