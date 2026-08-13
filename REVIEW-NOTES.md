# Local Corvus — Review Notes

A cumulative pattern library. Every entry comes from a real incident on this
project — a debugging session, a PR review, or a retrospective — and is written
to be recognizable the *next* time the same shape of problem appears, not just
to record what happened once.

Entries are append-only and dated. The **Pattern** line is the reusable part;
everything above it is the evidence that earned it.

Open items that came out of a session but were deliberately not acted on are
collected in [Deferred / outside work](#deferred--outside-work) at the bottom.

---

## Debugger session — 2026-08-13

### Interpreter resolution: `pytest` vs. `python -m pytest`

**Symptom.** Pre-flighting experiment 01 per `docs/harness_usage.md`. Docker
came up clean; the host test run died during collection:

```
tests\test_memory\test_store.py:24: in <module>
    import memory.store as store_module
backend\memory\store.py:17: in <module>
    import aiosqlite
E   ModuleNotFoundError: No module named 'aiosqlite'
```

`aiosqlite==0.20.0` is pinned in `backend/requirements.txt` and `pip list`
confirmed it installed. So the package was present and the error was still
truthful — which means the premise "the package is missing" was wrong, not the
error.

**Root cause.** Two independent facts collided:

1. This project's `pytest.exe` is not on `PATH`. Backend dependencies were
   installed with `pip install --user`, which puts packages in
   `AppData\Roaming\Python\Python39\site-packages` (importable — hence
   `python -m pytest` works) but puts console scripts in
   `AppData\Roaming\Python\Python39\Scripts`, which is not a `PATH` entry.
2. An unrelated project's virtualenv — `Projects\Demonstration\shakespeare_poet\venv\Scripts`
   — sits as the **first** entry in the persisted *user* `PATH`. Not an
   activated venv (`VIRTUAL_ENV` was empty); permanently installed there.

Windows resolved bare `pytest` by walking `PATH`, found no `pytest.exe` in any
Corvus-relevant directory, and handed over the Shakespeare project's runner:

| Command | Resolves to | Python | Has `aiosqlite`? |
|---|---|---|---|
| `python` | `C:\tools\miniconda3\python.exe` | 3.9.12 | ✅ |
| `pytest` | `...\shakespeare_poet\venv\Scripts\pytest.exe` | 3.11.9 | ❌ |

That runner imported Corvus's `backend/memory/store.py` under 3.11.9 and
reported, accurately, that *its* environment had no `aiosqlite`.

**The tell was in the header, not the traceback.** pytest prints its
interpreter on line one of every run:

```
platform win32 -- Python 3.11.9, pytest-9.0.2, pluggy-1.6.0
```

The project runs on 3.9.12. That single line identified the problem before any
of the traceback mattered.

**Fix.** `python -m pytest`. Verified: 1093 passed in 18.8s.

**Pattern — a `ModuleNotFoundError` for a package you know is installed is an
interpreter question, not a dependency question.** The useful question is never
"is it installed?" but "installed *where*, and which interpreter is asking?"
Reach for the runner's own version banner before reading the stack trace.

**Prevention — always `python -m <tool>`, never bare `<tool>`.** `python -m pytest`
resolves the tool *through the interpreter you just named*, so the runner and
the code under test cannot disagree by construction. Bare `pytest` resolves
through `PATH`, which is global, ordered, and shared with every other project on
the machine. On a box with four Python installations only the first form is
predictable. The same applies to `python -m pip` — a bare `pip` can install into
an entirely different environment than the one your code imports from, which
produces this exact bug one step earlier and even more confusingly.

**Blast radius beyond pytest.** That same stale venv also exports `fastapi.exe`,
`uvicorn.exe`, and `httpx.exe`. Any of those invoked bare from any directory on
this machine will run out of the Shakespeare environment. Worth knowing before
the next "but it works in Docker" investigation.

**Trigger — when to expect this again.** Any machine with more than one Python;
any `ModuleNotFoundError` that `pip list` contradicts; any "works in the
container, fails on the host" split. Note that Docker masked this entirely —
the container has its own interpreter and its own `requirements.txt` install, so
the stack came up healthy while the host suite could not collect. A green
container is not evidence about the host.

**Doc follow-through.** `docs/harness_usage.md` recommended bare `pytest` in two
places — the Quick Start block and the §2 prerequisites checklist — which is
what routed into the failure. Both now read `python -m pytest`.

---

## Deferred / outside work

Items identified during a session that were consciously *not* fixed, with the
reason. Revisit when the cost of the workaround exceeds the cost of the fix.

### Stale `shakespeare_poet` venv on the persisted user `PATH`

**Identified:** 2026-08-13 (debugger session above)
**Status:** Deferred by operator decision — not blocking Corvus work
**Scope:** System-level; affects another project

`C:\Users\PaulA\Projects\Demonstration\shakespeare_poet\venv\Scripts` is the
first entry in the persisted **user** `PATH` environment variable. A virtualenv's
`Scripts` directory on the permanent `PATH` is generally a mistake: venvs are
designed to be activated per-session precisely so their console scripts *don't*
leak into unrelated shells. While it stays there, every bare console-script
invocation on this machine — `pytest`, `fastapi`, `uvicorn`, `httpx` — silently
prefers that environment.

Removing it would eliminate this class of failure permanently, but it is a
system change that reaches into a different project, so it was left alone.

**Workaround in force:** `python -m <tool>` everywhere, which is the better habit
regardless and is what the docs now specify. The deferral costs nothing as long
as that habit holds.

**If revisited:** removing the entry may break whatever workflow originally added
it — check how `shakespeare_poet` is normally launched before editing the
variable, and prefer activating its venv per-session over restoring the global
entry.
