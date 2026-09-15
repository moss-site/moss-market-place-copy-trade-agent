# Infrastructure resilience review (2026-09-15)

PR scope: fix the public snapshot `56fbc9f`. No mainnet action, service restart or
strategy/risk parameter changes were executed during development.

## Bugs / impact

1. `run_moss_poller` returned permanently if registration or baseline reads failed.
   `gather(return_exceptions=True)` did not expose failed sibling tasks while the
   process remained alive. Registration is called independently by CLI, market
   validation, WS, poller and reporter, amplifying startup load.
2. Metadata was refreshed independently per instance every 600s, and startup
   forced a refresh. An 84-instance fleet with 11 DEXes can generate ~2016 weighted
   units/minute from this component alone (code estimate, not measured traffic).
3. SDK reads and exchange writes shared `hl_api_url`. Simply replacing it with a
   provider URL can select the wrong signing domain. SDK Exchange also constructs
   another Info and repeats metadata queries during construction.
4. HTTP reads lacked bounded transient retry and Retry-After handling. WS reset its
   backoff immediately on connect, could reconnect immediately on normal EOF or an
   invalid ready frame, and did not limit waiting for ready.
5. Account-mode read errors were treated as standard/manual mode (fail-open).

## Configuration and safety

Keep `hl_api_url` exactly the official mainnet/testnet base for signing and writes.
Optional `hl_info_url` is a native `/info` **base**, e.g. `https://provider.example`;
not `.../hypercore` (JSON-RPC), nor `.../info`. Provider must implement native Info
methods and use the same network. No automatic fallback silently mixes networks.

Set `hyper_metadata_cache_dir` to the SAME persistent directory for every instance
under one trusted OS account, e.g. `/data/app/shared/hypercore-metadata`. Without an
override it is `<FOLLOW_STATE_DIR>/cache/metadata`; if FOLLOW_STATE_DIR is different
per instance, a common explicit override is required for cross-instance savings.
Cache keys include endpoint and payload. Only meta/spotMeta/perpDexs are cached
(600s); balances, prices, account permissions and NAV data are never cached.
Disk lock single-flight works across processes. Rate-limit failures have a short
cooldown; malformed/error responses are not accepted as successful metadata.
The existing per-instance supported-coin cache remains compatible. Force refresh
rebuilds that derived cache but still respects the shared metadata TTL.

Registration uses a 60s per-instance exact-binding cache with an interprocess lock.
It stores only public response fields (never private keys/signatures). Agent, API,
wallet/main/builder binding changes miss the cache. The server still authenticates
subsequent signed reads; this cache is not a replacement for authorization checks.

Read retries: at most 4 attempts / nominal 45s budget, timeout at most 10s per
attempt; 429/408/selected 5xx and transport failures only, exponential jitter and
Retry-After (seconds or HTTP date). A Retry-After exceeding the budget is surfaced,
not shortened. `[INFO_RETRY_EXHAUSTED]` tells orchestration NOT to multiply retries.
This is a synchronous socket/read budget, not an OS-level whole-process deadline.

Moss reporting POSTs remain single-attempt at HTTP level; existing durable outbox
owns retries. Exchange/order/transfer submissions are NOT automatically retried.
Existing event IDs, pending-fill replay, per-coin locks and baseline logic are unchanged.
Permanent startup auth failures block the poller rather than retrying indefinitely;
transient read failures restart only the poller with bounded backoff. Other
unexpected task exceptions are surfaced, not automatically replayed.

`task_health.json` records current-process WS ready/retry, poller success/retry and
balance freshness separately from PID status. It is not proof of fills, reports
accepted by the marketplace, or NAV settlement; verify those separately.

## Tests / rollout

`python -m unittest discover -s tests` (declared requirements installed).
Tests use mocked HTTP (including real SDK construction), deterministic PUBLIC
fixture keys, no live orders. Covers read/write routing including xyz asset ID,
429/401/deadline/JSON failures, task recovery, registration binding isolation,
no persisted signature, and cross-process metadata single-flight.

Do not merge/deploy until reviewed. Canary one main and one xyz account, inspect
fresh funds and task health, then expand in small staggered batches. Restoring
historically stopped live accounts requires the operator's explicit authorization.
Known follow-ups: cross-process weighted request scheduling, provider capacity test,
full metrics/alerting, and report/NAV end-to-end verification remain deployment work.
