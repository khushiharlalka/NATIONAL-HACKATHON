# Section D – Global Event-Processing Platform

## 0. Assumptions and sizing
- 2M events/min ≈ 33k events/s on average. Design for 100k/s peak.
- Registration burst: 100k req/s, ~5% of which succeed as writes (the rest are reads, retries, or waitlist).
- 500k concurrent connections, with roughly 5k per WebSocket gateway pod → ~100 pods globally, plus 2× headroom.
- 3 regions (e.g. ap-south, eu-west, us-east). Each event has one **home region**, chosen when it is created.
- Paid registrations go through an external payment provider that supports idempotency keys.

---

## 1. Architecture diagram

```
             Clients (web / mobile)            Admin users (SSO + MFA, VPN/ZTNA)
                     │                                      │
          Global DNS / Anycast + CDN + WAF            Separate admin ingress
          (geo-routing, bot/rate limits,                    │
           static assets, cached event pages)        Admin API (own deployment,
                     │                                own DB role, audited)
        ┌────────────┴─────────────── per region ───────────┴──────────────┐
        │  API Gateway (authN, token-bucket rate limit, request size,       │
        │               idempotency-key check, load shedding)               │
        │        │                 │                    │                   │
        │  Registration Svc   Team Svc / Check-in Svc   WebSocket Gateway   │
        │  (sync, critical)   (sync)                    (live check-in feed)│
        │        │                 │                    │                   │
        │  Admission queue ──► Registration workers     │                   │
        │  (virtual waiting     │                       │                   │
        │   room, token-based)  ▼                       │                   │
        │              Primary SQL (Postgres, partitioned by event_id)      │
        │              + sync replica (same region) + async cross-region    │
        │                   │  transactional outbox                         │
        │                   ▼                                               │
        │   Event log (Kafka, partitioned by event_id/user_id)              │
        │      ├─► Notification Svc ─► Email/SMS/Push providers (+fallback) │
        │      ├─► Fraud Svc (stream scoring → risk flags)                  │
        │      ├─► Analytics pipeline ─► columnar store (dashboards)        │
        │      └─► Cache invalidation / search index                        │
        │   Redis (cache, counters, rate-limit state; NOT source of truth)  │
        └───────────────────────────────────────────────────────────────────┘
     Cross-region: async DB replication of reference data; Kafka MirrorMaker
     for analytics; global uniqueness service (see §3)
```

---

## 2. Components and responsibilities

| Component | Responsibility | Sync/Async |
|---|---|---|
| Edge (CDN/WAF) | TLS termination, geo-routing, bot filtering, caching public event pages, first-line rate limiting | Sync |
| API Gateway | JWT validation, per-user/IP/tenant token buckets, idempotency-key requirement on POSTs, priority-based shedding | Sync |
| Identity Svc | Signup/login, OAuth/OIDC, token issuance, MFA | Sync |
| Registration Svc | Seat reservation, capacity enforcement, payment orchestration, writes the registration and an outbox row in one transaction | Sync (admission is queued) |
| Waiting room | Issues signed, time-bound admission tokens during bursts so the DB sees a controlled rate | Sync token check |
| Team Svc | Team create/join, size limits, captain role | Sync |
| Check-in Svc | Validates signed QR tickets, marks attendance (idempotent) | Sync |
| WebSocket Gateway | Holds 500k connections; fans out check-in and status updates from Kafka | Async push |
| Notification Svc | Consumes events and sends email/SMS/push with retry, DLQ and provider failover | Async |
| Fraud Svc | Velocity, device and IP-reputation scoring on the stream; flags or holds registrations | Async (can block payment capture) |
| Analytics | Stream into a columnar store; admin dashboards read only from it, never from OLTP | Async |
| Admin API | Event config, capacity, refunds, user support. Separate ingress, roles and audit | Sync |

---

## 3. Core data model and key constraints

```
users(user_id PK, email_hash UNIQUE, email_enc, name_enc, home_region,
      created_at, status)
events(event_id PK, home_region, capacity, reg_open_at, reg_close_at,
       version)
registrations(registration_id PK, event_id, user_id, status
      [PENDING|CONFIRMED|CANCELLED|WAITLISTED], idempotency_key,
      payment_intent_id, created_at,
      UNIQUE(event_id, user_id),            -- one registration per user per event
      UNIQUE(idempotency_key))              -- replayed requests return same row
event_capacity(event_id PK, confirmed_count, capacity,
      CHECK (confirmed_count <= capacity))  -- no overselling
teams(team_id PK, event_id, name, captain_id, max_size,
      UNIQUE(event_id, name))
team_members(team_id, user_id, event_id, PRIMARY KEY(team_id,user_id),
      UNIQUE(event_id, user_id))            -- one team per user per event
checkins(event_id, registration_id PK, checked_in_at, gate_id)  -- idempotent
payments(payment_id PK, registration_id UNIQUE, provider_ref UNIQUE,
      amount, status)                       -- at most one charge per registration
outbox(id PK, aggregate_id, type, payload, created_at, published_at NULL)
processed_messages(consumer, message_id, PRIMARY KEY(consumer,message_id))
audit_log(append-only, admin_id, action, target, before, after, ts)
```

