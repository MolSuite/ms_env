# ms_env

`ms_env` gives a MolSuite app the external tools it needs: it finds one that
already works, or installs it, on this machine or on a host reached over `ssh`.
It is the lowest layer of MolSuite and uses the standard library only.

A **recipe** says how to tell that a tool works and how to install it. A
**host** says where. `ms_env` applies one to the other.

## Install

```bash
pip install ms_env
```

The package requires Python 3.12 or newer. This machine may be Linux, macOS or Windows; a remote host is a POSIX machine.

## First use

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

## Where next

- The [guide](guide.md) explains recipes, whose an environment is, hosts and
  the environment of an app.
- The [API reference](api_reference.md) lists every public name.
