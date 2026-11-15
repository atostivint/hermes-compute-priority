# hermes-compute-priority

A Hermes plugin that keeps the process serving your connection ahead of the processes running your
agents.

## The problem it solves

Hermes can run agent turns in a child process (`dashboard.turn_isolation`). That child is where the
GIL is held while a turn parses a model stream or dispatches tools. The process that answers your
Desktop's WebSocket is its **parent**.

On a small machine, the parent must win — and configuration alone cannot make it:

| Knob | What it arbitrates | Why it is not enough |
|---|---|---|
| `CPUWeight` (systemd) | between **units** | the child lives in the parent's cgroup, so it gets the same share |
| `Nice` (systemd) | between **processes** | niceness is **inherited** at fork/exec: the child starts at the parent's nice and stays there |

Measured on a 1 vCPU host, five dashboard processes across two generations: all at `-5`. The parent
had no priority over its own worker.

So the worker's niceness has to be raised **after** it starts. That needs no privilege — lowering a
process's priority of a process you own is always allowed — and it can never make anything more
urgent than it already was.

## What it does

Every two seconds, in the Hermes process that loaded it:

1. scans `/proc` for processes owned by the same user whose command line contains
   `tui_gateway.compute_host` (how `tui_gateway/host_supervisor.py` spawns a turn worker);
2. raises their niceness to `+10`, skipping any worker already at or below that value;
3. logs one line per worker, once.

It does nothing else. On a host where turn isolation is off, no worker of that shape exists, so the
plugin is a no-op.

## What it does not do

- It does **not** speed up turns: work handed to a worker still costs the same CPU, it just yields
  first.
- It does **not** help sessions whose agent was built in-process (they never use a worker, so there
  is no separate process to deprioritise).
- It is **not** a resource limit. If you want a hard ceiling on a whole process tree, use cgroup
  `CPUQuota` in the unit — this plugin is about *ordering*, not *capping*.

## Install

```bash
hermes plugins install https://github.com/atostivint/hermes-compute-priority
```

Or copy the directory into `<HERMES_HOME>/plugins/hermes-compute-priority/` and restart the gateway.

Linux only: the scan reads `/proc` and `os.setpriority`. Elsewhere the plugin loads and does
nothing.

## Settings

Environment, read once when the backend is imported:

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_COMPUTE_PRIORITY` | `1` | `0` disables the scan entirely |
| `HERMES_COMPUTE_PRIORITY_NICE` | `10` | target niceness for workers (0–19; `19` is the lowest) |
| `HERMES_COMPUTE_PRIORITY_INTERVAL_S` | `2.0` | seconds between scans |

## Verify it works

```bash
# before a turn that uses an isolated session
ps -o pid,ppid,ni,args -C python3 | grep compute_host
```

The worker should appear at niceness `10` within a couple of seconds of starting, while the
serving process keeps its own value. The plugin also answers a read-only route:

```bash
curl http://127.0.0.1:<dashboard port>/api/plugins/hermes-compute-priority/status
```

which reports the target, the scan count, the workers reniced so far, and the last error if any.

## Disclosure

Written the way the Hermes plugin catalog expects:

- **Network:** none. The plugin opens no socket and contacts nothing.
- **Files:** reads `/proc/<pid>/cmdline` and `/proc/<pid>/` ownership for processes of the same
  user. Reads nothing in your home directory and writes no file.
- **Processes:** calls `os.setpriority` on processes it has already matched **and** that belong to
  the same user, only ever raising their niceness. It never signals, stops or kills anything, and
  it skips its own pid.
- **Threads:** one daemon thread inside the Hermes process that loaded it, started at import and
  stopped when that process exits.
- **Credentials:** none read, none stored.
- **Telemetry:** none.

## Limitations, stated plainly

- **Polling, not hooking.** The delay between a worker starting and being reniced is up to one
  interval. Hooking the spawn would be exact, but that is core code: a plugin may only use the
  published plugin API.
- **Detection by command line.** If upstream renames the module, the match breaks — loudly in the
  status route (`applied_total` stops moving), silently otherwise.
- **One process.** The plugin acts inside the process that loaded it. If your dashboard and gateway
  are separate units, install it on the one that spawns the workers.

## The better fix, upstream

`subprocess.Popen(..., preexec_fn=...)` or a `nice=` argument in
`tui_gateway/host_supervisor.py` would make this exact, in one line, without a polling thread.
This plugin exists because that line is not there yet.

## License

MIT — see [LICENSE](LICENSE).
