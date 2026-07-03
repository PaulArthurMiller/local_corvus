"""Experiment harness package (M19 Phase 0) — run plan schema, turn resolution,
manifest/checkpoint persistence, notification stub, and backend client.

Standalone host-side package: imported by scripts/run_experiment_harness.py.
Does not import the backend/ package — mirrors the existing convention of
scripts/generate_seed_v2.py and scripts/verify_seed_v2.py, which talk to the
Dockerized backend over HTTP rather than importing its internals.
"""
