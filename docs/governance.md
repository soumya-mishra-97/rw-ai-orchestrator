# Governance and security

This note covers what is implemented, as opposed to what is only described. Each control links to its code and test.

## Secrets

- The Anthropic key is read **only** by the Anthropic SDK from the environment (`ANTHROPIC_API_KEY`, or an `ant auth login` profile). It is never a field on our `Settings` object, so it cannot be logged, serialised into a checkpoint or returned by the API.
- `.env` is exported into the process at startup (`runtime.load_environment`), so a key kept there reaches the SDK. Real environment variables always take precedence over `.env`.
- At startup the key is verified with one free `models.retrieve` call. `SDLC_MODE=auto` falls back to demo mode on failure and shows the reason, without the key, in the UI; `SDLC_MODE=live` refuses to start. The key itself is never displayed or logged.
- `.env.example` is committed and `.env` is git-ignored. A pre-commit hook fails any commit that adds a `.env` file, and `detect-private-key` runs on every commit.
- API keys for the service (`SDLC_API_KEYS`) are compared with `hmac.compare_digest` to avoid timing leaks.
- **Production:** keys come from a secret manager (AWS Secrets Manager or Vault) through workload identity, and the Anthropic key is replaced by workload identity federation. Nothing is long-lived in the environment.

## PII

- The business request is **redacted before it reaches any model or any log** ([`governance/pii.py`](../src/sdlc_loop/governance/pii.py), applied in `RunService.start`). Identical values map to stable placeholders (`<EMAIL_1>`), so the model can still reason about "the same customer".
- Every audit payload is redacted again on write, as defence in depth: LLM output could echo PII if a redaction had missed something.
- A `pii_redacted` audit row records entity *counts*, never values.
- The shared system prompt tells every agent that placeholders are intentional and must never be "reconstructed".
- **Tests:** `test_pii_is_redacted_before_any_model_call` asserts no model call ever contains the email or card number. `test_golden_scenarios_have_no_false_positives` guards against over-redaction.
- **Production:** Presidio (`SDLC_PII_BACKEND=presidio`) adds NER for names and addresses. The regex pass stays as a second layer for Luhn-valid cards.

## Audit trail

[`governance/audit.py`](../src/sdlc_loop/governance/audit.py) writes one row per event:

| Event | Recorded fields |
|---|---|
| `persona_call` / `parse_failure` | Persona, handoff, attempt, model, **input hash** (SHA-256 of model + system prompt + user content), tokens (uncached, cache-write, cache-read, output), cost, latency, route reason, output artifact (redacted), counterfactual costs |
| `evaluation` | Five scores, overall score, critical flag, verdict, which judge decided, feedback, deterministic findings |
| `escalation` / `human_decision` | Reason, reviewer identity (the actor), action, guidance |
| `code_verification` | Measured syntax and test results |
| `run_started` / `run_completed` / `run_aborted` / `run_failed` | Request hash, lever configuration, error |

The trail is protected in two ways:

- **Append-only.** SQLite triggers abort any `UPDATE` or `DELETE` (tested).
- **Tamper-evident.** Every row stores `prev_hash` and `row_hash`. `sdlc verify-audit` recomputes the chain and reports the first bad row. The test `test_audit_tampering_is_detected` drops the trigger, rewrites a verdict and confirms the chain catches it.

**Production:** stream rows to WORM storage (S3 Object Lock) or an append-only warehouse table, and anchor the chain head periodically in an external timestamp.

## Access control

| Role | Can |
|---|---|
| `operator` | Start runs; read runs |
| `reviewer` | **Resume escalated runs** (the human-in-the-loop decision); read runs and audit |
| `auditor` | Read runs and audit trails |

This is enforced in [`api/auth.py`](../src/sdlc_loop/api/auth.py) and tested in `test_role_based_access_on_resume`, where an operator key gets a 403 on `/resume`.

The reviewer's identity is written into the `human_decision` audit row as the actor, so every override of the evaluator is attributable. With no keys configured, the API runs in an explicit local-dev mode.

**Production:** replace API keys with OIDC/JWT from the corporate IdP. Map reviewer groups per handoff: an architecture board reviews Architect→Dev escalations, while product reviews PM→BA.

## Browser security

- **Content-Security-Policy** on the UI: `default-src 'self'`, no inline or third-party scripts or styles, `frame-ancestors 'none'`. Swagger and ReDoc are exempt because they load assets from a CDN.
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` on every response (tested in `test_security_headers`).
- Model output is untrusted, so the UI renders it with `textContent` only. `test_frontend_never_uses_innerhtml` fails the build if `innerHTML` is introduced.
- The API key a reviewer types into the UI lives in `sessionStorage`, which is cleared when the tab closes. Only the theme preference goes to `localStorage`.

## Prompt injection

- The request is wrapped in `<untrusted_business_request>`, and every artifact goes inside its own XML tags. The shared preamble states that tagged content is data and never an instruction.
- Even a fully "convinced" model cannot approve its own work. Gate decisions are computed in code from five integer scores. The judge sees the artifact as data and cannot change thresholds, routing or the verdict rule.
- The deterministic pre-gate and schema validation do not read natural-language instructions at all.
- `scenarios/probes.jsonl` includes an injection probe (`inject-1`) for live testing.

## Executing generated code

The QA stage always syntax-checks Dev's output with `ast.parse`, which executes nothing. Running the generated tests is **opt-in** (`SDLC_EXECUTE_GENERATED_CODE=true`). When enabled it uses:

- a throwaway temp directory, with path-traversal-safe writes (tested);
- a scrubbed environment, so no API keys are inherited;
- a timeout;
- `subprocess` with a fixed argv and no shell.

This is adequate for a local exercise, **not** for production. There it must run in an isolated container (gVisor or Firecracker) with no network egress and no credentials.

## Checkpoint integrity

LangGraph's msgpack deserialiser is given an explicit allowlist of the schema classes that live in state ([`graph/checkpoint.py`](../src/sdlc_loop/graph/checkpoint.py)). A tampered checkpoint therefore cannot instantiate arbitrary types.

## Scaling beyond one process

Runs execute in the server process and state lives in SQLite, so production runs **one worker**. To scale out:

- move checkpoints to `PostgresSaver` (same interface);
- move the audit log to Postgres or a warehouse;
- execute runs on a worker queue instead of FastAPI background tasks.

The run-status logic already distinguishes "executing here" from "stopped", and would need a shared lease table for multiple workers.

## Data retention (policy, not code)

- Audit rows are retained according to the organisation's SDLC-evidence policy, typically 7 years for regulated change management.
- Checkpoints are operational state and can be purged 30 days after a run completes.
- Eval reports contain only synthetic scenarios.
