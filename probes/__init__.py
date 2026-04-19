# probes/
# ---------------------------------------------------------------------------
# One script per external data source. Each probe:
#   - hits its endpoint with a minimal known-good request
#   - asserts the response shape matches the pinned snapshot
#   - writes the latest response to tests/probe_snapshots/
#
# Shared types in base.py. Run probes/run_all.py to execute every probe
# and print a summary.
# ---------------------------------------------------------------------------
