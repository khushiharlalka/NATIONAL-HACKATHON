# Section C – Security Review and Secure Remediation

**Hackathon Technical Selection – Extreme Challenge Pack**  
**Section:** C – Security Review and Remediation (10 Marks)  
**Candidate ID:** ____________________  
**Programming Language:** Python 3.10+  
**Database:** SQLite (local synthetic testing only)  
**Suggested Assessment Duration:** 45 minutes  

---

## 1. Executive Summary

The supplied user-management service is unsuitable for production use. The review identified critical SQL injection in authentication, fast unsalted MD5 password hashing, missing object-level and administrative authorisation, missing input validation, absent rate limiting, unclosed database connections, positional schema fragility, incomplete transaction and exception handling, lack of row-count checks on mutations, account enumeration risks, and missing audit logging.

The remediated implementation (`secure_service.py`) enforces parameterised SQL queries, memory-hard `scrypt` password hashing with unique cryptographic salts, server-side identity and role verification, database constraints, explicit ACID transactions, controlled domain exceptions, and secret-free audit logging. An automated `pytest` suite (`test_secure_service.py`) verifies these fixes against isolated temporary SQLite databases and synthetic accounts.

This submission is a focused security-remediation implementation rather than a complete production authentication framework. Production deployments require web-framework session/token middleware, distributed rate limiting, dedicated secrets management, centralised tamper-evident logging, and a client-server database such as PostgreSQL for multi-instance concurrency.

---

## 2. Repository Structure & Execution Commands

### Files
- `secure_service.py` – Remediated user-management service (`init_db`, `register`, `login`, `update_user`, `delete_user`, `list_users`).
- `test_secure_service.py` – Isolated `pytest` regression suite and local synthetic SQL-injection reproduction test.
- `README.md` – Complete security review report, threat model, architecture rationale, and operational guidance.

### Setup & Run Commands

```bash
# 1. Install test runner
python -m pip install pytest

# 2. Run the full regression & SQL-injection reproduction test suite
python -m pytest -v

# 3. Run the service module directly (initialises startup dummy hash)
python secure_service.py
```

*Controlled Testing Policy: All tests and demonstrations use isolated temporary databases (`tmp_path` or `:memory:`) and synthetic accounts only. Never run tests against real users, public services, or production systems.*

---

## 3. Scope and Assumptions

- Testing is strictly limited to local databases and synthetic accounts.
- The client is untrusted. Request fields, IDs, roles, and claimed identities are never trusted as proof of identity or authority.
- The `Principal` object must be constructed exclusively by trusted server-side authentication middleware after validating a session or token. A client must never be able to submit or construct a trusted `Principal`.
- Public registration always creates a non-administrator account (`is_admin = 0`).
- Administrative role assignment is a protected, out-of-band operation and is never exposed through public registration or ordinary profile-update requests.
- SQLite is used to keep the demonstration self-contained and reproducible locally; it is not recommended for horizontally scaled, high-write production APIs.

---

## 4. Vulnerability Report (Ranked by Severity)

### 4.1 Severity Ranking

| ID | Finding | Severity | Primary Impact |
|---|---|---|---|
| **V1** | SQL injection in `login` query | **Critical** | Authentication bypass or unauthorised data access |
| **V2** | Unsalted MD5 used for password storage | **Critical** | Rapid offline password cracking and rainbow-table lookup after DB leak |
| **V3** | Missing object-level authorisation in `update_user` / `delete_user` | **Critical** | Horizontal privilege escalation (IDOR) |
| **V4** | Missing administrative permission enforcement & mass-assignment protection | **High** | Vertical privilege escalation |
| **V5** | No login rate limiting or brute-force controls | **High** | Brute-force guessing, credential stuffing, and service load |
| **V6** | Missing input validation, normalisation, and size limits | **Medium** | Malformed records, duplicate case-variant emails, resource abuse |
| **V7** | Database connections not explicitly closed | **Medium** | Connection/file-descriptor leaks and degraded reliability |
| **V8** | Positional `INSERT INTO users VALUES (?, ?, 0)` | **Medium** | Schema fragility and unintended column assignment |
| **V9** | Incomplete transaction and exception handling | **Medium** | Non-atomic partial writes and raw database error leakage |
| **V10** | Absence of audit logging | **Medium** | Weak incident investigation and lack of accountability |
| **V11** | No `rowcount` check on `UPDATE` / `DELETE` | **Medium** | False-positive success responses when target ID does not exist |
| **V12** | Response/timing differences across authentication paths | **Medium** | Discovery of valid account identifiers (account enumeration) |

