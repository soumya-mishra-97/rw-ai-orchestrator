# Eval reports

Each `make eval` / `python -m evals.run_eval` invocation writes one directory here:

| File | Contents |
|---|---|
| `audit.db` | The full, hash-chained audit log of every run in the batch — the evidence every number is computed from (`sdlc verify-audit` works on it with `SDLC_DATA_DIR=<dir>`). |
| `checkpoints.db` | LangGraph checkpoints (lets you inspect any run's final state). |
| `metrics.json` | Machine-readable metrics (`evals/metrics.py::EvalMetrics`). |
| `runs.json` | One row per pipeline run: scenario, trial, status, escalations, retries, cost. |
| `report.md` | The rendered tables that get spliced into `docs/`. |

Directories are git-ignored by default; commit the one(s) you cite in the docs with `git add -f`.
