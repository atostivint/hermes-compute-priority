"""hermes-compute-priority backend, mounted at ``/api/plugins/hermes-compute-priority/``.

The plugin runs inside the Hermes process that serves the connection (the dashboard loader
imports this file as a top-level module). It starts one daemon thread that, every couple of
seconds, looks for the turn workers spawned by ``tui_gateway.host_supervisor`` and raises their
niceness, so the process answering the Desktop stays ahead of them.

Why a thread and not a hook: the supervisor's spawn call is core code, and a plugin may only use
the published plugin API. Finding our own user's workers needs nothing else, and raising a
process's niceness never requires a privilege.

Which platform does what is decided by ``platform_backend``, not here: Linux reads ``/proc`` and
calls ``os.setpriority``; macOS and Windows enumerate with ``psutil`` and lower the priority
through ``psutil.Process.nice()`` (a priority *class* on Windows). Without ``psutil`` on those two
platforms the plugin stays loaded, does nothing, and ``/status`` reports
``{"enabled": false, "reason": "psutil is not installed …"}`` instead of claiming to work.

Everything fails open. An unreadable pid, a worker that exits mid-scan, a refused priority change
only means that worker is skipped; the route and the thread keep running. The plugin does nothing
at all when turn isolation is off, because then no worker of that shape exists.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import threading
import time

# The dashboard loader imports this file as a top-level module, so a relative import of the
# plugin's core would fail. Put the plugin root on the path instead.
_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

import compute_priority as core  # noqa: E402  (after the path fix above)
import platform_backend as platform_layer  # noqa: E402

_log = logging.getLogger("hermes_compute_priority")

_DEFAULT_ENABLED = True
_RECENT_LIMIT = 20


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class WorkerNanny:
    """The loop. Named so the thread shows up in a stack dump as something recognisable."""

    def __init__(
        self,
        *,
        target_nice: int,
        interval_s: float,
        rules=None,
        backend: platform_layer.Backend | None = None,
    ) -> None:
        self.target_nice = target_nice
        self.interval_s = interval_s
        self.rules = list(rules or [])
        self.backend = backend if backend is not None else platform_layer.active()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._recent: list[dict[str, object]] = []
        self._applied_total = 0
        self._scans = 0
        self._last_error: str | None = None

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="compute-priority", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception as exc:  # the loop must survive anything: it watches, it never owns
                self._last_error = f"{type(exc).__name__}: {exc}"
                _log.warning("compute-priority scan failed: %s", self._last_error)

    # -- one pass --------------------------------------------------------------------------
    def tick(self) -> list[dict[str, object]]:
        applied = self.backend.apply(
            target_nice=self.target_nice,
            rules=self.rules or None,
            pid_exclude=os.getpid(),
        )
        with self._lock:
            self._scans += 1
            if applied:
                self._last_error = None
                for item in applied:
                    self._applied_total += 1
                    self._recent.append(
                        {"pid": item.pid, "from": item.previous, "to": item.current,
                         "at": time.time()}
                    )
                    _log.info("compute-priority: pid %d reniced %d -> %d",
                              item.pid, item.previous, item.current)
                del self._recent[:-_RECENT_LIMIT]
        return [{"pid": i.pid, "from": i.previous, "to": i.current} for i in applied]

    # -- reporting -------------------------------------------------------------------------
    def snapshot(self) -> dict[str, object]:
        with self._lock:
            payload: dict[str, object] = {
                "ok": True,
                "enabled": True,
                "target_nice": self.target_nice,
                "interval_s": self.interval_s,
                "scans": self._scans,
                "applied_total": self._applied_total,
                "recent": list(self._recent),
                "last_error": self._last_error,
                "rules": [{"name": r.name, "match": r.match, "nice": r.nice} for r in self.rules],
            }
        payload.update(self.backend.status())
        if not payload.get("reason"):
            payload.pop("reason", None)  # nothing to explain: do not ship a bare null
        # A target this platform cannot express (niceness < 5 on Windows) is worth saying out
        # loud: the loop would run and change nothing.
        problem = self.backend.target_problem(self.target_nice)
        if problem:
            payload["note"] = problem
        return payload


_ENABLED = _env_bool("HERMES_COMPUTE_PRIORITY", _DEFAULT_ENABLED)

# Opt-in extra classes of process. Empty unless the operator configures them: the shipped
# behaviour is exactly one class (the turn worker), because a published plugin must not
# deprioritise parts of someone else's Hermes without being asked.
_RULES = core.parse_rules(os.environ.get("HERMES_COMPUTE_PRIORITY_RULES"))

# Which mechanism this platform offers. Linux is the original /proc + os.setpriority path; macOS
# and Windows need psutil, and report why when it is missing. No `hasattr(os, "getpriority")`
# guess: `os.getpriority` exists on macOS too, but a scan there cannot read /proc.
_BACKEND = platform_layer.active()

_NANNY: WorkerNanny | None = None
_DISABLED_REASON: str | None = None

if not _ENABLED:
    _DISABLED_REASON = "disabled by HERMES_COMPUTE_PRIORITY"
elif not _BACKEND.available:
    _DISABLED_REASON = _BACKEND.reason or f"no usable backend on {_BACKEND.platform}"
else:
    _NANNY = WorkerNanny(
        target_nice=_env_int("HERMES_COMPUTE_PRIORITY_NICE", core.DEFAULT_TARGET_NICE),
        interval_s=_env_float("HERMES_COMPUTE_PRIORITY_INTERVAL_S", core.DEFAULT_INTERVAL_S),
        rules=_RULES,
        backend=_BACKEND,
    )
    _NANNY.start()


def status_payload() -> dict[str, object]:
    """The body of ``/status``. Module-level so it can be tested without FastAPI installed."""
    if _NANNY is None:
        # Backend facts first: `status()` carries its own `reason` (None when it is available), and
        # the route must still say *why* nothing is running.
        payload: dict[str, object] = {"ok": True, "enabled": False}
        payload.update(_BACKEND.status())
        payload["reason"] = _DISABLED_REASON or _BACKEND.reason or "disabled"
        return payload
    return _NANNY.snapshot()


def _router():
    """The read-only status route, or ``None`` where FastAPI is unavailable.

    Built lazily so that importing this module stays cheap for callers that only want the loop.
    """
    try:
        from fastapi import APIRouter
    except Exception:
        return None

    router = APIRouter()

    @router.get("/status")
    def status() -> dict[str, object]:
        return status_payload()

    return router


router = _router()