*(Note: Severity ratings are qualitative and assume the service is reachable by untrusted clients. Actual risk depends on deployment exposure, database permissions, and surrounding controls.)*

### 4.2 Detailed Findings, Triggers, Impacts & Fixes

#### V1 – SQL Injection in `login`
- **Trigger:** `login()` concatenates the user-supplied `email` string directly into the SQL query (`WHERE email = '" + email + "'...`). Crafted input alters SQL syntax.
- **Impact:** An attacker can supply payloads such as `' OR '1'='1' --` to bypass the password predicate, log in as an arbitrary user (including an administrator), or extract data depending on the driver and query context.
- **Fix:** Use parameterised `?` placeholders and pass values separately:
  ```python
  user = db.execute(
      "SELECT id, password_hash FROM users WHERE email = ?",
      (email,),
  ).fetchone()
  ```
  Parameterisation prevents user input from being interpreted as SQL syntax, though it does not replace authorisation checks.

#### V2 – Weak Password Hashing (Unsalted MD5)
- **Trigger:** Database disclosure, backup exposure, or SQL injection read access exposes stored password hashes.
- **Impact:** MD5 is a fast general-purpose hash designed for speed rather than resistance to guessing, making high-speed offline GPU/ASIC cracking inexpensive. Without a unique per-user salt, identical passwords produce identical hashes and are vulnerable to precomputed rainbow tables.
- **Fix:** Use a memory-hard password-hashing function designed for passwords (`Argon2id`, `scrypt`, or `bcrypt`). The remediated code uses Python's `hashlib.scrypt` (`SCRYPT_N = 2**15`, `SCRYPT_R = 8`, `SCRYPT_P = 1`, `SCRYPT_LENGTH = 32`, `SCRYPT_MAXMEM = 64 * 1024 * 1024`), a unique 16-byte random salt via `secrets.token_bytes(16)`, explicit parameter validation on verification, and constant-time comparison via `hmac.compare_digest`. For new production deployments, `Argon2id` via a maintained library is the preferred default.

#### V3 – Missing Object-Level Authorisation (IDOR)
- **Trigger:** An authenticated or unauthenticated caller changes `user_id` in `update_user` or `delete_user` to another user's ID.
- **Impact:** Horizontal privilege escalation—any caller can modify or delete another user's account.
- **Fix:** Derive actor identity from a server-validated `Principal` and enforce `require_owner_or_admin(principal, target_user_id)` on the server before mutating any record. Never treat a client-supplied user ID as proof of identity.

#### V4 – Missing Administrative Permission Enforcement & Mass Assignment
- **Trigger:** A standard user invokes a privileged operation, or submits protected fields such as `is_admin = 1` in an update request.
- **Impact:** Vertical privilege escalation and unauthorised administrative actions.
- **Fix:** Enforce `require_admin(principal)` on admin-only operations (`list_users`), query the actor's current `is_admin` flag from the database on every privileged check, and enforce a strict field allow-list (`set(request) != {"email"}`) in `update_user`.

#### V5 – No Login Rate Limiting
- **Trigger:** An attacker submits high-volume automated login attempts against one account or across many accounts.
- **Impact:** Brute-force password guessing, credential stuffing, account compromise, and denial-of-service load.
- **Fix:** Apply both account-keyed and source-IP-keyed rate limits, progressive delays, monitoring, and risk-based challenges at the API/edge layer using a shared distributed store (such as Redis) rather than process-local counters.

#### V6 – Missing Input Validation and Normalisation
- **Trigger:** Inputs are missing, non-strings, whitespace-padded, mixed-case, malformed, or excessively long.
- **Impact:** Duplicate accounts differing only by case/whitespace (`Alice@example.com` vs `alice@example.com`), unhandled exceptions, or memory/CPU abuse from oversized payloads.
- **Fix:** Validate Python types (`isinstance(..., str)`), normalise emails via `.strip().lower()`, enforce email length bounds (`3..254`) and basic syntactic validation (`[^@\s]+@[^@\s]+\.[^@\s]+`), and enforce password length bounds (`12..1024`).

