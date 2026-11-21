# Section C – Security Review and Secure Remediation

**Hackathon Technical Selection – Extreme Challenge Pack**  
**Section:** C – Security Review and Remediation (10 Marks)  
**Candidate ID:** 16464
**Programming Language:** Python 3.10+  
**Database:** SQLite (local synthetic testing only)  
**Suggested Assessment Duration:** 45 minutes  

---

## 1. Executive Summary

The sample user-management code has serious security flaws and isn't safe for production. While reviewing it, we found SQL injection in `login`, weak unsalted MD5 password hashing, missing ownership and admin checks, no input validation, no rate limiting, database connections that aren't closed, positional insert assumptions, weak transaction and error handling, missing `rowcount` checks, possible account enumeration, and no audit logging.

In our fix (`secure_service.py`), we switched to parameterised queries, salted `scrypt` password hashing, server-side ownership and role checks, database constraints, proper transactions with rollback, custom domain errors, and audit logging without secrets. We also wrote a `pytest` suite (`test_secure_service.py`) that runs against temporary local SQLite databases and synthetic test users.

This is only a local security fix rather than a full production auth system. A real production app would still need a web framework with proper session/token handling, distributed rate limiting, a secrets manager, centralised logs, and a database like PostgreSQL if multiple instances write at once.

---

## 2. Repository Structure & Execution Commands

### Files
- `secure_service.py` – Fixed user-management module (`init_db`, `register`, `login`, `update_user`, `delete_user`, `list_users`).
- `test_secure_service.py` – `pytest` regression tests plus a local test showing the original SQL injection bug.
- `README.md` – Security findings, threat model, explanations, and run instructions.

### Setup & Run Commands

```bash
# 1. Install pytest
python -m pip install pytest

# 2. Run the test suite
python -m pytest -v

# 3. Run the service module directly (creates the startup dummy hash)
python secure_service.py
```

*Controlled Testing Policy: Everything here uses local temporary databases (`tmp_path` or `:memory:`) and fake/synthetic test accounts only. Don't run this against real users, public sites, or unauthorised systems.*

---

## 3. Scope and Assumptions

- Testing only uses local SQLite databases and synthetic accounts.
- Any data, IDs, or roles coming from the client are treated as untrusted.
- `Principal` should only be built on the server after a session or token is verified—clients should never be able to pass or forge a trusted `Principal`.
- New registrations always start as regular users (`is_admin = 0`).
- Admin privileges are assigned out-of-band by a protected process, never through sign-up or profile updates.
- SQLite is fine for a self-contained local demo, though I wouldn't pick it for a high-write, multi-instance production API.

---

## 4. Vulnerability Report (Ranked by Severity)

### 4.1 Severity Ranking

| ID | Finding | Severity | Primary Impact |
|---|---|---|---|
| **V1** | SQL injection in `login` query | **Critical** | Authentication bypass or unauthorised data access |
| **V2** | Unsalted MD5 used for password storage | **Critical** | Fast offline password cracking and rainbow-table lookups if the DB leaks |
| **V3** | Missing object-level authorisation in `update_user` / `delete_user` | **Critical** | Horizontal privilege escalation (IDOR) |
| **V4** | Missing admin checks & mass-assignment protection | **High** | Vertical privilege escalation |
| **V5** | No login rate limiting | **High** | Brute-force guessing, credential stuffing, and extra server load |
| **V6** | Missing input validation, normalisation, and length limits | **Medium** | Bad data, duplicate case-variant emails, or oversized inputs |
| **V7** | Database connections not explicitly closed | **Medium** | Leaked connections/file descriptors over repeated calls |
| **V8** | Positional `INSERT INTO users VALUES (?, ?, 0)` | **Medium** | Fragile queries if table columns change or get reordered |
| **V9** | Incomplete transaction and exception handling | **Medium** | Partial updates on failure and raw DB error leaks |
| **V10** | No audit logging | **Medium** | Hard to investigate suspicious activity or trace actions |
| **V11** | No `rowcount` check on `UPDATE` / `DELETE` | **Medium** | Returns `True` even when no row was actually updated or deleted |
| **V12** | Response/timing differences during login | **Medium** | Attackers can figure out which emails exist (account enumeration) |

