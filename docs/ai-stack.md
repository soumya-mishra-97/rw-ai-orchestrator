# AI stack: what was chosen and why

The principle behind every choice: **use the least machinery that makes reliability measurable.** Each row below names the alternative that was rejected and the reason.

## Orchestration: LangGraph 1.2 (state graph + `interrupt()` + SQLite checkpointer)

The pipeline has fixed stages, conditional edges on a verdict, and a pause-for-a-human edge. That is a state machine, and LangGraph models it directly:

- `add_conditional_edges` expresses the verdict routing.
- `interrupt()` with `Command(resume=…)` pauses and resumes the run.
- The checkpointer makes a paused run survive a process restart. This is tested in `test_run_survives_process_restart`.

| Alternative | Why not |
|---|---|
| CrewAI / role-play multi-agent frameworks | These optimise for agents negotiating among themselves. This pipeline needs a *deterministic* control flow, where the reliability rules live in code rather than in an agent's judgement. |
| Claude Agent SDK / an agentic tool loop per persona | Each persona is a single structured transformation (artifact in, artifact out) with no tools to call. An open-ended agent loop per stage would add tokens, latency and variance for no gain. |
| Hand-rolled loop | Feasible, but I would be re-implementing durable pause/resume and checkpointing. LangGraph supplies exactly those and little else. |

The graph is kept thin. All decision logic lives in pure functions ([`graph/routing.py`](../src/sdlc_loop/graph/routing.py)) and agents sit behind a `Protocol`, so replacing LangGraph would mean rewriting one ~120-line builder, not the system.

## Models: Claude Haiku 4.5 (Tier 1) and Claude Sonnet 5 (Tier 2), routed asymmetrically

| Persona | Default | Tier 2 when | Reasoning |
|---|---|---|---|
| PM, BA, QA | Haiku 4.5 | Final retry, or after a critical flag | These are extraction and structuring tasks with a written rubric. Haiku handles them at half Sonnet's price, and the retry ladder catches its misses. |
| **Architect** | **Sonnet 5, always** | — | A design error is the most expensive error in the pipeline: it propagates into code and tests, and legacy-constraint violations originate here. Saving a few cents on this call is the wrong trade. |
| **Dev** | **Sonnet 5, always** | — | Same reasoning, and generated code is actually parsed and optionally executed downstream. |
| Evaluator | Haiku 4.5 | Borderline score (3.0–3.99) or a pending human escalation | Most artifacts are clearly good or clearly bad, and a cheap judge settles those. Sonnet is spent only where the verdict is uncertain or a human's time is at stake. |

This asymmetry is a judgement call, and it can be tested: `SDLC_ROUTING_POLICY=all_tier2` runs the "everything on Sonnet" baseline, and every call also logs its re-priced Tier 2 cost.

**Model features used:**

- Structured outputs (`output_config.format` with a JSON schema generated from the Pydantic models).
- Adaptive thinking on Sonnet 5, which is its default, with `effort=medium` to cap thinking spend.
- Prompt caching with explicit breakpoints.
- The Message Batches API for evals.

Sampling parameters are not sent, because SDK 1.x and current models reject them.

**Why not Opus for the Architect?** Sonnet 5 at $2/$10 per million tokens against Opus 5 at $5/$25. The eval measures whether Sonnet's Architect output passes the gate on the first attempt. If it did not, upgrading the model would be a one-line change in `SDLC_TIER2_MODEL`, backed by numbers.

## Validation: Pydantic v2 at every boundary

Every handoff is a closed (`extra="forbid"`), frozen Pydantic model. The same classes do three jobs:

- They generate the structured-output schema sent to the API.
- They validate the response client-side, including constraints the API cannot enforce, such as unique story ids.
- They type the LangGraph state.

The checkpointer's deserialiser has an explicit allowlist of exactly these classes.

## Persistence: SQLite (checkpoints + audit log). No vector store.

**No retrieval happens anywhere in this track.** Every persona receives a specific, small upstream artifact. Adding pgvector, Pinecone or embeddings here would be the "trendy default" the brief warns against: more infrastructure and more cost for zero quality gain.

SQLite serves this single-node exercise, both through `langgraph-checkpoint-sqlite` and through a plain append-only `audit_log` table. Production would use `PostgresSaver`, which has the same interface, and a Postgres or warehouse audit sink.

## Interfaces: FastAPI + Typer + a no-build web UI

- The browser UI is plain HTML, CSS and vanilla JavaScript served by FastAPI from the same origin. A React or Vite toolchain would add a second language ecosystem, a build step and CORS configuration to a Python take-home, and the UI needs none of it: one form, a polling loop and structured rendering. Model output is rendered with `textContent` only, which rules out script injection from generated artifacts.

- The CLI (`sdlc run --scenario 1`) runs end to end with no server, and prompts the human inline on escalation.
- The API exposes `POST /runs`, `GET /runs/{id}`, `GET /runs/{id}/audit` and `POST /runs/{id}/resume`, with role-based API keys.
- Runs execute in background tasks because a full pipeline takes minutes.

## Evaluation and observability: a custom harness over the audit log

| Alternative | Why not (for this track) |
|---|---|
| Ragas | Built for RAG metrics (faithfulness, context precision). There is no retrieval here. |
| promptfoo | Good for prompt-level A/B tests. The questions here are about the *pipeline*: first-attempt pass rate per handoff, escalation rate, and judge agreement with humans. |
| LangSmith / hosted tracing | Useful in production. For the exercise, the hash-chained audit log already records every call with tokens, cost, latency, model and verdict, and it doubles as the eval dataset, so there is no second system to keep consistent. |

The harness (`evals/`) is about 600 lines:

- Scenario × trial runs in live, batch or offline mode.
- Metrics computed purely from audit rows.
- A labeled fault-injection benchmark for the judge.
- The spec's anti-pattern check.
- A blind export/score tool for human agreement, reporting raw agreement and Cohen's kappa.

## PII: regex redactor by default, Presidio optional

The golden scenarios are synthetic and contain no PII; a test asserts zero false positives on them. The dependency-free regex redactor covers structured identifiers: email, Luhn-valid card numbers, SSN, IBAN, phone and IP. Presidio (`uv sync --extra pii`, `SDLC_PII_BACKEND=presidio`) adds NER for names and addresses, which is what production should run. It is optional because it pulls in spaCy models that are heavy for a take-home.

## Tooling

- `uv` with a lockfile, for reproducible installs.
- `ruff` for lint and format.
- `mypy --strict`, clean across 61 files.
- `pytest`: 122 offline, deterministic tests (96% line coverage).
- `pre-commit`, including hooks that block `.env` files and private keys.
