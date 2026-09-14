# Rail Dispatch v1 Starter

Implement `rail_dispatch.dispatch(events, config) -> dict` with Python standard library. **Stage 0 only:** basic single-section ARRIVE/DEPART/CANCEL events. Events have `event_id`, `kind`, `ts`, optional `seq`, `train_id`, `section`, `payload`. Later stage contracts are released incrementally by the evaluator; do not assume hidden implementation details.

Run tests with `python -m unittest discover -s tests -v`.

Do not assume any evaluator module layout or inspect files outside this starter package.
