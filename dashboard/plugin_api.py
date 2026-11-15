"""hermes-compute-priority backend, mounted at ``/api/plugins/hermes-compute-priority/``.

The plugin runs inside the Hermes process that serves the connection (the dashboard loader
imports this file as a top-level module). It starts one daemon thread that, every couple of
seconds, looks for the turn workers spawned by ``tui_gateway.host_supervisor`` and raises their
niceness, so the process answering the Desktop stays ahead of them.

Why a thread and not a hook: the supervisor's spawn call is core code, and a plugin may only use
the published plugin API. Scanning ``/proc`` for our own user's children needs nothing else, and
raising a process's niceness never requires a privilege.

Everything fails open. A missing ``/proc`` (macOS, Windows), an unreadable pid, a worker that
exits mid-scan, or a refused ``setpriority`` only means that worker is skipped; the route and the
thread keep running. The plugin does nothing at all when turn isolation is off, because then no
worker of that shape exists.
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

    def __init__(self, *, target_nice: int, interval_s: float) -> None:
        self.target_nice = target_nice
        self.interval_s = interval_s
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
        applied = core.apply_once(target_nice=self.target_nice, pid_exclude=os.getpid())
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
            return {
                "ok": True,
                "enabled": True,
                "target_nice": self.target_nice,
                "interval_s": self.interval_s,
                "scans": self._scans,
                "applied_total": self._applied_total,
                "recent": list(self._recent),
                "last_error": self._last_error,
            }


_ENABLED = _env_bool("HERMES_COMPUTE_PRIORITY", _DEFAULT_ENABLED)
_NANNY: WorkerNanny | None = None

if _ENABLED and hasattr(os, "getpriority"):
    _NANNY = WorkerNanny(
        target_nice=_env_int("HERMES_COMPUTE_PRIORITY_NICE", core.DEFAULT_TARGET_NICE),
        interval_s=_env_float("HERMES_COMPUTE_PRIORITY_INTERVAL_S", core.DEFAULT_INTERVAL_S),
    )
    _NANNY.start()


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
        if _NANNY is None:
            return {"ok": True, "enabled": False, "reason": "disabled"}
        return _NANNY.snapshot()

    return router


router = _router()
