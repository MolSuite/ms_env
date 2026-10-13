# ms_env

Environments for MolSuite apps: find an external tool that already works, or install
it, on this machine or on a host reached over `ssh`. Standard library only.

A **recipe** says how to tell that a tool works and how to install it. A **host**
says where. `ms_env` applies one to the other.

```python
from ms_env import READY, Host, ToolSpec, ensure, locate

spec = ToolSpec.load("openbabel.toml")

recipe = spec.recipe("/home/me/.myapp/tools")      # bound to this machine
state, executable = locate(recipe)                  # (READY, ".../bin/obabel") or (MISSING, "")
if state != READY:
    install = ensure(recipe)                        # returns at once: a daemon thread installs
    install.future.result()                         # wait; `install.lines` is the live log

cluster = Host("me@login.hpc")                    # as `ssh` knows it
ensure(spec.recipe("/home/me/.myapp/tools"), cluster).future.result()
```

## A recipe

One TOML file per tool, shipped by the app:

```toml
[tool]
name = "openbabel"
label = "OpenBabel"
version = "3.1.1"                         # pinned here; never a user option
prefix = "{tools_home}/{name}/{version}"  # the default

[locate]
paths = ["{prefix}/bin/obabel"]           # where the install puts it
commands = ["obabel"]                     # looked up on PATH
check = ["{exe}", "-V"]                   # {exe} = the candidate

[env]                                     # a conda prefix at {prefix}
conda = ["openbabel={version}"]
pip = []

[[install]]                               # plain steps, after [env]
name = "download the model"
argv = ["curl", "-fsSL", "-o", "{prefix}/model.pt", "https://..."]
timeout = 1800
```

Lookup on a host, first candidate whose check exits 0 wins: the paths the user
registered, the managed install under `tools_home`, then `PATH`. `[env]` is created
with micromamba: the one on the host's `PATH`, or one downloaded into `tools_home`.
Another conda-type program is used only when the app names it (`manager=`).

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
recipe's check, and only reads. An executable registered on its own (`extra_paths=`)
is not an environment: it only has to pass the check.

`ensure(recipe, host, busy=why)` asks `why()` before it changes an install of its own:
a reason (`"2 jobs are running from it"`) makes the install fail and leaves the prefix
as it is.

## A host

`Host()` is this machine. `Host("me@login.hpc")` runs every command through the system's
`ssh` with `BatchMode=yes`: users, keys and jumps come from `~/.ssh/config`, the key
logs in or the command fails at once, no credential is handled and nothing is left
listening on the other side. A host is a POSIX machine; only Linux is supported.

## The environment of an app

`app_recipe` makes the recipe that gives a host the environment an app runs in here:
the same Python, the same versions, and the code of a source checkout as it is now.

```python
from ms_env import app_recipe

home = cluster.home()
recipe = app_recipe("myapp", ["myapp"], ["myapp"], prefix=f"{home}/.myapp/env",
                    tools_home=f"{home}/.myapp/tools", cache="/home/me/.myapp/cache/wheels")
ensure(recipe, cluster).future.result()
```

## Install

```bash
pip install ms_env
```

Documentation: <https://molsuite.github.io/ms_env/>