Invariants are enforced by the database (unique keys and CHECK constraints), not by application code or Redis.

---

## 4. API boundaries and inter-service communication

**Public (REST/JSON over HTTPS):**
- `POST /v1/events/{id}/registrations` (header `Idempotency-Key`, admission token) → 201, or 202 + status URL
- `GET /v1/registrations/{id}`
- `POST /v1/teams`, `POST /v1/teams/{id}/members`
- `POST /v1/checkins` (signed QR payload)
- `WS /v1/live/{event_id}`

**Admin:** `/admin/v1/*` runs on a separate host and ingress and is never exposed through the public gateway.

**Internal communication:**
- **Synchronous** calls use gRPC with mTLS. They are used only where the caller needs the answer now: auth, capacity check plus reservation, payment authorisation. Every call has a deadline (e.g. 300 ms), a circuit breaker and a bulkhead (separate pool per dependency).
- **Asynchronous** communication uses Kafka events published via the transactional outbox (`RegistrationConfirmed`, `TeamJoined`, `CheckedIn`). Delivery is at-least-once, so consumers deduplicate on `message_id` using `processed_messages`.

---

## 5. Deployment and scaling strategy
- Each region runs its own Kubernetes cluster. Stateless services autoscale on RPS and p95 latency (not only CPU), and keep a **pre-warmed minimum** before announced launches, because autoscaling takes minutes and the burst takes seconds.
- Postgres is partitioned by `event_id`. Each hot event gets a dedicated partition or shard. There is one synchronous replica per region (the HA target) plus read replicas. PgBouncer caps connections.
- Kafka partitions are keyed by `event_id` (ordering per event) or `user_id`. Partition count is sized for the 100k/s peak.
- WebSocket gateways scale horizontally behind an L4 load balancer and subscribe only to the events their connected clients follow.
- Deployments are canary (1% → 10% → 100%) with automatic rollback on SLO burn.

---

## 6. Authentication, authorisation and data protection
- **Users:** OIDC. Access JWTs are short-lived (≤10 min); refresh tokens rotate and are revocable.
- **Authorisation:** RBAC (attendee, team captain, organiser, admin) plus resource checks (e.g. a user can only read their own registration). Checks are enforced in the services, not only at the gateway.
- **Admin isolation:** separate ingress behind ZTNA/VPN, SSO with hardware MFA, separate DB role with least privilege, just-in-time elevation for destructive actions, two-person approval for bulk refunds or exports, and an immutable audit log.
- **Service-to-service:** mTLS with workload identities (SPIFFE/service-mesh certificates, auto-rotated) plus per-service authorisation policy. Secrets live in a vault, never in images or env files in the repo.
- **In transit:** TLS 1.2+ at the edge and mTLS internally.
- **At rest:** disk/KMS encryption everywhere. PII fields (email, name, phone) are additionally encrypted at field level with per-region keys. Lookups use a keyed hash (`email_hash`).
- **Data minimisation:** collect only what registration needs (no DOB or ID numbers unless an event legally requires them). Analytics and fraud receive pseudonymous IDs. PII is kept in the user's home region for residency. Retention TTLs apply, and deletion uses crypto-shredding.
- **Replay and abuse prevention:** idempotency keys; signed admission tokens and QR tickets with expiry and a nonce (`jti`, checked once); request timestamps within ±5 min; WAF/bot detection and CAPTCHA only when risk is high; per-user/IP/device rate limits; payment webhooks verified by signature plus event-ID deduplication.

---

## 7. Failure handling, observability and disaster recovery

