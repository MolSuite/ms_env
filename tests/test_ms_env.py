"""Tool recipes: TOML binding, lookup order, env bootstrap script, project-less installs."""
from __future__ import annotations

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


def _spec(tmp_path: Path, steps: str = "") -> tools.ToolSpec:
    path = tmp_path / "faketool.toml"
    path.write_text(_TOML + steps)
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
    assert steps[0].argv[:4] == ["ssh", "-o", "BatchMode=yes", "mario@login.hpc"]
    assert steps[0].name.startswith("mario@login.hpc: ")
    assert "/scratch/tools/micromamba" in steps[0].argv[-1]  # bootstrap lands under the remote home
    assert steps[1].argv[-1].endswith("'extra pkg'")  # one quoted remote command line


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
    run_step(tools.recipe_steps(recipe)[0])
    assert log.read_text().split() == [
        "create", "--yes", "--quiet", "--prefix", recipe.prefix, "--channel", "conda-forge", "faketool=1.0",
    ]

    # No hint: the one on PATH wins before any download is attempted.
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    _fake_exe(tmp_path / "bin/micromamba").write_text(f'#!/bin/sh\necho micromamba "$@" >> {log}\n')
    run_step(tools.recipe_steps(_spec(tmp_path).recipe(str(tmp_path / "home")))[0])
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


