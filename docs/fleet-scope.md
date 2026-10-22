# Fleet scope

Settings › Connections › **Fleet scope** lists every repo the fleet knows about, each with a switch. On means the repo is **active**: fleet-wide runs (audits, cleanups, design sweeps, propagation) include it. Off means **dormant**: those runs skip it. The row shows the count ("40 active, 4 dormant"), and a filter appears above twelve repos.

## Where the state lives

The switch is fleet-config's `[global] architecture_ignore` array in its machine-local, gitignored `hooks/projects.toml`. A flip changes this PC only, so it needs no commit and leaves no uncommitted change.

The launcher never reads or writes that file itself. It loads fleet-config's `skills/_lib/fleet_status.py` by path (the same load the Board's claim check uses) and calls its `list_status()` and `set_status()`, so the format has one writer. The checkout is the **fleet-config folder** from Settings › Folders.

## API

- `GET /api/fleet/scope` returns `{available, reason, repos: [{name, status}]}`.
- `PUT /api/fleet/scope/{repo}` with `{status: "active"|"dormant"}` flips one repo and returns the same listing plus `outcome` (`changed` or `unchanged`).

Both sit behind the same auth as the rest of `/api`. A `PUT` answers `422` for a status that is neither value, `404` for a repo that is not listed, `503` when the contract is unavailable and `409` with the writer's own message when it refuses the edit.

## When the contract is unavailable

A fleet-config folder that is unset, missing, too old to have the module, or whose module fails to load reads `available: false` with a short reason. The sheet shows that reason and no switches. It is never an empty list and never "everything active". The load is cached and the failure is logged once per distinct reason, so a checkout that is absent does not warn on every read.
