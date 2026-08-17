# Setup-and-goals doc — template

Defined in `EXPERIMENT-PLAN.md`, Plan Concept Alpha, item A.c (agreed
2026-08-16). Every experiment run from `exp01-cfgA-rep3` forward gets one of
these, written **before the run starts** — a brief, pre-registered statement
of intent so the eventual read is graded against what we said we were
looking for, not against whatever the data happened to show.

**Naming and location:** same basename as the plan file it belongs to, `.md`
instead of `.json`, living beside it in `plans/`. For
`plans/exp01-cfgA-rep3_2026-08-1X.json`, the setup doc is
`plans/exp01-cfgA-rep3_2026-08-1X.md`.

**Backfilling older runs is not required** — their intent lives in
`PROGRESS.md` and `BUILD-PLAN.md` instead.

Copy the three headings below into the new file and fill them in; delete this
top block (everything above the `---`) from the copy.

---

## What we adjusted

The knob(s) and values this run changes, stated against what baseline. Name
the specific config keys/plan fields, not just a description (e.g.
`edge_update.seed_prior_alpha`/`_beta`: 1.0/1.0 → 3.0/3.0), and name which
prior run(s) this replicates or extends.

## What we're watching for

The expected phenomena if the idea behind this run is right, **and** the
signature that would show it's wrong. Both directions matter — a setup doc
that only describes the hoped-for outcome can't be falsified by the read.

## How we'll measure

Which report sections/metrics carry the answer (name them — e.g. "§6.1
final-session cluster-slot alignment", "trained-subset final Δ"), plus the
**pre-registered decision rule**: the specific thresholds or comparisons that
decide the outcome, written now, before any data exists to bias them.
