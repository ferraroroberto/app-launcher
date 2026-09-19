# CI on GitHub Actions — what it is and why we added it

This document has a didactic purpose. It explains what Continuous Integration (CI) is, what this repository now does with it, why that matters, and — the part most people get wrong — how running checks *on your own machine* differs from running them *on GitHub*.

## 1. What is CI?

**Continuous Integration** is the practice of automatically running your project's checks every time code changes, on a machine that is *not the developer's laptop*. "The checks" here means whatever proves the code still works: it compiles, the tests pass, the app boots.

The word "continuous" is the point. Instead of validating in a big batch before a release ("integration" used to be a dreaded multi-day event), you validate on *every push* — small, constant, automatic. A break is caught minutes after it is introduced, while the change is still fresh in the author's head, instead of weeks later when nobody remembers it.

A **CI workflow** (GitHub's term; other systems call it a pipeline or a job) is just a recipe: "when X happens, on a fresh machine, run these steps." Ours lives in `.github/workflows/e2e.yml`. GitHub reads that file, rents a clean virtual machine, and runs the recipe. Green check = passed, red X = something broke.

## 2. What we are doing in this repo

This project already had a **local pre-ship gate**: `scripts/verify-before-ship.ps1`. It runs, in one pass/fail:

1. Byte-compile every Python file (`app`, `src`, `tests`).
2. The non-browser test suite (`pytest`, ~1,800 tests).
3. The Playwright end-to-end suite — a real browser (Chromium *and* a WebKit engine projected onto an iPhone viewport) driving the webapp, against a disposable server the script boots itself.

That gate is the **contract**: `CLAUDE.md` says it must pass before any change to the webapp/launcher/session-host is declared done.

Issue #38 added a CI workflow that runs *that same gate* on GitHub, automatically, on:

- every **push to `main`**, and
- every **pull request into `main`**.

Nothing about the gate script itself changed. CI just runs it for you, somewhere else, without you having to remember.

### What CI runs, and what it deliberately does not (#1041)

CI runs steps 1 and 2 above and stops. It does **not** run step 3, the Playwright browser suite. That is a decision, not an accident: `scripts/e2e-gate-route.ps1` routes any run that sets `CI=true` to tier `skip`, and `tests/test_verify_gate_route.py` pins it.

The reasoning is the whole of §4 below, read backwards. CI's unique value is the *clean machine* — a fresh `.venv`, committed files only, at the commit rather than somebody's working tree. That value is fully delivered by the install and the non-browser suite, in the first few minutes. The browser leg, by contrast, was a **second** run of a suite that had already passed locally on the same script before the PR was opened, and it needed three separate timeout widenings (`E2E_LOG_POLL_DEADLINE_MS`, `E2E_STOP_OVERLAY_HIDE_MS`, `E2E_REAL_AGENT_ECHO_MS`) purely to survive the hosted runner. A test that needs its clock loosened to survive the environment is testing the environment.

**What this costs, plainly.** Every browser-visible regression is now proven only by the local gate. If a lane forgets it, or routes it narrow and is wrong, nothing downstream catches it. Two smaller losses go with it: the e2e suite's own health on a clean machine (the §4 bug below — a hollow green from mass-skipping — would not be caught a second time by CI), and Playwright browser-install drift.

**What it keeps, and what that is worth.** #1041 is the argument for the half that stayed. Two guard-coverage tests failed on CI and passed locally for two days, because `fastapi>=0.110` is a lower bound: this box had resolved it to 0.136 and the runner installed 0.141, and 0.141 changed how `include_router` exposes routes. No browser was involved. Only a machine that installs the dependencies from scratch could have surfaced it.

### The workflow steps (`.github/workflows/e2e.yml`)

| Step | Why it exists |
|---|---|
| `runs-on: windows-2025` | `pywinpty` is Windows-only and the suite includes real-ConPTY tests — a Linux runner physically cannot run this project. |
| Checkout + set up Python 3.12 | A fresh machine starts with *nothing* — not even the code. |
| Create `.venv`, install `requirements.txt` | `verify-before-ship.ps1` hard-requires the venv interpreter. We build the venv so the script runs unmodified — the script stays the contract. **The install is itself part of what this job checks.** |
| **Seed config files from samples** | `config/{config,webapp_config,apps}.json` are gitignored — only the `*.sample.json` templates are committed. A fresh runner has only the samples. (See §4 — this step was the bug fix.) |
| Run `verify-before-ship.ps1` | The actual gate — byte-compile + the non-browser suite; the browser phase routes to `skip` on CI (see above). |

## 3. Why this is important

**Humans forget; machines do not.** The local gate is mandatory, but "mandatory" relied entirely on the developer remembering to type the command before pushing. One tired evening and an unverified change lands on `main`. CI removes the human from that loop: the gate runs whether you remember or not.

**It catches "works on my machine."** A developer's laptop accumulates state — installed tools, leftover config files, a server already running. Code can depend on that state by accident. A fresh CI runner has none of it, so it surfaces those hidden dependencies. (This is not theoretical — see §4.)

**It documents the truth.** A green check on a pull request is a shared, visible fact: "this branch passed the gate on a clean machine." A reviewer no longer has to take "I tested it" on faith.

**But CI is supplementary, not the contract.** The local gate stays authoritative. CI is the safety net, not the trapeze. There is a standing project rule (carried since issue #22): *a test that flakes on CI is worse than no test* — a red X people learn to ignore is actively harmful. This workflow has been established for months now; `CLAUDE.md`'s "CI expectations" block owns the current scope and advisory-not-required status; the named flaky legs moved with the browser suite to its "E2E browser suite — local gate only" block.

## 4. Local vs. GitHub — the difference that actually bites

This is the didactic heart of the document.

Running `verify-before-ship.ps1` **on your machine** and running the **same script on GitHub** are *not* the same test, even though it is byte-for-byte the same script. The difference is the **environment**.

Your machine is *dirty* in useful and misleading ways:

- It has a real `config/webapp_config.json`, `config.json`, `apps.json` — you created them during setup. They are **gitignored**, so they exist only on your disk and never travel with the code.
- `claude` is on your `PATH`.
- A tray, a session-host, certificates may already be running or present.

A GitHub runner is *pristine*: a brand-new Windows VM with only what the workflow explicitly installs. Anything your code silently assumed to be "just there" is suddenly **not there**.

### The bug this surfaced immediately

The very first CI run was **green** — and the green was a lie. The gate's e2e step reported:

```
4 passed, 50 skipped in 3.78s
```

On a developer machine that same step runs ~40-60s and passes dozens of tests. The runner had no `config/webapp_config.json` (gitignored — only the sample is committed). The e2e `webapp_config` fixture *skips* when that file is missing; that skip cascades through the `auth_token` fixture and silently skips every test that needs an authenticated page — 50 of them. The suite exited 0 because **a skip is not a failure**.

This is the single most important lesson about CI: **a green check that skipped everything is more dangerous than a red one.** A red X gets investigated; a hollow green gets trusted. It is the same trap the local gate was built to avoid — "a forgotten tray looks like a green run."

The fix was one workflow step: seed the three config files from their committed `*.sample.json` templates before running the gate (loopback access bypasses the bearer-token middleware, so the sample tokens are sufficient — see `tests/e2e/conftest.py`). After the fix:

The suite genuinely ran. Counts and timings are deliberately not quoted here — they move with every closed bite that adds a regression pin, and a frozen number in a second document is how this section went stale in the first place (#1008). **`CLAUDE.md`'s CI block is the canonical home** for what a typical green looks like and when to investigate.

What matters is *which* skips are honest ones. Since #534 the terminal-regression tests no longer need the real agent: `launched_pty_session` spawns a deterministic lightweight stub that needs only Python. The one fixture that still skips on a bare machine is **`launched_claude_pty_session`** — used only by the handful of tests whose assertions depend on the real Claude CLI's own rendering and lifecycle — because `claude` is not on a fresh runner's `PATH`. That is an honest skip: we know exactly which tests it covers and why, and it is written down. A skip whose cause you cannot name is the failure mode this whole section is about.

Since #1041 those fixtures are local-gate concerns — CI runs no browser suite, so it can no longer hollow out in this particular way. Read the story as the lesson it is, not as a description of what CI does today: **the shape of the bug outlived the workflow step that caused it.** A check that cannot establish its fact must say so, instead of folding the unknown into the passing state. That is exactly what #1041 itself was — two guard-coverage tests that could not see the route table, one of which passed anyway because there was nothing left to check.

### The takeaway

| | Your machine | GitHub runner |
|---|---|---|
| Gitignored config files | Present (you made them) | **Absent** — seeded from samples by the workflow |
| `claude` on PATH | Yes | No — matters only to the browser suite, which CI no longer runs (#1041) |
| Pre-existing tray / session-host | Maybe | Never — and since #1041 CI boots neither |
| Browser suite | **Runs — and is the contract** | Not run (#1041) |
| Dependency versions | Whatever you installed, whenever | Resolved fresh from `requirements.txt` every run |
| Good for | Fast dev loop | Proving it works from *nothing* |

Use the **local gate** for the fast inner loop while developing, and as the contract for anything a browser can see. Trust **CI** for the one thing it alone can say: that this works starting from nothing.
