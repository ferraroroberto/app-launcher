# CI on GitHub Actions — what it is and why we added it

**Issue:** #38 · **Date:** 2026-05-21

This document has a didactic purpose. It explains what Continuous
Integration (CI) is, what this repository now does with it, why that
matters, and — the part most people get wrong — how running checks *on
your own machine* differs from running them *on GitHub*.

## 1. What is CI?

**Continuous Integration** is the practice of automatically running your
project's checks every time code changes, on a machine that is *not the
developer's laptop*. "The checks" here means whatever proves the code
still works: it compiles, the tests pass, the app boots.

The word "continuous" is the point. Instead of validating in a big batch
before a release ("integration" used to be a dreaded multi-day event),
you validate on *every push* — small, constant, automatic. A break is
caught minutes after it is introduced, while the change is still fresh in
the author's head, instead of weeks later when nobody remembers it.

A **CI workflow** (GitHub's term; other systems call it a pipeline or a
job) is just a recipe: "when X happens, on a fresh machine, run these
steps." Ours lives in `.github/workflows/e2e.yml`. GitHub reads that file,
rents a clean virtual machine, and runs the recipe. Green check = passed,
red X = something broke.

## 2. What we are doing in this repo

This project already had a **local pre-ship gate**:
`scripts/verify-before-ship.ps1`. It runs, in one pass/fail:

1. Byte-compile every Python file (`app`, `src`, `tests`).
2. The non-browser test suite (`pytest`, ~80 tests).
3. The Playwright end-to-end suite — a real browser (Chromium *and* a
   WebKit engine projected onto an iPhone viewport) driving the webapp,
   against a disposable server the script boots itself.

That gate is the **contract**: `CLAUDE.md` says it must pass before any
change to the webapp/launcher/session-host is declared done.

Issue #38 added a CI workflow that runs *that exact same gate* on GitHub,
automatically, on:

- every **push to a non-`main` branch**, and
- every **pull request into `main`**.

Nothing about the gate itself changed. CI just runs it for you, somewhere
else, without you having to remember.

### The workflow steps (`.github/workflows/e2e.yml`)

| Step | Why it exists |
|---|---|
| `runs-on: windows-latest` | `pywinpty` is Windows-only and the e2e suite spawns real PTYs — a Linux runner physically cannot run this project. |
| Checkout + set up Python 3.12 | A fresh machine starts with *nothing* — not even the code. |
| Create `.venv`, install `requirements.txt` | `verify-before-ship.ps1` hard-requires `.venv\Scripts\python.exe`. We build the venv so the script runs unmodified — the script stays the contract. |
| `playwright install chromium webkit` | The browser engines the e2e suite drives. They are large binaries, not Python packages, so they need a separate install. |
| **Seed config files from samples** | `config/{config,webapp_config,apps}.json` are gitignored — only the `*.sample.json` templates are committed. A fresh runner has only the samples. (See §4 — this step was the bug fix.) |
| Run `verify-before-ship.ps1` | The actual gate. |

## 3. Why this is important

**Humans forget; machines do not.** The local gate is mandatory, but
"mandatory" relied entirely on the developer remembering to type the
command before pushing. One tired evening and an unverified change lands
on `main`. CI removes the human from that loop: the gate runs whether you
remember or not.

**It catches "works on my machine."** A developer's laptop accumulates
state — installed tools, leftover config files, a server already running.
Code can depend on that state by accident. A fresh CI runner has none of
it, so it surfaces those hidden dependencies. (This is not theoretical —
see §4.)

**It documents the truth.** A green check on a pull request is a shared,
visible fact: "this branch passed the gate on a clean machine." A reviewer
no longer has to take "I tested it" on faith.

**But CI is supplementary, not the contract.** The local gate stays
authoritative. CI is the safety net, not the trapeze. There is a standing
project rule (carried since issue #22): *a test that flakes on CI is worse
than no test* — a red X people learn to ignore is actively harmful. So
this workflow only "counts" after **3 consecutive green runs on a no-op
PR**, and if it flakes more than 1-in-10 in its first week, the workflow
file gets reverted. The local gate would still be there; nothing is lost.

## 4. Local vs. GitHub — the difference that actually bites

This is the didactic heart of the document.

Running `verify-before-ship.ps1` **on your machine** and running the
**same script on GitHub** are *not* the same test, even though it is
byte-for-byte the same script. The difference is the **environment**.

Your machine is *dirty* in useful and misleading ways:

- It has a real `config/webapp_config.json`, `config.json`, `apps.json` —
  you created them during setup. They are **gitignored**, so they exist
  only on your disk and never travel with the code.
- `claude` is on your `PATH`.
- A tray, a session-host, certificates may already be running or present.

A GitHub runner is *pristine*: a brand-new Windows VM with only what the
workflow explicitly installs. Anything your code silently assumed to be
"just there" is suddenly **not there**.

### The bug this surfaced immediately

The very first CI run was **green** — and the green was a lie. The gate's
e2e step reported:

```
4 passed, 50 skipped in 3.78s
```

On a developer machine that same step runs ~40-60s and passes dozens of
tests. The runner had no `config/webapp_config.json` (gitignored — only
the sample is committed). The e2e `webapp_config` fixture *skips* when
that file is missing; that skip cascades through the `auth_token` fixture
and silently skips every test that needs an authenticated page — 50 of
them. The suite exited 0 because **a skip is not a failure**.

This is the single most important lesson about CI: **a green check that
skipped everything is more dangerous than a red one.** A red X gets
investigated; a hollow green gets trusted. It is the same trap the local
gate was built to avoid — "a forgotten tray looks like a green run."

The fix was one workflow step: seed the three config files from their
committed `*.sample.json` templates before running the gate (loopback
access bypasses the bearer-token middleware, so the sample tokens are
sufficient — see `tests/e2e/conftest.py`). After the fix:

```
30 passed, 24 skipped in 43.35s
```

The suite genuinely runs now. The 24 remaining skips are *expected and
documented*: the terminal-regression tests launch a real `claude` PTY, and
`claude` is not on a GitHub runner's `PATH`, so the `launched_pty_session`
fixture skips them cleanly. That is an honest skip — we know exactly why,
and it is written down — not a silent collapse.

### The takeaway

| | Your machine | GitHub runner |
|---|---|---|
| Gitignored config files | Present (you made them) | **Absent** — seed from samples |
| `claude` on PATH | Yes | No — terminal tests skip (expected) |
| Pre-existing tray / session-host | Maybe | Never — gate boots its own |
| Good for | Fast dev loop | Proving it works from *nothing* |

Use the **local gate** for the fast inner loop while developing. Trust
**CI** as the impartial second opinion that has none of your machine's
conveniences — which is exactly why it is worth having.

## Files modified

- `.github/workflows/e2e.yml` (new) — the CI workflow.
- `README.md` — note on the CI gate and the expected `claude`-not-on-PATH
  test skip.
- `docs/ci-github-actions.md` (new) — this document.

## Validation run

- First CI run on `feat/38-ci-e2e-workflow`: green but hollow —
  `4 passed, 50 skipped` in the e2e step (config files missing).
- After the seed-from-samples fix: green and real — non-e2e `80 passed`,
  e2e `30 passed, 24 skipped`, gate total 54s (under the 90s budget).
- Both the `push` and `pull_request` triggers fired and passed.
