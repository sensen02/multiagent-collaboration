# Stage 1 — ordering and idempotency
Incremental contract: sort every event by `(ts, seq default 0, event_id)`; process each event_id once. Identical duplicates are ignored. Conflicting duplicate payloads retain the first event under that canonical order and add a deterministic error. Output is JSON serializable and canonical. Each section has independent state.
