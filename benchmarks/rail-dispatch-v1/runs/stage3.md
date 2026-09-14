# Rail Dispatch benchmark — Stage 3 final change

Preserve all Stage 0–2 contracts. Work only inside this workspace; do not inspect parent/sibling directories or use network access.

`config.max_length` is a positive finite number when present. An ARRIVE whose train length exceeds it is rejected without adding the train or event ID. `config.headway` is a nonnegative finite number, default 0. For each section independently, accepted ARRIVE movements must be at least `headway` time units apart (`difference >= headway` is allowed); rejected arrivals do not advance the headway clock. Capacity counts active trains only. `safe` must be true exactly when every section's active count is within capacity, all retained train lengths obey max_length when configured, and accepted arrival movements obey headway. Capacity zero accepts no arrival.

Reject malformed API inputs consistently with TypeError or ValueError: events must be a list of dicts; required fields are event_id/kind/ts/train_id; ts and seq (when present) are finite numbers but booleans are invalid; kinds are ARRIVE/DEPART/CANCEL/FAILURE/REPAIR; config must be a dict; capacity/headway/max_length must obey the rules above. Keep canonical JSON output, run the full public suite, and add useful regression tests.

Use native multi-agent delegation where useful, with the root responsible for integration and verification. All root/child agents must remain on configured gpt-5.6-sol; never request or invoke gpt-6, gpt-6-astra, Astra, or another model. Finish with a concise verification summary.
