# National Hackathon Registration Backend

A PostgreSQL-backed FastAPI reference implementation for high-contention team registration. It is designed for multiple application instances; correctness does not depend on process-local locks or an in-memory cache.

## Architecture and concurrency guarantees

- **PostgreSQL is the source of truth.** UUID primary keys, foreign keys, checks, unique constraints, and partial unique indexes enforce core invariants.
- **Track capacity:** registration uses one conditional SQL update: `UPDATE tracks SET reserved_count = reserved_count + 1 WHERE id = :id AND active AND reserved_count < capacity RETURNING id`. The update is atomic under PostgreSQL row locking. Exactly one competing transaction can consume the final seat. The registration, counter update, idempotency result, audit row, and outbox event commit together.
- **Team membership:** team rows are locked `FOR UPDATE` while accepting invitations. Membership count and insertion occur in the same transaction. A partial unique index permits only one active membership per user across all teams. A second partial index supports efficient active-member lookup.
- **Invitation acceptance:** the invitation row is locked, intended user is checked, team row is locked, then invitation status and membership are changed in one transaction. Replays observe a non-pending status and return 409.
- **Idempotency:** `(actor_id, key)` is unique. The key row is inserted with PostgreSQL `ON CONFLICT DO NOTHING`, then locked. A SHA-256 hash of the canonical request detects key reuse with a different payload (409). The idempotency record and registration commit together. A lost response followed by the same key returns the original registration, including its current status. Retention is 30 days in this reference; use a scheduled cleanup job after the published client retry window.
- **Cancellation:** the registration row is locked and only an `active -> cancelled` transition decrements capacity. Repeated DELETE calls return the cancelled resource and do not decrement again. A cancelled registration is never changed back to active by a stale idempotent retry.
- **Notifications:** the outbox event is inserted in the business transaction. A separate worker sends notifications after commit. Provider failure cannot roll back registration. The included worker is a minimal demonstrator; production should add dead-letter handling, provider idempotency, and metrics. The sample worker uses a five-minute `locked_until` lease to recover events claimed by a worker that crashes before delivery.
- **Audit:** team, invitation, registration and track administrative changes create append-only audit rows in the same transaction. Track creation is admin-only.

## API

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | `/users` | Public | Create user; email normalised with trim + casefold |
| POST | `/teams` | User | Create team; caller becomes owner |
| POST | `/teams/{team_id}/invites` | Owner | Invite a user by user ID |
| POST | `/invites/{invite_id}/accept` | Intended user | Accept once |
| POST | `/registrations` | Team owner | Register; requires `Idempotency-Key` header |
| DELETE | `/registrations/{registration_id}` | Owner/admin | Cancel idempotently |
| GET | `/registrations/{registration_id}` | Team member/admin | Retrieve |
| GET | `/tracks` | Public | List active tracks and remaining capacity |
| POST | `/admin/tracks` | Admin | Create a track (supporting admin operation) |

Mock auth uses `X-User-ID: <uuid>` and `X-Role: user|admin`. Create the user first with `POST /users`, then use the returned ID. The admin actor ID is not required to correspond to a user in this mock. This is intentionally a **development-only authentication adapter**, not production identity. Replace it with JWT/OIDC validation, trusted issuer/audience checks, key rotation, and server-derived roles before deployment. Never trust a client-controlled role header in production.

## Install and run (macOS/Linux)

Requirements: Python 3.11+ and Docker Compose.

```bash
cd national-hackathon-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open API docs at `http://localhost:8000/docs`.

## Test

Tests require a **disposable PostgreSQL database**. They are skipped when `TEST_DATABASE_URL` is absent. Start the included database, create a separate test DB, then run:

```bash
docker compose exec db createdb -U hackathon hackathon_test
export TEST_DATABASE_URL='postgresql+psycopg://hackathon:hackathon@localhost:5432/hackathon_test'
pytest -q
```

