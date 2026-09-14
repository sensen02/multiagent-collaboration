# Rail Dispatch benchmark — Stage 2 incremental change

Preserve Stage 0–1 contracts. Work only inside this workspace; do not inspect parent/sibling directories or use network access.

ARRIVE payload supports `priority` (`emergency`, `passenger`, `freight`; default passenger) and numeric positive `length` (default 1). Unknown priority and nonnumeric/nonpositive length are invalid inputs. Retained train records expose `train_id`, `priority`, `length`, `status`, `section`, and `last_arrive`. `config.capacity` is a nonnegative integer per section, default 1. At capacity, an emergency ARRIVE may preempt the lowest-priority active train only when strictly higher priority; choose the victim by `(priority rank, train_id)`, mark it `held`, and activate the emergency. Otherwise the arrival is rejected and not accepted. `held` does not consume capacity.

FAILURE applies only to an existing active train, marks it `failed`, releases its capacity, and is accepted. A repeated failure or failure of unknown/non-active train is ignored. REPAIR applies only to `held`: reactivate it only when capacity is available, otherwise leave it held and do not accept the repair. A failed train is not repaired by REPAIR. DEPART applies only to active; CANCEL removes an existing train. Unknown/no-op lifecycle events are not added to `accepted_event_ids`. Preserve deterministic behavior under reordering/duplicates and keep `safe` truthful.

Run and extend public tests. Use native delegation where useful. All root/child agents must remain on gpt-5.6-sol; never invoke Astra or another model.
