# ms_env

Environments for MolSuite apps: find an external tool that already works, or install
it, on this machine or on a host reached over `ssh`. Standard library only.

A **recipe** says how to tell that a tool works and how to install it. A **target**
says where. `ms_env` applies one to the other.

```python
from ms_env import READY, Target, ToolSpec, ensure, locate

spec = ToolSpec.load("openbabel.toml")

recipe = spec.recipe("/home/me/.myapp/tools")      # bound to this machine
state, executable = locate(recipe)                  # (READY, ".../bin/obabel") or (MISSING, "")
if state != READY:
    install = ensure(recipe)                        # returns at once: a daemon thread installs
    install.future.result()                         # wait; `install.lines` is the live log

cluster = Target("me@login.hpc")                    # as `ssh` knows it
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

Lookup on a target, first candidate whose check exits 0 wins: the paths the user
registered, the managed install under `tools_home`, then `PATH`. With no
conda, mamba or micromamba on the target, `[env]` downloads micromamba into
`tools_home` first.

## A target

`Target()` is this machine. `Target("host")` runs every command through the system's
`ssh` with `BatchMode=yes`: users, keys and jumps come from `~/.ssh/config`, the key
logs in or the command fails at once, no credential is handled and nothing is left
listening on the other side. Targets are POSIX hosts; only Linux is supported.

## Install

```bash
pip install ms_env
```
