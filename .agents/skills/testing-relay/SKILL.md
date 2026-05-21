---
name: testing-relay
description: End-to-end testing of the grokManager relay API. Use this when verifying OpenAI- or Anthropic-protocol behavior, content negotiation on /v1/models, real Grok account integration, or any /v1/* endpoint via curl. Covers local dev server boot, admin token injection, and the streaming-by-default gotcha.
---

# Testing the grokManager Relay

This skill captures how to test the relay end-to-end against real Grok session JWTs on the local dev server.

## 1. Default credentials (local dev only)

The shipped `.env` for local dev contains placeholder strings — they are NOT secrets and are only valid for `127.0.0.1` development. Export them once per shell:

```bash
# Read from .env to avoid hardcoding the literal values in code/examples
export API_KEY="$(grep -E '^GROK_APP_API_KEY=' .env | cut -d= -f2-)"
export ADMIN_KEY="$(grep -E '^GROK_APP_API_KEY=' .env | cut -d= -f2-)"  # admin uses the same key in default config
```

For production-style testing, set `GROK_APP_API_KEY` in `.env` and restart.

## 2. Starting the local server

```bash
cd /path/to/grokManager
nohup .venv/bin/granian --interface asgi --host 127.0.0.1 --port 8000 --workers 1 app.main:app > /tmp/grokmanager.log 2>&1 &
sleep 3
curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8000/   # expect 307
```

**CRITICAL — restart after code changes**: granian does NOT auto-reload. Any time you edit `app/**`, kill the process (`pkill -f 'granian.*app.main:app'`) and start it again. If you don't, you'll be testing stale code and content-negotiation features can silently appear broken.

## 3. Injecting test accounts (real Grok JWTs)

The relay refuses to expose any model in `/v1/models` until at least one *manageable* (= active, non-deleted) account is in a pool whose tier matches that model's `pool_candidates()`.

```bash
# Tokens must be strings (NOT objects). pool=auto triggers async tier detection.
curl -sS -X POST \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8000/admin/api/tokens/add \
  -d '{"tokens":["<jwt-1>","<jwt-2>"],"pool":"auto"}'
```

Then wait a few seconds and confirm:

```bash
curl -sS -H "Authorization: Bearer ${ADMIN_KEY}" http://127.0.0.1:8000/admin/api/tokens | python3 -m json.tool
```

Look for `status: "active"`. `status: "expired"` means the JWT was rejected by Grok upstream.

**Secret hygiene**: never echo Grok JWTs in tool output. Store them in `/home/ubuntu/grok-tokens.txt` mode 600, OUTSIDE the repo. Never include them in commits, PR descriptions, or comments.

## 4. Streaming-by-default gotcha

`features.stream = true` is the shipped default (`data/config.toml`). This means **omitting** the `stream` field in a `POST /v1/chat/completions` or `POST /v1/messages` body still returns SSE — not JSON.

To get a single JSON response, pass `"stream": false` explicitly:

```bash
curl -sS -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/chat/completions \
  -d '{"model":"grok-4.20-0309-non-reasoning","messages":[{"role":"user","content":"Reply PONG"}],"max_tokens":10,"stream":false}'
```

## 5. Content negotiation on /v1/models

The SAME endpoint serves OpenAI or Anthropic shape based on the `anthropic-version` request header:

```bash
# OpenAI shape
curl -sS -H "Authorization: Bearer ${API_KEY}" http://127.0.0.1:8000/v1/models
# Anthropic shape
curl -sS -H "Authorization: Bearer ${API_KEY}" -H "anthropic-version: 2023-06-01" http://127.0.0.1:8000/v1/models
```

404 errors also differ in shape: OpenAI returns `{error:{type:"invalid_request_error"}}`, Anthropic returns `{type:"error",error:{type:"not_found_error"}}`. Same logic applies to `GET /v1/models/{id}`.

## 6. Anthropic count_tokens

Non-billing endpoint that estimates input tokens for a request:

```bash
curl -sS -H "Authorization: Bearer ${API_KEY}" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/messages/count_tokens \
  -d '{"model":"grok-4.20-0309-non-reasoning","messages":[{"role":"user","content":"hello"}]}'
```

`model` is optional; `messages` is required (empty array returns 400 `messages cannot be empty`).

## 7. Model-tier mismatch ≠ relay bug

Grok upstream returns HTTP 403 for some mode/tier combinations the supplied JWT doesn't entitle (e.g. EXPERT reasoning on a free-tier account). The relay correctly forwards this as `{error:{type:"upstream_error",code:"upstream_error",message:"Chat upstream returned 403"}}`. When testing, report these as `untested — account entitlement`, not as relay failures.

Also: `grok-4.20-multi-agent-0309` is HEAVY tier — it will NOT appear in `/v1/models` unless a HEAVY-tier account is in the pool. Correct behavior.

## 8. Unit tests

```bash
cd /path/to/grokManager
uv run python -m unittest discover -s tests -v
```

No `httpx` available — route-level tests call coroutines directly with `asyncio.run(...)` and inspect `JSONResponse.body`. See `tests/test_anthropic_compat.py` for the pattern.

## 9. Lint

```bash
uv run ruff check app tests
```

There is no `npm`/`pnpm` step; this is a pure Python project.
