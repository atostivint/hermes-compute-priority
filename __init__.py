"""The plugin loader's contract file — and nothing else.

``hermes_cli/plugins.py`` describes a directory plugin as "a directory with a ``plugin.yaml`` and
an ``__init__.py`` exposing ``register(ctx)``", and the loader enforces the shape before it reads a
single registration: ``_load_directory_module`` raises ``FileNotFoundError("No __init__.py in
<dir>")``, which ``_load_plugin_scoped`` turns into ``Failed to load plugin '<name>': ...``. That
warning lands in ``logs/agent.log`` and ``logs/errors.log`` on every plugin reload.

Nothing was broken by its absence. This plugin's behaviour lives in ``dashboard/plugin_api.py``,
which the *dashboard* discovery walk (``<plugin>/dashboard/manifest.json``) imports — a different
path that never reads this file. But a log line claiming a plugin failed to load, about a plugin
that is loaded and running, is a trap for whoever diagnoses this host later. So this file exists to
satisfy the loader's shape check.

``register(ctx)`` is required too — an absent ``__init__.py`` and a present file without
``register`` produce two different warnings — and its return value is ignored: a plugin that
registers nothing is a valid plugin. This one registers exactly that: no hook, no tool, no thread,
no watcher, nothing imported at module scope, nothing touched when ``register`` is called.
``tests/test_loader_contract.py`` asserts those properties, so the inertness is not a promise in a
docstring.
"""


def register(ctx):
    """Satisfy the loader's shape check without registering anything (see the module docstring)."""
    return None