#### V7 – Database Connections Not Explicitly Closed
- **Trigger:** Repeated invocations of `login`, `register`, `update_user`, or `delete_user`.
- **Impact:** Open SQLite connections and file descriptors accumulate until garbage collection, degrading reliability under load.
- **Fix:** Wrap database access in a `get_db()` context manager that commits on success, rolls back on exception, and always calls `db.close()` in a `finally` block. (Note: Python's built-in `with sqlite3.connect(...)` context manager manages transactions, not connection closure.)

#### V8 – Positional Schema Assumptions
- **Trigger:** `INSERT INTO users VALUES (?, ?, 0)` relies on implicit column count and ordering.
- **Impact:** Schema migrations or column reordering break inserts or write values into unintended columns.
- **Fix:** Explicitly specify target columns (`INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, 0)`) and enforce schema defaults and constraints.

#### V9 – Incomplete Transaction and Exception Handling
- **Trigger:** A database constraint error or mid-operation failure occurs during multi-step writes.
- **Impact:** Related state changes (such as user updates and audit logging) can become inconsistent, and raw database exceptions may leak internal schema details to callers.
- **Fix:** Group related writes inside a single atomic transaction (`get_db()`), roll back on error, and translate internal database exceptions into safe domain exceptions (`ValidationError`, `SecurityError`, `NotFoundError`, `ConflictError`).

#### V10 – Missing Audit Logging
- **Trigger:** Security-sensitive actions (registrations, logins, failed logins, updates, deletions, administrative queries) occur.
- **Impact:** Insufficient forensic trail to detect abuse, investigate incidents, or establish accountability.
- **Fix:** Write structured audit records (`actor_id`, `action`, `target_user_id`, `outcome`, `created_at`) to `audit_logs` within the same transaction as the state mutation, strictly excluding passwords, password hashes, tokens, cookies, and API keys.

#### V11 – False Success Reporting
- **Trigger:** `update_user` or `delete_user` is called with a `target_user_id` that does not exist.
- **Impact:** The original implementation unconditionally returns `True` even when zero rows are affected.
- **Fix:** Check `cursor.rowcount == 0` after executing `UPDATE` or `DELETE` and raise `NotFoundError("User not found")`.

#### V12 – Account Enumeration and Timing Differences
- **Trigger:** Login or registration responses or execution times differ depending on whether an email address exists in `users`.
- **Impact:** Attackers can enumerate valid registered email addresses for targeted phishing or credential stuffing.
- **Fix:** Return generic client-facing responses on authentication failure and execute `verify_password(password, DUMMY_PASSWORD_HASH)` using a fixed startup-generated dummy hash when `user is None` to reduce timing discrepancies. *(Note: `audit_logs` records entries when a valid `actor_id` exists, while unknown-user login attempts emit a generic `logging.warning` without inserting orphan rows into `audit_logs`.)*

---

## 5. Implementation (`secure_service.py`)

```python
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

DB_NAME = "users.db"

# Demonstration parameters. Benchmark on deployment hardware and
# use a maintained Argon2id library for a new production deployment.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LENGTH = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024  # 64 MiB

logging.basicConfig(
    filename="security.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


class SecurityError(Exception):
    """The caller is not authenticated or authorised."""


class ValidationError(Exception):
    """The supplied input is invalid."""


class NotFoundError(Exception):
    """The requested resource does not exist."""


class ConflictError(Exception):
    """The requested operation conflicts with a database constraint."""


@dataclass(frozen=True)
class Principal:
    # This object must be created by trusted server-side middleware
    # after validating the user's session. Never construct it from
    # an ID supplied by the client.
    id: int


@contextmanager
def get_db():
    db = sqlite3.connect(DB_NAME, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Create the local demonstration schema if it does not exist."""
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE
                    CHECK(length(email) BETWEEN 3 AND 254),
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0
                    CHECK(is_admin IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                action TEXT NOT NULL,
                target_user_id INTEGER,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(actor_id) REFERENCES users(id)
                    ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_email
                ON users(email);

            CREATE INDEX IF NOT EXISTS idx_audit_actor
                ON audit_logs(actor_id);
            """
        )


def normalise_email(email):
    if not isinstance(email, str):
        raise ValidationError("Invalid email")

    email = email.strip().lower()

    if not 3 <= len(email) <= 254:
        raise ValidationError("Invalid email")

    # Basic syntactic validation only. Production systems should use
    # a well-maintained email validation policy and verification flow.
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValidationError("Invalid email")

    return email


def validate_password(password):
    if not isinstance(password, str):
        raise ValidationError("Invalid password")

    # Length is measured in Python characters. Production policy should
    # document its Unicode and byte-length handling.
    if not 12 <= len(password) <= 1024:
        raise ValidationError("Password does not meet length policy")


def hash_password(password):
    salt = secrets.token_bytes(16)

    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LENGTH,
        maxmem=SCRYPT_MAXMEM,
    )

    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
        f"{salt.hex()}${derived_key.hex()}"
    )


def verify_password(password, stored_hash):
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False

    try:
        algorithm, n, r, p, salt_hex, expected_hex = stored_hash.split("$")

        if algorithm != "scrypt":
            return False

        n, r, p = int(n), int(r), int(p)

        # Accept only parameters configured by this service. This also
        # prevents an untrusted/corrupted database value from requesting
        # excessive hashing resources.
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False

        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)

        if len(salt) != 16 or len(expected) != SCRYPT_LENGTH:
            return False

        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=SCRYPT_LENGTH,
            maxmem=SCRYPT_MAXMEM,
        )

        return hmac.compare_digest(actual, expected)

    except (ValueError, TypeError, AttributeError):
        return False


# Create a fixed dummy hash once at process startup. It is not a real
# user's password and must never be returned or logged.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def audit(db, actor_id, action, target_user_id, outcome):
    # Do not add passwords, hashes, tokens, cookies, or secrets here.
    db.execute(
        """
        INSERT INTO audit_logs
            (actor_id, action, target_user_id, outcome)
        VALUES (?, ?, ?, ?)
        """,
        (actor_id, action, target_user_id, outcome),
    )


def register(email, password):
    email = normalise_email(email)
    validate_password(password)
    password_hash = hash_password(password)

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO users (email, password_hash, is_admin)
                VALUES (?, ?, 0)
                """,
                (email, password_hash),
            )

            user_id = cursor.lastrowid
            audit(db, user_id, "REGISTER", user_id, "SUCCESS")
            return user_id

    except sqlite3.IntegrityError as exc:
        # The API layer should return a safe, generic response where
        # account enumeration is a concern.
        raise ConflictError("Unable to register with this email") from exc


def login(email, password):
    try:
        email = normalise_email(email)
    except ValidationError:
        logging.warning("LOGIN_FAILURE invalid_input")
        return None

    if not isinstance(password, str):
        return None

    with get_db() as db:
        user = db.execute(
            """
            SELECT id, password_hash
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()

        if user is None:
            # Dummy verification reduces one source of timing difference.
            # A production service should use a fixed, valid dummy hash
            # created at startup rather than generating it per request.
            verify_password(password, DUMMY_PASSWORD_HASH)
            logging.warning("LOGIN_FAILURE invalid_credentials")
            return None

        if not verify_password(password, user["password_hash"]):
            audit(db, user["id"], "LOGIN", user["id"], "FAILURE")
            logging.warning("LOGIN_FAILURE invalid_credentials")
            return None

        audit(db, user["id"], "LOGIN", user["id"], "SUCCESS")
        return Principal(id=user["id"])


def require_owner_or_admin(principal, target_user_id):
    if not isinstance(principal, Principal):
        raise SecurityError("Authentication required")

    with get_db() as db:
        actor = db.execute(
            "SELECT is_admin FROM users WHERE id = ?",
            (principal.id,),
        ).fetchone()

        if actor is None:
            raise SecurityError("Authentication required")

        if principal.id != target_user_id and not actor["is_admin"]:
            raise SecurityError("Permission denied")


def require_admin(principal):
    if not isinstance(principal, Principal):
        raise SecurityError("Authentication required")

    with get_db() as db:
        actor = db.execute(
            "SELECT is_admin FROM users WHERE id = ?",
            (principal.id,),
        ).fetchone()

        if actor is None or not actor["is_admin"]:
            raise SecurityError("Administrator access required")


def update_user(principal, target_user_id, request):
    if not isinstance(request, dict):
        raise ValidationError("Invalid request")

    # Explicit allow-list prevents mass assignment of is_admin or other
    # protected fields.
    if set(request) != {"email"}:
        raise ValidationError("Invalid fields")

    email = normalise_email(request["email"])
    require_owner_or_admin(principal, target_user_id)

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                UPDATE users
                SET email = ?
                WHERE id = ?
                """,
                (email, target_user_id),
            )

            if cursor.rowcount == 0:
                raise NotFoundError("User not found")

            audit(
                db,
                principal.id,
                "UPDATE_USER",
                target_user_id,
                "SUCCESS",
            )
            return True

    except sqlite3.IntegrityError as exc:
        raise ConflictError("Email is already in use") from exc


def delete_user(principal, target_user_id):
    require_owner_or_admin(principal, target_user_id)

    with get_db() as db:
        cursor = db.execute(
            "DELETE FROM users WHERE id = ?",
            (target_user_id,),
        )

        if cursor.rowcount == 0:
            raise NotFoundError("User not found")

        audit(
            db,
            principal.id,
            "DELETE_USER",
            target_user_id,
            "SUCCESS",
        )
        return True


def list_users(principal):
    require_admin(principal)

    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, email, is_admin, created_at
            FROM users
            ORDER BY id
            """
        ).fetchall()

        return [dict(row) for row in rows]
```

### Implementation Notes & Production Caveats
1. `DUMMY_PASSWORD_HASH` is created once at process startup rather than per unknown-user login attempt.
2. `Principal` is an identity container only; in a real HTTP API it must be produced by validated session or token authentication middleware.
3. Authorisation helpers re-query `is_admin` from the database rather than trusting cached role state in a client-supplied payload.
4. For strict high-concurrency APIs, combine authorisation and mutation into a single atomic conditional database statement to eliminate time-of-check/time-of-use (TOCTOU) windows.
5. `executescript()` in SQLite commits implicitly; in production, schema evolution should be managed through controlled migration tooling.
6. A web framework layer must map internal domain exceptions (`ValidationError`, `SecurityError`, `NotFoundError`, `ConflictError`) to safe HTTP status codes and generic client responses.

---

## 6. Regression Test Suite (`test_secure_service.py`)

```python
import hashlib
import sqlite3

import pytest

import secure_service as app


@pytest.fixture
def database(tmp_path, monkeypatch):
    db_path = tmp_path / "test_users.db"
    monkeypatch.setattr(app, "DB_NAME", str(db_path))
    app.init_db()
    return str(db_path)


@pytest.fixture
def users(database):
    alice_id = app.register("alice@example.com", "AliceSecurePassword123!")
    bob_id = app.register("bob@example.com", "BobSecurePassword123!")
    return alice_id, bob_id


def test_valid_login(database, users):
    principal = app.login("alice@example.com", "AliceSecurePassword123!")
    assert principal is not None
    assert principal.id == users[0]


def test_invalid_password(database, users):
    assert app.login("alice@example.com", "WrongPassword123!") is None


def test_sql_injection_attempt_is_rejected(database, users):
    assert app.login("' OR '1'='1' --", "anything") is None


def test_duplicate_email_is_rejected(database, users):
    with pytest.raises(app.ConflictError):
        app.register("alice@example.com", "AnotherSecurePassword123!")


def test_email_normalisation_prevents_case_duplicate(database, users):
    with pytest.raises(app.ConflictError):
        app.register("ALICE@example.com", "AnotherSecurePassword123!")


def test_weak_password_is_rejected(database):
    with pytest.raises(app.ValidationError):
        app.register("new@example.com", "123")


def test_invalid_email_is_rejected(database):
    with pytest.raises(app.ValidationError):
        app.register("not-an-email", "LongEnoughPassword123!")


def test_user_can_update_own_email(database, users):
    alice = app.login("alice@example.com", "AliceSecurePassword123!")
    assert app.update_user(alice, users[0], {"email": "alice-new@example.com"})

    with sqlite3.connect(database) as db:
        email = db.execute(
            "SELECT email FROM users WHERE id = ?",
            (users[0],),
        ).fetchone()[0]

    assert email == "alice-new@example.com"


def test_horizontal_privilege_escalation_is_prevented(database, users):
    alice = app.login("alice@example.com", "AliceSecurePassword123!")
    with pytest.raises(app.SecurityError):
        app.update_user(alice, users[1], {"email": "hacked@example.com"})


def test_non_admin_cannot_list_users(database, users):
    alice = app.login("alice@example.com", "AliceSecurePassword123!")
    with pytest.raises(app.SecurityError):
        app.list_users(alice)


def test_admin_can_list_users(database, users):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (users[0],))

    admin = app.login("alice@example.com", "AliceSecurePassword123!")
    assert len(app.list_users(admin)) == 2


def test_password_is_not_stored_as_plaintext_or_md5(database, users):
    with sqlite3.connect(database) as db:
        stored_hash = db.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (users[0],),
        ).fetchone()[0]

    assert stored_hash.startswith("scrypt$")
    assert stored_hash != "AliceSecurePassword123!"
    assert len(stored_hash.split("$")) == 6


def test_corrupt_password_hash_fails_closed(database, users):
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            ("not-a-valid-hash", users[0]),
        )

    assert app.login("alice@example.com", "AliceSecurePassword123!") is None


def test_nonexistent_user_delete_returns_not_found(database, users):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (users[0],))

    admin = app.login("alice@example.com", "AliceSecurePassword123!")
    with pytest.raises(app.NotFoundError):
        app.delete_user(admin, 999999)

def test_mass_assignment_is_rejected(database, users):
    alice = app.login("alice@example.com", "AliceSecurePassword123!")
    with pytest.raises(app.ValidationError):
        app.update_user(
            alice,
            users[0],
            {"email": "alice@example.com", "is_admin": 1},
        )


def test_successful_login_is_audited(database, users):
    app.login("alice@example.com", "AliceSecurePassword123!")
    with sqlite3.connect(database) as db:
        records = db.execute(
            "SELECT action, outcome FROM audit_logs WHERE actor_id = ?",
            (users[0],),
        ).fetchall()

    assert ("LOGIN", "SUCCESS") in records


def test_failed_login_is_audited_for_known_user(database, users):
    app.login("alice@example.com", "IncorrectPassword123!")
    with sqlite3.connect(database) as db:
        records = db.execute(
            "SELECT action, outcome FROM audit_logs WHERE actor_id = ?",
            (users[0],),
        ).fetchall()

    assert ("LOGIN", "FAILURE") in records


def vulnerable_login(db, email, password):
    query = (
        "SELECT * FROM users WHERE email = '"
        + email
        + "' AND password = '"
        + hashlib.md5(password.encode()).hexdigest()
        + "'"
    )
    return db.execute(query).fetchone()


def test_original_login_is_vulnerable_locally():
    db = sqlite3.connect(":memory:")
    db.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT,
            password TEXT,
            is_admin INTEGER
        )
        """
    )
    db.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?)",
        (1, "synthetic@example.com", hashlib.md5(b"correct-password").hexdigest(), 0),
    )

    result = vulnerable_login(db, "' OR '1'='1' --", "wrong-password")
    assert result is not None
    db.close()
```

---

## 7. Security Concepts & Defence-in-Depth

### 7.1 Why Different Controls Solve Different Classes of Problems

No single security control replaces the others; each mitigates a distinct class of failure:

| Control | Problem Class Solved | What It Does *Not* Solve |
|---|---|---|
| **Parameterised Queries** | Separates SQL command structure from untrusted input, preventing SQL injection. | Does not verify whether an authenticated caller owns a target record. |
| **Password Hashing (`scrypt` / Argon2id)** | Protects stored credentials against offline brute-force and rainbow-table attacks if the database or backups leak. | Does not protect active sessions or stop online credential stuffing without rate limiting. |
| **Least Privilege & Object-Level Auth** | Prevents horizontal (IDOR) and vertical (role) privilege escalation. | Cannot stop SQL injection if queries concatenate raw input strings. |
| **Database Constraints & Transactions** | Enforces invariants (`UNIQUE`, `CHECK`, `FOREIGN KEY`, `NOT NULL`) and atomic all-or-nothing writes at the storage layer. | Cannot evaluate web-session ownership or business-level permissions. |
| **Secure Error Handling** | Prevents internal stack-trace/schema leakage and reduces account enumeration. | Does not block unauthorised mutations if access checks are missing. |
| **Audit Trails** | Provides tamper-evident forensic visibility and accountability for incident response. | Detective control only; does not prevent an attack in real time. |

### 7.2 Authentication vs. Authorisation

| Aspect | Authentication ("Who are you?") | Authorisation ("What are you permitted to access?") |
|---|---|---|
| **Purpose** | Verifies claimed identity | Enforces permissions on actions and resources |
| **Mechanisms** | Passwords, MFA, passkeys, cryptographic tokens | Ownership checks, RBAC/ABAC policies, least privilege |
| **Output** | Trusted server-side `Principal` | Allow or deny (`SecurityError`) decision |
| **Lifecycle** | Performed at login / session validation | Enforced on every protected operation |

### 7.3 Preventing Horizontal and Vertical Privilege Escalation

- **Horizontal Privilege Escalation (IDOR):** Occurs when user `101` manipulates a request identifier to modify or delete user `202`'s account.
  - *Mitigation:* Bind identity to a server-validated `Principal`, verify `principal.id == target_user_id` (or active administrative status) on the server before mutating data, never trust client-supplied user IDs as identity proof, and log denied attempts.
- **Vertical Privilege Escalation:** Occurs when a standard user gains administrative privileges (e.g., submitting `{"is_admin": 1}` in an update request).
  - *Mitigation:* Never accept role attributes from public registration or profile-update payloads, enforce strict input allow-lists (`set(request) == {"email"}`), query current `is_admin` from the database on every privileged check, apply least privilege, and audit administrative actions.

### 7.4 Password Storage Design

Passwords must be hashed one-way (never stored in plaintext or reversible encryption) using a slow, memory-hard password-hashing scheme (`Argon2id`, `scrypt`, or `bcrypt`):
1. Generate a unique cryptographically random 16-byte salt per password (`secrets.token_bytes(16)`).
2. Derive the key using `hashlib.scrypt` with explicit cost parameters (`N=2**15, r=8, p=1, dklen=32, maxmem=64 MiB`).
3. Persist the algorithm identifier, parameters, salt, and derived digest (`scrypt$N$r$p$salt$hash`).
4. On login, validate that stored parameters match the service's allowed configuration (preventing parameter-downgrade or resource-exhaustion attacks), recompute the digest with the stored salt, and compare with `hmac.compare_digest`.

### 7.5 Rate Limiting, Account Lockout, Session Expiry & Secret Rotation

- **Rate Limiting:** Restrict repeated authentication attempts by both normalised account identifier and source IP (e.g., 5 failures per 5 minutes) using a shared distributed store (Redis) across API instances. Combining account and IP keys defends against both single-source brute force and distributed credential stuffing.
- **Account Lockout Trade-Offs:** Permanent or overly aggressive lockouts create a denial-of-service vector where attackers can intentionally lock out legitimate users. Prefer progressive backoff delays, short temporary lockouts, risk-based step-up challenges (CAPTCHA/MFA), and out-of-band user notifications.
- **Session and Token Expiry:**

| Credential Type | Recommended Lifetime & Controls |
|---|---|
| **Access Token** | Short-lived (5–15 minutes), signed with asymmetric/symmetric key |
| **Refresh Token** | Multi-day lifetime with single-use rotation and reuse detection |
| **Server-Side Session** | Configurable inactivity timeout and absolute maximum lifetime, invalidated on logout or password change |
| **Browser Cookies** | `HttpOnly`, `Secure`, `SameSite=Strict` (or `Lax`) plus CSRF protection |

- **Secret Rotation:** Store signing keys, encryption keys, and database credentials in a dedicated secrets manager:
  1. Generate a new cryptographically random secret with a distinct key identifier (`kid`).
  2. Deploy verification support for both old and new keys.
  3. Switch signing/issuance to the new key.
  4. Retain the old verification key only until existing tokens expire, then revoke it (or revoke immediately if compromise is suspected) and audit the rotation event.

### 7.6 Audit Logging Rules

Record security-relevant events (`REGISTER`, `LOGIN`, `UPDATE_USER`, `DELETE_USER`, administrative reads, and permission denials) with actor, action, target, outcome, and UTC timestamp:

```json
{
  "event": "UPDATE_USER",
  "actor_id": 101,
  "target_user_id": 202,
  "outcome": "DENIED",
  "timestamp": "2026-11-21T15:30:00Z"
}
```

**Never log:** plaintext passwords, password hashes, access tokens, refresh tokens, session cookies, API keys, or database credentials. Export production logs to restricted, append-only centralised storage.

### 7.7 Database Constraints & ACID Transactions

| Constraint / Property | Security & Integrity Benefit |
|---|---|
| `PRIMARY KEY` | Guarantees unique, non-null user identifiers |
| `UNIQUE` (on `email`) | Prevents duplicate account registration even under concurrent requests |
| `NOT NULL` | Ensures mandatory credentials and metadata cannot be omitted |
| `CHECK(is_admin IN (0, 1))` | Rejects invalid privilege values at the storage layer |
| `DEFAULT 0` | Ensures new accounts default to unprivileged status |
| `FOREIGN KEY` | Preserves referential integrity between `audit_logs` and `users` |
| **ACID Transactions** | Ensures user mutations and `audit_logs` inserts succeed or roll back atomically |

---

## 8. Threat Model

### 8.1 Assets
1. User accounts and profile ownership
2. Password hashes and cryptographic salts
3. User email addresses (PII)
4. Administrative privileges (`is_admin`)
5. Sessions and authentication tokens
6. Database records and audit log integrity

### 8.2 Trust Boundaries
- **Untrusted Client $\rightarrow$ API Layer:** All inputs, query parameters, payloads, and target IDs are untrusted and require strict validation.
- **API Layer $\rightarrow$ Application Service:** Authentication middleware must validate sessions/tokens and construct an immutable `Principal` before invoking service operations.
- **Application Service $\rightarrow$ Database:** Parameterised SQL, schema constraints, and ACID transactions enforce data integrity and prevent injection.
- **Application Service $\rightarrow$ Audit/Logging Sink:** Security events are persisted without exposing secrets.

### 8.3 Attacker Goals and Mitigations

| Attacker Goal | Threat Vector | Mitigation |
|---|---|---|
| Bypass authentication | SQL injection in login email field | Parameterised SQL queries (`?` placeholders) |
| Recover user passwords | Offline cracking of leaked MD5 hashes | Salted memory-hard `scrypt` (`N=2**15, r=8, p=1`) + `hmac.compare_digest` |
| Modify or delete victim's account | IDOR / horizontal privilege escalation | Server-side `require_owner_or_admin` check using `Principal` |
| Escalate to administrator | Mass assignment (`{"is_admin": 1}`) or unguarded endpoints | Strict field allow-list + DB `DEFAULT 0`/`CHECK` + `require_admin` |
| Guess user credentials | High-rate brute force / credential stuffing | Shared IP + account rate limiting and progressive delays |
| Enumerate registered emails | Distinct error messages or timing side channels | Generic errors + startup `DUMMY_PASSWORD_HASH` verification |
| Disrupt availability | Oversized inputs or connection exhaustion | Input length bounds (`12..1024`) + `get_db()` connection cleanup |
| Conceal malicious activity | Missing logs or log tampering | Transactional `audit_logs` + restricted append-only log sink |

---

## 9. Limitations & Production Improvements

1. **Database Concurrency (SQLite $\rightarrow$ PostgreSQL):** SQLite is used for self-contained local testing. Migrate to **PostgreSQL** with connection pooling for multi-instance production write concurrency.
2. **Password Hashing Upgrade (`scrypt` $\rightarrow$ `Argon2id`):** `scrypt` is used via the Python standard library for zero-dependency portability; use **Argon2id** (`argon2-cffi`) tuned to production hardware benchmarks for new deployments.
3. **Distributed Rate Limiting & Session Layer:** Add Redis-backed rate limiting, HTTP/TLS transport security, `HttpOnly`/`Secure`/`SameSite` cookies or short-lived JWTs with refresh rotation, and CSRF protection.
4. **Multi-Factor Authentication (MFA):** Enforce MFA for all administrative accounts and sensitive account-recovery flows.
5. **TOCTOU Race Hardening:** Combine authorisation checks and state mutations into atomic conditional database statements where concurrent role revocation is a concern.
6. **Centralised Observability & Secrets Management:** Stream audit logs to tamper-evident central monitoring with alerting on anomalous login failures, and store all keys/credentials in a secrets manager with automated rotation.

---

## 10. Conclusion

The security review identified critical vulnerabilities in the original service, notably SQL injection, unsalted MD5 password hashing, and missing object-level and administrative authorisation. The remediated implementation addresses these flaws through parameterised queries, salted `scrypt` hashing, server-side ownership and role verification, database constraints, ACID transactions, secret-free audit logging, and automated `pytest` regression tests. Together, these layered controls establish defence in depth while keeping testing strictly isolated to local databases and synthetic accounts.