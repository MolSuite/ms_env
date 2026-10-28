# Guide

## A recipe

An app ships one TOML file per tool. Every section but `[tool]` and `[locate]`
is optional.

```toml
[tool]
name = "openbabel"
label = "OpenBabel"
version = "3.1.1"                         # pinned here; never a user option
prefix = "{tools_home}/{name}/{version}"  # the default

[locate]
paths = ["{bin}/obabel"]                  # where the install puts it; +".exe" on Windows
commands = ["obabel"]                     # looked up on PATH
check = ["{exe}", "-V"]                   # {exe} = the candidate; none: being there is enough

[env]                                     # a conda prefix at {prefix}
conda = ["openbabel={version}"]
pip = []
channels = ["conda-forge"]                # the default

[[install]]                               # plain steps, after [env]
name = "fetch the weights"
argv = ["{python}", "-m", "tool.fetch", "{prefix}/weights"]
timeout = 1800

[[install]]                               # a download, checked, per platform
name = "download the binary"
to = "{prefix}/tool"                      # an archive is unpacked into `to`; a file keeps ".exe"
download.linux-64 = { url = "https://.../tool_linux", sha256 = "..." }
download.win-64 = { url = "https://.../tool.exe", sha256 = "..." }
# the same file everywhere: download = { url = "...", sha256 = "..." }
```

`{bin}` and `{python}` are where a conda environment keeps them: `{prefix}/bin` and
`{prefix}/bin/python`, or `{prefix}/Library/bin` and `{prefix}/python.exe` on Windows.
Platforms are named as conda names them (`linux-64`, `linux-aarch64`, `osx-64`,
`osx-arm64`, `win-64`; `Host.platform()` says the host's). A download needs its sha256.
On a platform none of its downloads names, the tool has no build: `recipe.unsupported`
says so and `ensure` fails with it. A remote host gets a download made and checked here,
then sent over `ssh`: it needs no `curl`.

`ToolSpec.load(path)` reads the file, and `spec.recipe(tools_home)` binds its
templates to one host. `tools_home` is a path on that host.

```python
from ms_env import ToolSpec

spec = ToolSpec.load("openbabel.toml")
recipe = spec.recipe("/home/me/.myapp/tools")
```

`recipe()` takes three more choices of the user:

| Argument | What it is |
|---|---|
| `prefix=` | A location in place of the managed one: an environment of their own, or a place to install into |
| `extra_paths=` | Executables the user registered, tried before anything else |
| `manager=` | A conda-type program to use on the host in place of micromamba |
| `platform=` | The host's platform (`Host.platform()`); this machine's by default |

### Finding a tool

`locate(recipe, host)` returns `(READY, executable)`, or `(MISSING, "")`, or
`(UNREACHABLE, "")` when `ssh` could not connect. `status` returns the state
alone. The first candidate whose check exits 0 wins, in this order:

1. The paths the user registered.
2. The managed install under `tools_home`.
3. The commands on `PATH`.

Both calls block on a subprocess, so keep them off UI threads.

### Installing a tool

`ensure(recipe, host)` returns an `Install` at once and installs on a daemon
thread. Nothing is installed where the tool is already usable.

```python
from ms_env import ensure

install = ensure(recipe)
install.future.result()     # waits; raises when the install failed
print(*install.lines, sep="\n")
```

- `install.lines` is the live log, and its last line is the error when there is one.
- A second `ensure` of the same tool on the same host joins the install that is running.
- `[env]` is created with micromamba: the one on the host's `PATH`, or one
  downloaded into `tools_home`.

An app that shows the steps itself takes them from `recipe_steps(recipe, host)`
and runs them with `run_steps(steps, on_line, on_step)`.

## Whose an environment is

`ensure` stamps the prefix it installs. The stamp decides what may happen to it:

| At the prefix | `ensure` | `remove` |
|---|---|---|
| Nothing | Creates it | Nothing to delete |
| A stamp of this recipe | Nothing | Deletes it |
| A stamp of another recipe (the TOML changed) | Creates it again: the update | Deletes it |
| Something without a stamp: the user's own environment | Verifies it, never changes it | Leaves it |

```python
from ms_env import remove, verify

mine = spec.recipe("/home/me/.myapp/tools", prefix="/home/me/envs/babel")   # an environment I already have
verify(mine)    # [] when it meets the recipe, else what it lacks:
                # ["conda: openbabel=3.1.1 is asked for and is not there"]
remove(recipe)  # True: ms_env had installed it and it is gone
```

`verify` looks up every pinned conda package, every pinned pip package and the
recipe's check, and only reads. An executable registered on its own
(`extra_paths=`) is not an environment: it only has to pass the check.

### While something runs from it

An update replaces an install, so an app can refuse it while the install is in
use. `ensure` takes `busy=`, a function that returns why the install cannot be
changed now, or an empty string.

```python
ensure(recipe, host, busy=lambda: "2 jobs are running from it")
```

`busy` is asked only before an install of `ms_env` is changed. When it returns
a reason the install fails saying so and the prefix is left as it is. A prefix
that is missing, or already as the recipe asks, never asks.

## A host

`Host()` is this machine. `Host("me@login.hpc")` runs every command through the
system's `ssh` with `BatchMode=yes`:

- Users, keys and jumps come from `~/.ssh/config`.
- The key logs in or the command fails at once; no credential is handled.
- Nothing is left listening on the other side.

```python
from ms_env import Host

host = Host("me@login.hpc")
home = host.home()                  # the home of the account there
host.run("nproc").decode()          # what the command printed; HostError when it fails
```

A failed login says what to do: `ssh-copy-id` when the key is not accepted, a
first login by hand when the host key is not known yet.

## The environment of an app

`app_recipe` makes the recipe that gives a host the environment an app runs in
here: the same Python and the same versions of what this one has installed.

```python
from ms_env import Host, app_recipe, ensure

host = Host("me@login.hpc")
home = host.home()
recipe = app_recipe(
    "myapp",
    ["myapp", "ms_flow"],                     # packages: `name` or `name[extras]`
    ["myapp", "ms_flow"],                     # modules that have to import there
    prefix=f"{home}/.myapp/env",              # on the host
    tools_home=f"{home}/.myapp/tools",        # on the host
    cache="/home/me/.myapp/cache/wheels",     # on this machine
)
ensure(recipe, host).future.result()
```

What travels and what is downloaded:

- A package imported from a source checkout goes as a wheel of its code as it
  is now. The wheel is built once for each state of the code and of its tags:
  its version comes from the last tag, so the tags of a git checkout are
  fetched first. A release tagged on the server would otherwise give the wheel
  a development version below it, which the host refuses to install for an app
  that asks for that release.
- A package that is installed here is asked for in the version it has.
- Everything else the host downloads, pinned to the versions of this
  environment, so the two machines may be different systems.
- When an index lacks a version that conda installed here, the install tries
  again pinning only what pip installed.

The environment is updated where it stands for as long as the Python version
is the same; a new Python makes it again. `app_recipe` builds wheels the first
time and after the code changes, so keep it off UI threads.

## The environment manager

`find_environment_manager()` returns the program that creates environments on
this machine, or `None` when a recipe has to download its own micromamba.

1. The variables the app names, in the order given.
2. `MOLSUITE_ENV_MANAGER`.
3. `micromamba` on `PATH`.

Any of the variables may name a conda or a mamba.
