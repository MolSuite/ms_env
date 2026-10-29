"""Tool recipes: TOML binding, lookup order, env bootstrap script, project-less installs, whose a prefix is."""
from __future__ import annotations

import subprocess
import dataclasses
import hashlib
import os
import json
import sys
from pathlib import Path

import pytest

import ms_env as tools
from ms_env import Host, run_step

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


def test_spec_binds_per_host(tmp_path):
    spec = _spec(tmp_path)
    assert spec.info["label"] == "Fake tool"

    local = spec.recipe(str(tmp_path / "home"))
    assert local.prefix == f"{tmp_path}/home/faketool/1.0"
    assert local.candidates == (f"{local.prefix}/bin/faketool",)
    assert local.check == ("{exe}", "--ok")  # {exe} survives binding

    remote = spec.recipe("/scratch/tools", prefix="/opt/mine/", extra_paths=["/usr/local/bin/faketool"])
    assert remote.candidates == ("/usr/local/bin/faketool", "/opt/mine/bin/faketool")
    steps = tools.recipe_steps(remote, Host("mario@login.hpc"))
    assert [step.name.removeprefix("mario@login.hpc: ") for step in steps] == [
        "prepare /opt/mine", "create the faketool environment", "pip install into faketool", "record the faketool install",
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


def test_installs_on_one_host_take_turns_and_count_their_steps(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    recipes = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        recipes.append(dataclasses.replace(_plain_spec(tmp_path / name, slow=True), name=f"faketool-{name}"))
    start = time.monotonic()
    installs = [tools.ensure(recipe) for recipe in recipes]
    for install in installs:
        install.future.result(timeout=30)
    assert time.monotonic() - start >= 2  # one `sleep 1` after the other: they share the host's micromamba
    # the check, "prepare", the two steps of the recipe, the stamp
    assert [(install.done, install.total) for install in installs] == [(5, 5), (5, 5)]
    again = tools.ensure(recipes[0])
    again.future.result(timeout=30)
    assert (again.done, again.total) == (1, 1)  # only the check: nothing to install


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


def test_a_host_runs_commands_and_says_why_one_failed():
    assert Host().run("cat; echo there", b"here ") == b"here there\n"
    assert Host("login.hpc").argv("ls") == ["ssh", "-o", "BatchMode=yes", "login.hpc", "ls"]
    with pytest.raises(tools.HostError, match="this machine: no such thing"):
        Host().run("echo no such thing >&2; exit 3")


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




def test_a_recipe_with_a_base_is_updated_where_it_stands(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    sent = tmp_path / "code.txt"
    sent.write_text("one")
    recipe = _plain_spec(tmp_path)
    refresh = (tools.RecipeStep("send the code", ("tar", "-x", "-C", recipe.prefix), 30.0, send=(str(sent),)),)
    recipe = dataclasses.replace(recipe, base="python 3.12", refresh=refresh, steps=(*recipe.steps, *refresh))
    tools.ensure(recipe).future.result(timeout=30)
    assert Path(recipe.prefix, "code.txt").read_text() == "one"

    Path(recipe.prefix, "left-over").write_text("of the install before")
    sent.write_text("two")
    newer = dataclasses.replace(recipe, stamp="the code changed")
    tools.ensure(newer).future.result(timeout=30)
    assert Path(recipe.prefix, "left-over").exists()  # the prefix was not made again
    assert Path(recipe.prefix, "code.txt").read_text() == "two"
    assert json.loads(Path(recipe.prefix, tools.STAMP).read_text()) == {
        "name": "faketool", "stamp": "the code changed", "base": "python 3.12"}

    held = dataclasses.replace(newer, stamp="changed with a job running from it")
    error = tools.ensure(held, busy=lambda: "2 jobs run from it").future.exception(timeout=30)
    assert "2 jobs run from it" in str(error)
    assert json.loads(Path(recipe.prefix, tools.STAMP).read_text())["stamp"] == "the code changed"  # untouched
    tools.ensure(newer, busy=lambda: "2 jobs run from it").future.result(timeout=30)  # as asked: nobody is asked

    other = dataclasses.replace(newer, base="python 3.13", stamp="another python")
    tools.ensure(other).future.result(timeout=30)
    assert not Path(recipe.prefix, "left-over").exists()  # another base: from nothing


def test_the_app_recipe_sends_checkouts_as_wheels_and_pins_the_rest(tmp_path):
    from ms_env import app

    recipe = tools.app_recipe("app", ["ms_env", "pytest"], ["ms_env"], prefix="/far/env", tools_home="/far/tools",
                              cache=tmp_path)
    wheel = next(tmp_path.glob("ms_env-*/ms_env-*.whl"))  # this checkout, built
    install = recipe.refresh[1].argv
    assert f"/far/env/.ms_env.d/{wheel.name}" in install and f"pytest=={pytest.__version__}" in install
    assert set(recipe.refresh[0].send) == {str(tmp_path / "constraints.txt"), str(tmp_path / "constraints-pip.txt"),
                                           str(wheel)}
    pins = (tmp_path / "constraints.txt").read_text().splitlines()
    assert f"pytest=={pytest.__version__}" in pins and not any(pin.startswith("ms-env==") for pin in pins)
    assert set((tmp_path / "constraints-pip.txt").read_text().splitlines()) <= set(pins)
    assert recipe.base and recipe.steps[-len(recipe.refresh):] == recipe.refresh
    again = tools.app_recipe("app", ["ms_env", "pytest"], ["ms_env"], prefix="/far/env", tools_home="/far/tools",
                             cache=tmp_path)
    assert again.stamp == recipe.stamp and app._checkout("pytest") is None


def _download_spec(tmp_path: Path, builds: dict[str, Path], to: str = "{prefix}/faketool") -> tools.ToolSpec:
    """A tool that is one downloaded file per platform; `file://` URLs stand for the release."""
    import hashlib

    table = "".join(
        f'download{"." * bool(where)}{where} = {{ url = "{path.as_uri()}", sha256 = "{hashlib.sha256(path.read_bytes()).hexdigest()}" }}\n'
        for where, path in builds.items()
    )
    path = tmp_path / "download.toml"
    path.write_text(
        '[tool]\nname = "faketool"\nversion = "1.0"\n[locate]\npaths = ["{prefix}/faketool"]\ncheck = ["{exe}"]\n'
        f'[[install]]\nname = "download faketool"\nto = "{to}"\n{table}'
    )
    return tools.ToolSpec.load(path)


def test_a_download_is_picked_per_platform_checked_and_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    build = _fake_exe(tmp_path / "release/faketool_linux")
    spec = _download_spec(tmp_path, {"linux-64": build, "win-64": tmp_path / "release/faketool_linux"})
    home = str(tmp_path / "home")
    assert spec.recipe(home, platform="osx-arm64").unsupported == "faketool has no build for osx-arm64"
    with pytest.raises(tools.StepFailed, match="no build for osx-arm64"):
        tools.recipe_steps(spec.recipe(home, platform="osx-arm64"))

    recipe = spec.recipe(home, platform="linux-64")
    assert recipe.steps[0].url == build.as_uri() and recipe.steps[0].to == f"{recipe.prefix}/faketool"
    tools.ensure(recipe).future.result(timeout=30)
    assert tools.locate(recipe) == (tools.READY, f"{recipe.prefix}/faketool")  # saved under `to`, executable

    build.write_text("#!/bin/sh\nexit 0\n# another build under the same name\n")
    tampered = spec.recipe(str(tmp_path / "other"), platform="linux-64")
    error = tools.ensure(dataclasses.replace(tampered, steps=recipe.steps, prefix=tampered.prefix)).future
    assert "is not the file the recipe names" in str(error.exception(timeout=30))
    with pytest.raises(ValueError, match="no sha256"):
        (tmp_path / "bad.toml").write_text(
            '[tool]\nname = "x"\n[locate]\n[[install]]\nname = "d"\ndownload = { url = "https://x" }\n')
        tools.ToolSpec.load(tmp_path / "bad.toml")


def test_an_archive_is_unpacked_and_a_remote_host_with_no_way_out_gets_the_download_sent(tmp_path, monkeypatch):
    import tarfile
    import zipfile

    archive = tmp_path / "release/faketool.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("index.html", "<html/>")
    recipe = _download_spec(tmp_path, {"": archive}, to="{prefix}").recipe(str(tmp_path / "home"))
    run_step(tools.recipe_steps(recipe)[1])
    assert Path(recipe.prefix, "index.html").read_text() == "<html/>"

    sent: list[tuple[str, bytes]] = []

    def run(self, command, data=b"", timeout=0):  # a host with no way out: what it cannot download is sent
        if command.startswith("python3"):
            raise tools.HostError("no route to host")
        return sent.append((command, data)) or b""

    monkeypatch.setattr(Host, "run", run)
    far = _download_spec(tmp_path, {"": _fake_exe(tmp_path / "release/faketool")}).recipe("/far/tools")
    run_step(tools.recipe_steps(far, Host("login.hpc"))[1])
    (command, data), = sent
    assert command == "mkdir -p /far/tools/faketool/1.0 && tar -x -C /far/tools/faketool/1.0"
    with tarfile.open(fileobj=__import__("io").BytesIO(data)) as tar:
        assert [(entry.name, entry.mode) for entry in tar] == [("faketool", 0o755)]


def test_windows_layout_and_platform_names():
    spec = tools.ToolSpec(name="t", version="1", info={}, prefix="{tools_home}/t", paths=("{bin}/t",), commands=(),
                          check=(), pip=("p",))
    win = spec.recipe("C:\\Users\\me\\tools\\", platform="win-64")
    assert win.prefix == "C:\\Users\\me\\tools/t" and win.candidates == (f"{win.prefix}/Library/bin/t",)
    assert win.steps[1].argv[0] == f"{win.prefix}/python.exe"
    assert spec.recipe("/h", platform="linux-64").candidates == ("/h/t/bin/t",)
    assert tools.conda_platform("Windows", "AMD64") == "win-64"
    assert tools.conda_platform("Darwin", "arm64") == "osx-arm64"
    assert tools.conda_platform("Linux", "x86_64") == "linux-64" == Host().platform() or sys.platform != "linux"


def test_a_new_tag_on_the_same_code_builds_a_new_wheel(tmp_path):
    from ms_env import app

    def git(*words):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *words],
                       check=True, capture_output=True)

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("tag", "v0.1.0")
    git("commit", "-q", "--allow-empty", "-m", "docs only")
    before = app._described(tmp_path)
    git("tag", "v0.2.0")  # a release of the same code: its wheel has another version
    assert before.startswith("v0.1.0-1-g") and app._described(tmp_path).startswith("v0.2.0-0-g")
    assert app._described(tmp_path / "not-a-checkout") == ""


def test_a_folder_of_many_files_is_sent_as_a_tar_of_its_size(tmp_path):
    import resource
    import tarfile
    from io import BytesIO

    from ms_env.recipes import _tar

    for i in range(300):  # each file once took a deep copy of all packed before it: GBs by 100
        (tmp_path / "pack" / f"sub{i % 10}").mkdir(parents=True, exist_ok=True)
        (tmp_path / "pack" / f"sub{i % 10}" / f"f{i}.js").write_bytes(b"x" * 20_000)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    data = _tar([str(tmp_path / "pack")])
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 200 * 1024  # kB
    with tarfile.open(fileobj=BytesIO(data)) as tar:
        members = tar.getmembers()
    assert len([m for m in members if m.isfile()]) == 300 and all(m.mode == 0o755 for m in members)


def test_a_host_downloads_for_itself_and_is_sent_the_file_only_when_it_cannot(tmp_path):
    from ms_env.recipes import RecipeStep, _step

    payload = tmp_path / "tool.bin"
    payload.write_bytes(b"#!/bin/sh\necho hi\n")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()

    class Far(Host):  # a "remote" host that is this machine; `blocked` is a host with no way out
        blocked = False

        def argv(self, command):
            if self.blocked and command.startswith("python3"):
                return ["sh", "-c", "echo 'no route to host' >&2; exit 1"]
            return ["sh", "-c", command]

    for blocked in (False, True):
        host, said = Far("far"), []
        host.blocked = blocked
        to = tmp_path / f"there-{blocked}" / "tool"
        _step(RecipeStep("get", (), 60.0, url=payload.as_uri(), sha256=digest, to=str(to)), "", host).run(said.append)
        assert to.read_bytes() == payload.read_bytes() and os.access(to, os.X_OK)
        assert any("sending it from here" in line for line in said) == blocked
