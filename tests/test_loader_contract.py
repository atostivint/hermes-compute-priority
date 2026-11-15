"""Guards for the root ``__init__.py`` — the file the plugin loader requires and this plugin ignores.

Hermes' plugin scanner treats a directory plugin as "a ``plugin.yaml`` and an ``__init__.py``
exposing ``register(ctx)``" (``hermes_cli/plugins.py``). It does not merely document the shape:
``_load_directory_module`` raises ``FileNotFoundError("No __init__.py in <dir>")`` and
``_load_plugin_scoped`` reports that as ``Failed to load plugin '<name>': ...`` in
``logs/errors.log`` — three times per reload, about a plugin that is mounted and running, because
the loop is started by ``dashboard/plugin_api.py`` through ``dashboard/manifest.json``, not here.

The fix is a file that satisfies the check and does nothing, so these tests are written to fail if
it stops doing either half:

* the file exists and exposes a callable ``register`` (otherwise the loader's other branch fires:
  ``Plugin '<name>' has no register() function``);
* importing it and calling ``register`` are inert — no thread, no process-wide priority change, no
  registration, no import of the modules that could do any of that.

The last property is what keeps this honest: the proof that nothing renices a process is that
neither module able to do it (``platform_backend``, and ``compute_priority`` under it) is ever
imported by this file, and that ``register`` hands its context nothing.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import threading

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_INIT_PATH = _ROOT / "__init__.py"

# The names the loader can reach from here. Kept as a tuple so a failure message can name them.
_FORBIDDEN_MODULES = ("compute_priority", "platform_backend")


class _RecordingContext:
    """A stand-in for Hermes' ``PluginContext`` that records every call instead of performing it.

    ``__getattr__`` answers everything, so a plugin that registers *anything* (``ctx.register_hook``,
    ``register_tool``, ``register_command``, a future method) is caught without this test having to
    know the method's name: the call lands in ``calls`` and the assertion below fails naming it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return _record


def _load_as_the_loader_does():
    """Import this file the way ``_load_directory_module`` does: a package spec, ``exec_module``.

    The module name is ours (the loader uses ``hermes_plugins.<slug>``); what matters is that the
    spec is a *package* spec pointing at this directory, because that is what a relative import of
    a sibling module would resolve against — the thing an accidental ``import platform_backend``
    would exploit.
    """
    assert _INIT_PATH.is_file(), (
        "the plugin root has no __init__.py: hermes_cli.plugins._load_directory_module raises "
        'FileNotFoundError("No __init__.py in <dir>") and the scanner logs "Failed to load plugin '
        "'hermes-compute-priority'\" into logs/errors.log on every reload, about a plugin that is "
        "actually mounted. The file is required; it must stay."
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_compute_priority_loader_contract_under_test",
        _INIT_PATH,
        submodule_search_locations=[str(_ROOT)],
    )
    assert spec is not None and spec.loader is not None, f"Cannot create module spec for {_INIT_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract_module():
    return _load_as_the_loader_does()


def test_the_plugin_root_ships_the_init_py_the_loader_looks_for():
    # Existence alone; the import-side asserts live in the fixture so a missing file fails here
    # first, with the message that explains what Hermes does without it.
    assert _INIT_PATH.is_file()


def test_it_exposes_a_register_callable(contract_module):
    register = getattr(contract_module, "register", None)
    assert register is not None, (
        "no register(): hermes_cli.plugins._load_plugin_scoped sets loaded.error = 'no register() "
        'function\' and warns "Plugin \'hermes-compute-priority\' has no register() function"'
    )
    assert callable(register), "register must be callable: the loader calls register(ctx)"


def test_importing_it_creates_no_thread():
    """The scan thread belongs to ``dashboard/plugin_api.py``; this file must not start a second one."""
    before = {t.ident for t in threading.enumerate()}
    _load_as_the_loader_does()
    after = {t.ident for t in threading.enumerate()}
    assert after - before == set(), (
        "importing __init__.py started a thread; the compute-priority scan thread is started once, "
        "by dashboard/plugin_api.py when the dashboard loader imports it"
    )
    assert [t.name for t in threading.enumerate() if t.name == "compute-priority"] == []


def test_importing_it_imports_nothing_that_could_touch_a_process():
    """No ``platform_backend``, so no ``setpriority``/``psutil.Process.nice`` can be reached.

    Measured in a fresh interpreter, from the plugin root, so the answer cannot be polluted by
    another test module having imported ``plugin_api.py`` first (``sys.modules`` is process-wide)
    and so that a bare ``import platform_backend`` inside ``__init__.py`` — which *would* succeed,
    the root being on ``sys.path`` — is exactly what the probe would see.
    """
    names = ", ".join(repr(n) for n in _FORBIDDEN_MODULES)
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('loader_contract_probe', {str(_INIT_PATH)!r}, "
        f"submodule_search_locations=[{str(_ROOT)!r}])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"print(', '.join(n for n in ({names},) if n in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=str(_ROOT), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, f"the probe failed:\n{result.stdout}\n{result.stderr}"
    assert result.stdout.strip() == "", (
        f"importing __init__.py pulled in {result.stdout.strip()}; only dashboard/plugin_api.py "
        "needs them, and importing them here would make a shape-check file able to renice processes"
    )


def test_register_is_inert(contract_module):
    """It registers nothing, touches nothing, and its return value (which the loader ignores) is None."""
    ctx = _RecordingContext()
    thread_count = threading.active_count()
    result = contract_module.register(ctx)
    assert ctx.calls == [], f"register() called {ctx.calls} on its context; this plugin registers nothing"
    assert result is None, "register() must not return a value the loader might start honouring"
    assert threading.active_count() == thread_count
