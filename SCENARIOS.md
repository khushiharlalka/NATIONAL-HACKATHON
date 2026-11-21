# Race-condition walkthroughs

## 1. Last available seat
Both requests execute the conditional `UPDATE tracks ... WHERE reserved_count < capacity RETURNING id`. PostgreSQL serializes updates to the same track row. The winner increments 0 to 1 and returns the ID. The waiting updater re-evaluates the predicate against the committed row (now 1), returns no row, and the API responds 409. Because the failed transaction rolls back, no registration or outbox record is created for the loser.

## 2. Lost response
The registration, idempotency record's `registration_id`, counter increment, audit record and outbox event commit atomically. If the response is lost after commit, the retry's `INSERT ... ON CONFLICT DO NOTHING` encounters the existing `(actor_id,key)` row, then locks and reads it. A matching request hash returns the original registration ID; it does not increment capacity again. A different hash returns 409.

## 3. Simultaneous invitation acceptance
Both requests lock the same invitation row. The first verifies the intended user, locks the team row, checks membership, changes `pending` to `accepted`, and inserts the membership before commit. The second then sees `accepted` and returns 409. A request from a different user fails the intended-user check with 403. The partial unique index on active membership is an additional guard against cross-team races.

## 4. Cancellation retry
The cancellation locks the registration row and only executes the capacity decrement when the state is `active`. It commits `cancelled` and the decrement together. A retry sees `cancelled`, performs no decrement, and returns the same resource. A stale POST replay with the original idempotency key also returns the existing cancelled row; there is no transition back to active.