### 7.1 Traffic and resilience
- **Absorbing 100x:** the CDN serves all read-only pages. The edge and gateway rate-limit. A **virtual waiting room** turns the spike into a queue: clients receive a signed position token and are admitted at the rate the DB can safely sustain (measured, e.g. 5k writes/s per event shard). Pre-warmed capacity covers the first seconds; autoscaling covers the rest.
- **Must be synchronous:** login/token validation, admission-token check, seat reservation plus capacity check, payment authorisation, check-in validation. The user needs the answer and it guards an invariant.
- **Can be asynchronous:** confirmation email/SMS, analytics, fraud scoring (except a fast pre-check), search indexing, dashboards, receipts, team-invite notifications, live feed fan-out.
- **Preventing retry storms:** clients use exponential backoff with full jitter and a capped number of attempts, and honour `Retry-After`. Servers enforce a retry budget (retries ≤10% of requests), never retry at more than one layer, and use circuit breakers. Idempotency keys make retries safe, so a retry never creates a second record.
- **Backpressure:** bounded queues everywhere. When a queue or the DB pool is full, the gateway returns 429/503 with `Retry-After` instead of queueing indefinitely. Kafka consumers pull at their own rate, and lag is the scaling signal. Shedding is priority-based: analytics, dashboards and non-critical reads go first, registrations and check-in last.
- **Notification provider down:** the outbox → Kafka path keeps messages durable. The circuit breaker opens and the service fails over to a secondary provider. If both are down, messages wait in the retry topic with backoff, then a DLQ, with alerts. The registration still succeeds, because the in-app status page and WebSocket show the confirmation.
- **Cache stampede:** request coalescing (single-flight per key), probabilistic early refresh, TTL jitter, serve-stale-while-revalidate, and a lock with a short lease for rebuilding.

### 7.2 Data and consistency
- **Strong consistency:** registrations, capacity counts, payments, team membership limits, user identity (email uniqueness), check-in status. These use a single writer per event (its home region) and serialisable or row-locked transactions.
- **Eventual consistency:** analytics, dashboards, notification status, fraud scores, live counters shown to users, search, the cross-region read copy of event pages.
- **Partitioning:** OLTP is partitioned by `event_id` (registration, team and check-in queries are all scoped to an event). The users table is partitioned by `user_id` hash within the home region. Kafka uses the same keys.
- **Hot keys/partitions:** a big event is a hot key. It gets its own shard. The capacity counter can be split into N sub-counters ("seat buckets") that are summed for display; the admission rate caps write pressure. Hot read keys are served from the CDN or a local in-process cache with a short TTL.
- **Uniqueness across regions:** each event is owned by one home region, so `UNIQUE(event_id, user_id)` is enforced in one database. Global email uniqueness is enforced in the user's home region via a global directory (email_hash → home_region) stored in a strongly consistent store (e.g. Spanner/CockroachDB or a single-leader table), written once at signup. IDs are generated with no coordination (UUIDv7 / region-prefixed).
- **Cross-region failover and conflicts:** single-leader per event avoids write conflicts in normal operation. On region loss, ownership of the event is promoted to the standby region using fencing (epoch number in the leader lease) so the old leader cannot write. After recovery, writes from the old region past the fence are rejected and reconciled: the newer epoch wins, and duplicates are resolved by unique keys plus payment-provider reconciliation (refund the extra charge, flag it for review).

### 7.3 Observability
**SLIs and SLOs:**

| Type | SLI | SLO |
|---|---|---|
| Availability | % of registration requests that return non-5xx (429 counted separately) | 99.95% monthly |
| Latency | p99 registration confirm; p99 check-in | < 800 ms; < 300 ms |
| Correctness | oversold seats; duplicate registrations; duplicate charges (from daily reconciliation) | 0 |
| Freshness | notification sent within 5 min; analytics lag | 99%; < 2 min |

**Logs:** structured JSON with `trace_id`, `request_id`, `idempotency_key`, `user_id` (pseudonymised), route, status, latency, retry count, shed reason. No PII or tokens.

**Metrics:** RED per service (rate, errors, duration); USE for DB, Redis and Kafka (pool saturation, replication lag, consumer lag); queue depth; circuit-breaker state; 429/503 counts; retry rate; payment-provider error rate; outbox age.

**Traces:** OpenTelemetry end to end (edge → gateway → service → DB → Kafka → consumer) with tail-based sampling that keeps all errors and slow traces.

Alerts fire on SLO burn rate, not on raw thresholds.

### 7.4 Backup and DR
- **RPO / RTO:** registrations and payments RPO ≈ 0 within a region (synchronous replica) and ≤ 5 s cross-region (async). RTO is 15 min for regional failover and 5 min for a primary DB failover within a region. Analytics: RPO 1 h, RTO 4 h.
- **Backups:** continuous WAL archiving plus daily snapshots, copied cross-region and immutable (object lock). Kafka retention is 7 days for replay.
- **Testing:** monthly restore tests and quarterly region failover drills.

### 7.5 Safe schema changes
Use expand → migrate → contract:
1. Add nullable columns or new tables (backward compatible).
2. Deploy code that writes both the old and new form.
3. Backfill in throttled batches.
4. Switch reads (behind a feature flag).
5. Drop the old form in a later release.