The suite covers registration success, email normalisation/duplicate rejection, team eligibility, invitee identity, duplicate invitation acceptance, final-seat concurrency, idempotency replay and payload conflict, cancellation replay, malformed JSON, unknown registrations, admin authorisation, and transactional outbox creation. The concurrency test issues two simultaneous requests from separate worker threads against PostgreSQL. Run tests against PostgreSQL, not SQLite, because partial indexes, `ON CONFLICT`, row locks, and `SKIP LOCKED` are PostgreSQL-specific.

## Required written answers

1. **Why is read-then-write capacity checking unsafe?** Two transactions can both read `remaining=1` before either writes. Without a lock or atomic predicate, both increment/reserve, overselling capacity. A process-local mutex also fails with multiple application instances. Use a single conditional update or a locked row.
2. **Concurrency strategy comparison:**
   - *Row-level locking (`SELECT ... FOR UPDATE`):* serializes writers for the selected row; explicit and useful for invitation/team state machines. Can reduce throughput if locks are held too long and requires consistent lock ordering.
   - *Conditional update:* a single statement includes the invariant in its `WHERE` clause and returns whether it won. Efficient for counters/capacity; the database arbitrates across instances. Used for track seats.
   - *Serializable isolation:* detects transactions whose combined effects cannot be serialised and aborts one with a serialization error. Strong but can increase retries and transaction cost; callers must retry safely.
   - *Optimistic concurrency:* update with a version predicate (`WHERE version = old_version`) and increment version. Good for low-contention edits; conflicting writers must retry. A version column is not currently needed for the capacity counter because conditional update is simpler.
3. **Idempotency record:** store actor/client scope, key, canonical request hash, outcome/resource ID, response status (and optionally a response snapshot), created/expiry timestamps. Keep at least as long as the maximum documented client retry/replay window; this implementation uses 30 days. Expire only after that window, and retain longer if financial/legal audit policy requires it. Do not silently reuse expired keys while clients may still retry.
4. **Reliable notifications:** write an outbox event in the same DB transaction as registration. A worker claims pending rows (typically with `FOR UPDATE SKIP LOCKED`), commits the claim, then calls the email provider outside the business transaction. Retry transient failures with backoff; use provider idempotency or a stable event ID to avoid duplicate sends; dead-letter permanent failures. This avoids holding row locks while waiting on an external API.
5. **Crash immediately after commit:** the registration and outbox row remain durable. The HTTP response may be lost, so the client retries with the same key and receives the existing registration. A worker eventually processes the outbox event. This assumes PostgreSQL durability settings and persistent storage are configured correctly.
6. **Database constraints vs application checks:** put uniqueness (normalised email, idempotency key, active membership per user, active team/track registration), referential integrity, legal status values, capacity bounds, and team-size bounds in the database. Keep contextual policy checks (caller owns team, intended invitee, track eligibility, verified-member policy) in application logic, executed within transactions and protected by locks where concurrent changes matter. Database constraints are the final defence against races and bugs.

## Scale and production hardening notes

The million-user / 100k-team scale is well within PostgreSQL's normal indexing and partitioning design envelope, but benchmark with realistic data and workload. Use connection pooling (configured), PgBouncer where appropriate, query plans, statement/lock timeouts, rate limiting, request tracing, and read replicas only for non-authoritative reads. Keep writes on the primary. Consider partitioning high-volume audit/outbox tables by time, and archive expired idempotency rows. The team row is the serialization point for that team's membership changes; track capacity uses a single-row atomic counter. Use a consistent lock order if future flows lock multiple tracks/teams. Add a production-grade auth provider and secrets manager.

## Important limitations of this submission

- Mock header auth is only for local evaluation. It is not safe for public deployment.
- The included outbox dispatcher is a small reference worker, not a complete production queue (no lease recovery, retry schedule, or dead-letter table yet).
- Team invites are invitations to an existing user ID; invitation expiry, email delivery, revoke/decline endpoints, and duplicate pending-invite suppression are intentionally outside the requested API surface.
- The migration creates the current SQLAlchemy metadata as the initial revision. For future schema changes, generate and review incremental Alembic revisions rather than editing an already-applied revision.