*(Note: These severities assume an untrusted client can reach the service. Real-world risk depends on how it's exposed, DB permissions, and other layers around it.)*

### 4.2 Detailed Findings, Triggers, Impacts & Fixes

#### V1 – SQL Injection in `login`
- **Trigger:** `login()` builds the SQL string by pasting `email` straight into it (`WHERE email = '" + email + "'...`), so special characters change the query structure.
- **Impact:** Someone can pass something like `' OR '1'='1' --` to bypass the password check, sign in as another account, or read extra data depending on the query and driver.
- **Fix:** Use `?` placeholders and pass values in a tuple instead:
  ```python
  user = db.execute(
      "SELECT id, password_hash FROM users WHERE email = ?",
      (email,),
  ).fetchone()
  ```
  Parameterised queries keep user input as data rather than SQL syntax, though they don't replace authorisation checks.

#### V2 – Weak Password Hashing (Unsalted MD5)
- **Trigger:** Someone gets hold of the database file, a backup, or reads `password_hash` values.
- **Impact:** MD5 is built to be fast, not hard to crack, so GPUs can test guesses quickly. Because there is no per-user salt, identical passwords give identical hashes and rainbow tables can be used.
- **Fix:** Use a password hashing algorithm made for passwords (`Argon2id`, `scrypt`, or `bcrypt`). Here I used Python's `hashlib.scrypt` (`SCRYPT_N = 2**15`, `SCRYPT_R = 8`, `SCRYPT_P = 1`, `SCRYPT_LENGTH = 32`, `SCRYPT_MAXMEM = 64 * 1024 * 1024`), a fresh 16-byte salt from `secrets.token_bytes(16)` per password, parameter checks during verification, and `hmac.compare_digest`. For a new production system, `Argon2id` from a maintained library is a better default.

#### V3 – Missing Object-Level Authorisation (IDOR)
- **Trigger:** A caller passes a different `user_id` into `update_user` or `delete_user`.
- **Impact:** Horizontal privilege escalation—one user can edit or delete someone else's account.
- **Fix:** Require a server-created `Principal` and call `require_owner_or_admin(principal, target_user_id)` before changing anything. Never trust a `user_id` sent by the client as proof of who the caller is.

#### V4 – Missing Administrative Permission Enforcement & Mass Assignment
- **Trigger:** A normal user calls an admin function or includes extra fields like `is_admin = 1` in an update dictionary.
- **Impact:** Vertical privilege escalation or unauthorised role changes.
- **Fix:** Check `require_admin(principal)` for admin-only actions (`list_users`), look up the current `is_admin` value from the database rather than trusting a client flag, and only allow `{"email"}` in `update_user`.

#### V5 – No Login Rate Limiting
- **Trigger:** Lots of repeated login attempts hit the service quickly.
- **Impact:** Brute-force attacks, credential stuffing, and heavy request load.
- **Fix:** Add rate limits per account and per IP, along with backoff delays, in a shared store like Redis (not just in-memory per process) when running multiple instances.

#### V6 – Missing Input Validation and Normalisation
- **Trigger:** Inputs aren't strings, have extra spaces or uppercase letters, are formatted wrong, or are way too long.
- **Impact:** Duplicate-looking accounts (`Alice@example.com` vs `alice@example.com`), unexpected crashes, or huge inputs wasting resources.
- **Fix:** Check types with `isinstance(..., str)`, trim and lowercase emails, enforce length limits (`3..254` for email, `12..1024` for password), and do a basic email regex check (`[^@\s]+@[^@\s]+\.[^@\s]+`).

#### V7 – Database Connections Not Explicitly Closed
- **Trigger:** Functions like `login`, `register`, `update_user`, or `delete_user` are called repeatedly.
- **Impact:** Connections and file descriptors stay open longer than needed.
- **Fix:** Use `get_db()` as a context manager that commits on success, rolls back on errors, and always closes `db` in `finally`. (SQLite's built-in connection `with` block handles transactions, not closing the connection.)

#### V8 – Positional Schema Assumptions
- **Trigger:** `INSERT INTO users VALUES (?, ?, 0)` assumes exact column order and count.
- **Impact:** Adding or reordering columns in `users` can break inserts or put values in the wrong columns.
- **Fix:** Name the columns explicitly (`INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, 0)`).

#### V9 – Incomplete Transaction and Exception Handling
- **Trigger:** Something fails partway through a write or violates a database rule.
- **Impact:** Half-finished updates or raw SQLite error messages leaking out.
- **Fix:** Keep related writes and audit inserts inside one `get_db()` transaction so everything rolls back together on error, and raise clear exceptions (`ValidationError`, `SecurityError`, `NotFoundError`, `ConflictError`).

#### V10 – Missing Audit Logging
- **Trigger:** Events like registration, login, failed login, update, delete, or listing users happen.
- **Impact:** No trail to check what happened or who did it during an incident.
- **Fix:** Insert a row into `audit_logs` (`actor_id`, `action`, `target_user_id`, `outcome`, `created_at`) inside the same transaction, without ever logging passwords, hashes, tokens, cookies, or secrets.

#### V11 – False Success Reporting
- **Trigger:** `update_user` or `delete_user` is called with a `target_user_id` that isn't in `users`.
- **Impact:** The original code still returns `True` even though zero rows were touched.
- **Fix:** Check `if cursor.rowcount == 0:` and raise `NotFoundError("User not found")`.

#### V12 – Account Enumeration and Timing Differences
- **Trigger:** Login or registration behaves differently or takes noticeably different time when an email already exists vs. when it doesn't.
- **Impact:** Attackers can test emails to see which ones have accounts.
- **Fix:** Keep failure responses generic and run `verify_password(password, DUMMY_PASSWORD_HASH)` using a dummy hash created once at startup when `user is None`, cutting down one timing clue. *(Here, `audit_logs` is written when a known `actor_id` is involved, while unknown-email logins write a generic `logging.warning` instead of adding extra rows to `audit_logs`.)*

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
1. `DUMMY_PASSWORD_HASH` is generated once when the module starts up, not on every failed login for an unknown email.
2. `Principal` just holds the authenticated `id`; in a real web API it should come from a verified session or token.
3. Permission checks read `is_admin` from the database when called rather than trusting anything passed by a client.
4. Where high concurrency matters, combining the check and update into a single atomic SQL statement avoids race conditions between checking and modifying data.
5. `executescript()` commits automatically in SQLite, so real apps should handle schema changes with proper migration steps.
6. A web layer around these functions should turn the domain exceptions into appropriate HTTP responses without leaking internals.

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

Each control addresses a specific kind of bug—none of them is a silver bullet on its own:

| Control | Problem Class Solved | What It Does *Not* Solve |
|---|---|---|
| **Parameterised Queries** | Stops untrusted input from changing SQL structure (SQL injection). | Doesn't check if a logged-in user is allowed to touch another user's record. |
| **Password Hashing (`scrypt` / Argon2id)** | Makes offline cracking of leaked hashes much harder. | Doesn't stop online password guessing if there's no rate limit. |
| **Least Privilege & Object-Level Auth** | Prevents horizontal (IDOR) and vertical (role) privilege escalation. | Won't save a query that concatenates raw strings into SQL. |
| **Database Constraints & Transactions** | Enforces rules (`UNIQUE`, `CHECK`, `FOREIGN KEY`, `NOT NULL`) and all-or-nothing writes in the DB. | Can't check session ownership or application permissions by itself. |
| **Secure Error Handling** | Avoids leaking internal errors/stack traces and reduces account enumeration. | Doesn't enforce access permissions on its own. |
| **Audit Trails** | Helps track actions and investigate incidents after the fact. | Logs activity rather than blocking an attack in real time. |

### 7.2 Authentication vs. Authorisation

| Aspect | Authentication ("Who are you?") | Authorisation ("What are you permitted to access?") |
|---|---|---|
| **Purpose** | Confirms who the user claims to be | Checks what that user is allowed to do |
| **Mechanisms** | Passwords, MFA, passkeys, tokens | Ownership checks, roles, policies, least privilege |
| **Output** | Trusted server-side `Principal` | Allowed or rejected (`SecurityError`) |
| **Lifecycle** | Happens at sign-in / session check | Checked on every protected action |

### 7.3 Preventing Horizontal and Vertical Privilege Escalation

- **Horizontal Privilege Escalation (IDOR):** Happens when user `101` changes a target ID to user `202` to edit or delete someone else's account.
  - *Prevention:* Tie identity to a server-side `Principal`, check `principal.id == target_user_id` (or active admin role) on the server before updating/deleting, never treat a client ID as identity proof, and log blocked attempts.
- **Vertical Privilege Escalation:** Happens when a standard user gets admin access, such as sending `{"is_admin": 1}` in an update request.
  - *Prevention:* Never accept role fields from registration or profile updates, use a strict allow-list (`set(request) != {"email"}`), check `is_admin` from the DB on privileged actions, keep privileges minimal, and log admin actions.

### 7.4 Password Storage Design

Passwords should never be stored as plain text or reversible encryption—they should be hashed one-way with a dedicated password-hashing algorithm (`Argon2id`, `scrypt`, or `bcrypt`):
1. Pick a random 16-byte salt for each user (`secrets.token_bytes(16)`).
2. Hash with `hashlib.scrypt` using explicit settings (`N=2**15, r=8, p=1, dklen=32, maxmem=64 MiB`).
3. Store the algorithm name, parameters, salt, and derived hash together (`scrypt$N$r$p$salt$hash`).
4. When verifying on login, check that the stored parameters match expected settings (so nobody can force huge parameters or downgrade them), recompute with the stored salt, and compare using `hmac.compare_digest`.

### 7.5 Rate Limiting, Account Lockout, Session Expiry & Secret Rotation

- **Rate Limiting:** Limit repeated login attempts per account and per IP (for example, 5 failures in 5 minutes) using a shared store like Redis across instances. Using both account and IP limits helps against both single-target brute force and distributed credential stuffing.
- **Account Lockout Trade-Offs:** Permanent lockouts can backfire because an attacker can lock out normal users on purpose. Short temporary lockouts, progressive delays, CAPTCHAs/step-up checks, and alerts to the user are usually safer.
- **Session and Token Expiry:**

| Credential Type | Typical Lifetime & Controls |
|---|---|
| **Access Token** | Short-lived (around 5–15 minutes) |
| **Refresh Token** | Longer-lived (several days) with rotation |
| **Server-Side Session** | Idle timeout plus max lifetime, cleared on logout or password change |
| **Browser Cookies** | `HttpOnly`, `Secure`, and `SameSite` attributes, plus CSRF protection |

- **Secret Rotation:** Keep signing keys, DB credentials, and secrets in a secrets manager:
  1. Generate a new random secret/key (with a key ID if signing tokens).
  2. Roll out support for verifying with both old and new keys.
  3. Switch signing/usage to the new key.
  4. Keep the old verification key only until active tokens expire, then remove it (or revoke right away if leaked), and log the rotation.

### 7.6 Audit Logging Rules

Log security events like `REGISTER`, `LOGIN`, `UPDATE_USER`, `DELETE_USER`, admin reads, and denied actions with who acted, what happened, the target, the outcome, and a timestamp:

```json
{
  "event": "UPDATE_USER",
  "actor_id": 101,
  "target_user_id": 202,
  "outcome": "DENIED",
  "timestamp": "2026-11-21T15:30:00Z"
}
```

**Never log:** plaintext passwords, password hashes, access/refresh tokens, session cookies, API keys, or DB passwords. In production, send logs to restricted, tamper-resistant storage.

### 7.7 Database Constraints & ACID Transactions

| Constraint / Property | What It Helps With |
|---|---|
| `PRIMARY KEY` | Keeps user IDs unique |
| `UNIQUE` (on `email`) | Stops duplicate emails even under race conditions |
| `NOT NULL` | Makes sure required fields aren't missing |
| `CHECK(is_admin IN (0, 1))` | Blocks invalid `is_admin` values in the database |
| `DEFAULT 0` | Starts new users as non-admins by default |
| `FOREIGN KEY` | Keeps `audit_logs.actor_id` consistent with `users.id` |
| **ACID Transactions** | Makes sure DB updates and audit rows commit together or roll back on error |

---

## 8. Threat Model

### 8.1 Assets
1. User accounts and who owns them
2. Password hashes and salts
3. User email addresses
4. Admin roles (`is_admin`)
5. Sessions and auth tokens
6. Database data and audit logs

### 8.2 Trust Boundaries
- **Client $\rightarrow$ API:** Everything coming from the client is untrusted and needs validation.
- **API $\rightarrow$ Service:** Auth/session checks should create a trusted `Principal` before calling service functions.
- **Service $\rightarrow$ Database:** Parameterised SQL, constraints, and transactions protect stored records.
- **Service $\rightarrow$ Logs:** Audit/log entries record events without leaking secrets.

### 8.3 Attacker Goals and Mitigations

| Attacker Goal | Threat | Mitigation |
|---|---|---|
| Bypass login | SQL injection in `email` | Parameterised `?` queries |
| Crack stolen password hashes | Fast unsalted MD5 hashes | Salted `scrypt` (`N=2**15, r=8, p=1`) + `hmac.compare_digest` |
| Edit/delete another user's account | IDOR / horizontal escalation | Server-side `require_owner_or_admin` check with `Principal` |
| Become an admin | Mass assignment (`{"is_admin": 1}`) or missing role check | Allow-list (`{"email"}`), DB `DEFAULT 0`/`CHECK`, and `require_admin` |
| Guess passwords repeatedly | Brute force / credential stuffing | Rate limiting by account and IP + progressive backoff |
| Find valid user emails | Different error messages or timing | Generic responses + startup `DUMMY_PASSWORD_HASH` check |
| Overload or crash the service | Huge inputs or unclosed DB connections | Length limits (`12..1024`) + `get_db()` cleanup |
| Hide tracks | Missing or altered logs | Transactional `audit_logs` + secret-free logging |

---

## 9. Limitations & Production Improvements

1. **SQLite $\rightarrow$ PostgreSQL:** SQLite works well for local tests, but **PostgreSQL** is much better for multi-instance production apps with heavy writes.
2. **`scrypt` $\rightarrow$ `Argon2id`:** `scrypt` works out of the box with Python's standard library, while **Argon2id** (`argon2-cffi`) is a great choice for new production deployments once benchmarked on target hardware.
3. **Rate Limiting, Sessions & Transport:** Add Redis rate limiting, HTTPS/TLS, `HttpOnly`/`Secure`/`SameSite` cookies or short-lived tokens with refresh rotation, and CSRF protection.
4. **MFA:** Require multi-factor authentication for admin accounts.
5. **Race Conditions (TOCTOU):** For heavy concurrent updates, combine permission checks and mutations into atomic conditional queries so roles can't change mid-check.
6. **Centralised Monitoring & Secrets:** Ship logs to a protected central store with alerts, and manage keys/secrets in a secrets manager with rotation.

---

## 10. Conclusion

The original code had major issues—especially SQL injection, unsalted MD5 password hashes, and missing ownership and admin checks. The updated `secure_service.py` fixes these using parameterised queries, salted `scrypt`, server-side permission checks, DB constraints, transactions, secret-free audit logging, and `pytest` tests. Using these controls together gives defence in depth, while keeping all testing strictly on local synthetic data.