Indexes are created concurrently, there are no long locks (lock timeout is set), and event schemas are versioned in a schema registry with compatibility checks in CI.

---

## 8. Extreme failure scenario (chronological)

| Time | What happens | Action |
|---|---|---|
| T+0–2 s | Traffic ×100 | The CDN absorbs page loads. Edge/gateway token buckets reject the excess with **429 + Retry-After + jitter**. The waiting room issues position tokens, so the DB only sees the admission rate. **Shed:** analytics, dashboards, recommendations, non-critical reads. |
| T+2 s | Redis unavailable | Its circuit breaker opens. Rate limiting falls back to **local in-memory per-pod buckets** (conservative limits). Cache reads go to the DB with single-flight plus a **reduced admission rate**, so the DB is protected. Correctness is unaffected because Redis holds no source-of-truth data. **Degraded:** live counters are hidden or stale. |
| T+3 s | One DB replica unhealthy | The health check removes it from the read pool. If it is the synchronous replica, the primary switches to the other replica in the same zone; if none is available, admission is lowered further rather than risk data loss. Reads that need freshness go to the primary; the rest are served stale or shed. |
| T+5 s | Email failing | The notification circuit breaker opens and traffic fails over to the secondary provider. If that also fails, messages are **queued** in the retry topic and DLQ. Registrations keep succeeding, and confirmation is shown in-app. |
| T+5–30 s | 15% timeouts; clients retry aggressively | Retries carry the same **Idempotency-Key**. The server returns the stored result (or 409 "in progress") and does no new work. The retry budget and circuit breakers stop internal amplification. The gateway lowers concurrency (adaptive limit) to bring p99 back under the deadline. **Retried:** only idempotent operations, with backoff. |
| T+30 s+ | Stabilise | Autoscaling adds pods. Admission rate rises step by step while SLOs hold. The canary or feature flags **roll back** any deploy correlated with the errors. |
| After | Recovery | Redis is rebuilt cold (warming is gated). The replica resyncs. The DLQ is drained at a controlled rate. Payment reconciliation runs. |

**Preserving correctness (no duplicate records or charges):**
1. The client generates an `Idempotency-Key` per registration attempt. The server stores key → result in Postgres (not Redis).
2. One transaction: insert the registration as `PENDING` (`UNIQUE(event_id,user_id)`, `UNIQUE(idempotency_key)`), decrement capacity under the CHECK constraint, and write the outbox row.
3. Payment is created with the provider using **the same idempotency key / registration_id**, so a retried charge request returns the original charge. `payments.registration_id UNIQUE` allows at most one recorded payment.
4. The payment webhook (signature-verified, deduplicated by event ID) moves the status to `CONFIRMED`. If there is no webhook, a reconciler polls the provider for stuck `PENDING` registrations.
5. A crash after commit but before the response is safe: the client retry hits the idempotency key and receives the committed result.
6. Timeouts with unknown outcome are never retried blindly. Status is checked through the key.
7. Expired `PENDING` registrations (payment not completed within N min) release their seat through a compensating transaction. Daily reconciliation against the provider refunds any orphaned charge.

---

## 9. Architecture trade-offs

1. **Waiting room vs. scaling the DB to raw peak.** Users see a queue, but writes stay bounded and correct, which is far cheaper than provisioning OLTP for a 2-second ×100 spike. *Alternative:* pure autoscaling cannot react within 2 s and still bottlenecks on row locks for one hot event.
2. **Single home region per event vs. multi-master.** Writes for a user registering far from an event's region pay cross-region latency (~150 ms), and failover needs fencing. In return there are no write conflicts and uniqueness/capacity is simple to enforce. *Alternative:* multi-master with CRDTs cannot express "capacity ≤ N" safely without coordination.
3. **Postgres plus outbox vs. writing directly to Kafka and the DB (dual write).** Costs an extra table and publisher-lag monitoring, but there is no lost or phantom event and it gives exactly-once *effect* with consumer deduplication. *Alternative:* event sourcing on Kafka alone makes constraint checks (unique, capacity) hard.
4. **Redis as an accelerator only (not source of truth).** Lower peak throughput when Redis is down, but Redis failure degrades performance, not correctness. *Alternative:* Redis seat counters are faster but risk overselling on failover or data loss.
5. **Asynchronous notifications and fraud.** A fraudulent registration can be briefly `CONFIRMED` before being flagged (mitigated by a fast synchronous pre-check and by holding payment capture), but the critical path stays short and independent of third-party uptime.
