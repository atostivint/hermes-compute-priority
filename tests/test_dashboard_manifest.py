"""Guards for ``dashboard/manifest.json`` — the dashboard's only entry point to this plugin.

Hermes mounts a plugin backend exclusively through ``<plugin>/dashboard/manifest.json``: the
discovery walk in ``hermes_cli/web_server_dashboard.py`` skips any plugin directory that has no
such file, and only the ``api`` key it declares makes the loader import ``plugin_api.py`` and call
``app.include_router(router, prefix="/api/plugins/<name>")``. Version 0.2.0 shipped
``dashboard/plugin_api.py`` without the manifest, so the loop never started and
``/api/plugins/hermes-compute-priority/status`` did not exist. These tests fail if that regresses,
and keep the manifest's declared version equal to ``plugin.yaml``'s (Hermes reports them
separately; a mismatch makes an install claim one version and run another).

PyYAML is deliberately not imported: CI installs pytest only, so ``plugin.yaml`` is read with an
anchored regex on the top-level ``version:`` line.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _ROOT / "dashboard" / "manifest.json"
_PLUGIN_YAML_PATH = _ROOT / "plugin.yaml"


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert _MANIFEST_PATH.is_file(), (
        "dashboard/manifest.json is missing: Hermes only discovers a dashboard plugin through "
        "<plugin>/dashboard/manifest.json, so without it dashboard/plugin_api.py is never imported "
        "and no /api/plugins/hermes-compute-priority/ route exists."
    )
    data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    assert isinstance(data, dict), "the manifest must be a JSON object"
    return data


def _declared_version() -> str:
    """``version:`` of ``plugin.yaml``, without a YAML dependency (CI has pytest only)."""
    text = _PLUGIN_YAML_PATH.read_text(encoding="utf-8-sig")
    match = re.search(r"^version:[ \t]*(.+?)[ \t]*$", text, re.MULTILINE)
    assert match is not None, "plugin.yaml has no top-level 'version:' line"
    return match.group(1).strip().strip("\"'")


def test_the_manifest_and_plugin_yaml_agree_on_the_version(manifest):
    assert manifest["version"] == _declared_version()


def test_the_manifest_names_the_plugin_the_same_way_plugin_yaml_does(manifest):
    # The name is the URL segment: /api/plugins/<name>/ and the filesystem directory.
    assert manifest["name"] == "hermes-compute-priority"
    assert re.search(r"^name:[ \t]*hermes-compute-priority[ \t]*$",
                     _PLUGIN_YAML_PATH.read_text(encoding="utf-8-sig"), re.MULTILINE)


def test_the_api_file_is_a_relative_path_inside_dashboard(manifest):
    # Mirrors _safe_plugin_api_relpath: an absolute path or a '..' climb is refused at discovery
    # time and the backend routes are silently not mounted.
    api = manifest["api"]
    assert isinstance(api, str) and api and not pathlib.PureWindowsPath(api).is_absolute()
    assert not pathlib.PurePosixPath(api).is_absolute()
    resolved = (_MANIFEST_PATH.parent / api).resolve()
    assert resolved.is_relative_to((_MANIFEST_PATH.parent).resolve())
    assert resolved.is_file(), f"manifest declares api={api!r} but that file does not exist"


def test_the_manifest_does_not_advertise_a_bundle_it_does_not_ship(manifest):
    """A plugin with no frontend must declare a hidden tab.

    Hermes injects a ``<script src>`` for the declared ``entry`` of *every* manifest, hidden or
    not (``web/src/plugins/usePlugins.ts``), so a missing bundle costs a 404 and a console
    warning. What it must not cost is a sidebar tab: an entry in the nav that opens a page saying
    "the plugin failed to load" is the worse failure. So: ship the bundle, or hide the tab.
    """
    entry = manifest.get("entry", "dist/index.js")
    bundle = _MANIFEST_PATH.parent / entry
    if not bundle.is_file():
        assert manifest.get("tab", {}).get("hidden") is True, (
            f"entry={entry!r} does not exist, so tab.hidden must be true — otherwise the dashboard "
            "adds a nav entry for a bundle that cannot load"
        )


def test_the_backend_module_exposes_the_router_that_hermes_mounts(manifest, monkeypatch):
    """Hermes skips a plugin whose api module has no ``router`` attribute."""
    # Import runs the module body, which starts the scan thread unless disabled: keep this test
    # from touching any process's niceness.
    monkeypatch.setenv("HERMES_COMPUTE_PRIORITY", "0")
    api_path = (_MANIFEST_PATH.parent / manifest["api"]).resolve()
    spec = importlib.util.spec_from_file_location("hermes_compute_priority_api_under_test", api_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "router"), "plugin_api.py must define 'router'"

    try:
        import fastapi
    except ImportError:
        # router is None where FastAPI is absent (the module builds it lazily). CI installs
        # fastapi, so the real assertion below runs there; a workstation without it skips.
        pytest.skip("fastapi is not installed here; router is None by design without it")
    from fastapi import APIRouter

    assert module.router is not None, "with fastapi installed, plugin_api.py must build a router"
    assert isinstance(module.router, APIRouter)
    # APIRoute keeps the decorator's path verbatim, leading slash included: starlette's
    # Route.__init__ asserts path.startswith("/") and assigns self.path = path.
    assert "/status" in {getattr(r, "path", "") for r in module.router.routes}
