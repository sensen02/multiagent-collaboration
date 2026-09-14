# Rail Dispatch benchmark — Stage 1 incremental change

Keep every earlier Stage 0 behavior unless this contract explicitly changes it. Work only inside this workspace; do not inspect parent/sibling directories or use network access.

Events are now a complete batch and may arrive in any input order. Validate them, then process them in canonical order `(ts, seq if present else 0, string(event_id))`. Each `event_id` is applied at most once. Identical duplicates are ignored. If records sharing an `event_id` differ, canonical ordering chooses the first record and the result adds exactly one deterministic string `duplicate:<event_id>` to `errors`. Each event's explicit `section` is independent; absent section uses `config.section`, default `A`. Unknown/new section names are valid.

The output remains a JSON-serializable dict with exactly `section`, `trains`, `accepted_event_ids`, `errors`, and `safe`. Sort trains by `(section, train_id)` and keep accepted IDs in actual processing order; sort errors. Run and extend public tests. Use native multi-agent delegation only where useful. All root/child agents must remain on configured gpt-5.6-sol; never invoke Astra or another model.
