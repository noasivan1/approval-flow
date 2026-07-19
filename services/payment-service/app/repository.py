import json
import os
import threading
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

_PAYMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    tracking_id TEXT NOT NULL UNIQUE,
    department TEXT,
    amount_usd NUMERIC NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    invoice_number TEXT,
    status TEXT NOT NULL,
    failure_reason TEXT,
    correlation_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    executed_at TEXT,
    released_at TEXT,
    result_published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_BUDGETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_accounts (
    budget_id TEXT PRIMARY KEY,
    department TEXT NOT NULL,
    currency TEXT NOT NULL,
    total NUMERIC NOT NULL CHECK (total >= 0),
    reserved NUMERIC NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    spent NUMERIC NOT NULL DEFAULT 0 CHECK (spent >= 0),
    updated_at TEXT NOT NULL,
    UNIQUE (department, currency),
    CHECK (reserved + spent <= total)
)
"""

_MIGRATIONS = (
    "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments (status)",
)

_PAYMENT_COLUMNS = (
    "payment_id",
    "tracking_id",
    "department",
    "amount_usd",
    "currency",
    "invoice_number",
    "status",
    "failure_reason",
    "correlation_id",
    "requested_at",
    "executed_at",
    "released_at",
    "result_published_at",
    "created_at",
    "updated_at",
)

_SELECT = ", ".join(_PAYMENT_COLUMNS)


def money(value) -> Decimal:
    """Quantize to cents; budget arithmetic never touches binary floats."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _row_to_payment(row) -> dict:
    return dict(zip(_PAYMENT_COLUMNS, row))


class InMemoryPaymentRepository:
    """In-memory store, used when no DATABASE_URL is set."""

    def __init__(self) -> None:
        self._payments: dict[str, dict] = {}  # keyed by tracking_id
        self._budgets: dict[str, dict] = {}  # keyed by (department, currency)
        self._lock = threading.Lock()

    def seed_budgets(self, accounts: list[dict]) -> None:
        with self._lock:
            for account in accounts:
                key = f"{account['department']}|{account['currency'].upper()}"
                if key not in self._budgets:
                    self._budgets[key] = {
                        "budget_id": account["budget_id"],
                        "department": account["department"],
                        "currency": account["currency"].upper(),
                        "total": money(account["total"]),
                        "reserved": Decimal("0.00"),
                        "spent": Decimal("0.00"),
                    }

    def get_budget(self, department: str, currency: str = "USD") -> dict | None:
        with self._lock:
            budget = self._budgets.get(f"{department}|{currency.upper()}")
            return dict(budget) if budget else None

    def create_payment(self, payment: dict, precheck_failure: str | None):
        with self._lock:
            existing = self._payments.get(payment["tracking_id"])
            if existing is not None:
                return "exists", dict(existing)
            record = dict(payment)
            if precheck_failure:
                record["status"] = "failed"
                record["failure_reason"] = precheck_failure
            else:
                budget = self._budgets.get(f"{record['department']}|USD")
                amount = record["amount_usd"]
                if budget is None:
                    record["status"] = "failed"
                    record["failure_reason"] = "budget_not_found"
                elif budget["total"] - budget["reserved"] - budget["spent"] >= amount:
                    budget["reserved"] += amount
                    record["status"] = "reserved"
                else:
                    record["status"] = "failed"
                    record["failure_reason"] = "insufficient_budget"
            self._payments[record["tracking_id"]] = record
            return "created", dict(record)

    def get_payment(self, tracking_id: str) -> dict | None:
        with self._lock:
            payment = self._payments.get(tracking_id)
            return dict(payment) if payment else None

    def commit_paid(self, payment_id: str, executed_at: str) -> bool:
        with self._lock:
            payment = self._find_by_id(payment_id)
            if payment is None or payment["status"] != "reserved":
                return False
            payment["status"] = "paid"
            payment["executed_at"] = executed_at
            payment["updated_at"] = executed_at
            budget = self._budgets.get(f"{payment['department']}|USD")
            budget["reserved"] -= payment["amount_usd"]
            budget["spent"] += payment["amount_usd"]
            return True

    def fail_reserved(self, payment_id: str, reason: str, released_at: str) -> bool:
        with self._lock:
            payment = self._find_by_id(payment_id)
            if payment is None or payment["status"] != "reserved":
                return False
            payment["status"] = "failed"
            payment["failure_reason"] = reason
            payment["released_at"] = released_at
            payment["updated_at"] = released_at
            budget = self._budgets.get(f"{payment['department']}|USD")
            budget["reserved"] -= payment["amount_usd"]
            return True

    def mark_result_published(self, payment_id: str, published_at: str) -> None:
        with self._lock:
            payment = self._find_by_id(payment_id)
            if payment is not None and payment.get("result_published_at") is None:
                payment["result_published_at"] = published_at

    def find_reserved(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._payments.values() if p["status"] == "reserved"]

    def find_unpublished_results(self) -> list[dict]:
        with self._lock:
            return [
                dict(p)
                for p in self._payments.values()
                if p["status"] in ("paid", "failed") and p.get("result_published_at") is None
            ]

    def _find_by_id(self, payment_id: str) -> dict | None:
        for payment in self._payments.values():
            if payment["payment_id"] == payment_id:
                return payment
        return None


class PostgresPaymentRepository:
    """PostgreSQL store, used when DATABASE_URL is set.

    Owns the payments and budget_accounts tables. Money moves only through
    guarded conditional updates.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._table_ready = False
        self._lock = threading.Lock()

    def _connect(self):
        # lazy import: the driver is only needed when Postgres is configured
        import psycopg

        return psycopg.connect(self._dsn)

    def _ensure_table(self) -> None:
        with self._lock:
            if self._table_ready:
                return
            with self._connect() as conn:
                conn.execute(_PAYMENTS_SCHEMA)
                conn.execute(_BUDGETS_SCHEMA)
                for migration in _MIGRATIONS:
                    conn.execute(migration)
            self._table_ready = True

    def seed_budgets(self, accounts: list[dict]) -> None:
        self._ensure_table()
        with self._connect() as conn:
            for account in accounts:
                conn.execute(
                    "INSERT INTO budget_accounts (budget_id, department, currency, total, reserved, spent, updated_at) "
                    "VALUES (%s, %s, %s, %s, 0, 0, %s) ON CONFLICT (budget_id) DO NOTHING",
                    (
                        account["budget_id"],
                        account["department"],
                        account["currency"].upper(),
                        money(account["total"]),
                        account.get("updated_at", "1970-01-01T00:00:00+00:00"),
                    ),
                )

    def get_budget(self, department: str, currency: str = "USD") -> dict | None:
        self._ensure_table()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT budget_id, department, currency, total, reserved, spent "
                "FROM budget_accounts WHERE department = %s AND currency = %s",
                (department, currency.upper()),
            ).fetchone()
        if row is None:
            return None
        keys = ("budget_id", "department", "currency", "total", "reserved", "spent")
        return dict(zip(keys, row))

    def create_payment(self, payment: dict, precheck_failure: str | None):
        self._ensure_table()
        record = dict(payment)
        with self._connect() as conn:
            columns = ", ".join(_PAYMENT_COLUMNS)
            placeholders = ", ".join(f"%({column})s" for column in _PAYMENT_COLUMNS)
            cursor = conn.execute(
                f"INSERT INTO payments ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT (tracking_id) DO NOTHING",
                {**record, "status": "reserved"},
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    f"SELECT {_SELECT} FROM payments WHERE tracking_id = %s",
                    (record["tracking_id"],),
                ).fetchone()
                return "exists", _row_to_payment(row)

            failure = precheck_failure
            if failure is None:
                reserved = conn.execute(
                    "UPDATE budget_accounts SET reserved = reserved + %s, updated_at = %s "
                    "WHERE department = %s AND currency = 'USD' "
                    "AND total - reserved - spent >= %s",
                    (record["amount_usd"], record["updated_at"], record["department"], record["amount_usd"]),
                ).rowcount
                if reserved == 0:
                    exists = conn.execute(
                        "SELECT 1 FROM budget_accounts WHERE department = %s AND currency = 'USD'",
                        (record["department"],),
                    ).fetchone()
                    failure = "insufficient_budget" if exists else "budget_not_found"

            if failure:
                conn.execute(
                    "UPDATE payments SET status = 'failed', failure_reason = %s WHERE payment_id = %s",
                    (failure, record["payment_id"]),
                )
                record["status"] = "failed"
                record["failure_reason"] = failure
            else:
                record["status"] = "reserved"
            return "created", record

    def get_payment(self, tracking_id: str) -> dict | None:
        self._ensure_table()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_SELECT} FROM payments WHERE tracking_id = %s", (tracking_id,)
            ).fetchone()
        return _row_to_payment(row) if row else None

    def commit_paid(self, payment_id: str, executed_at: str) -> bool:
        self._ensure_table()
        with self._connect() as conn:
            # the status guard makes this transition one-shot; the budget move
            # rides the same transaction, so money can never move twice
            row = conn.execute(
                "UPDATE payments SET status = 'paid', executed_at = %s, updated_at = %s "
                "WHERE payment_id = %s AND status = 'reserved' RETURNING department, amount_usd",
                (executed_at, executed_at, payment_id),
            ).fetchone()
            if row is None:
                return False
            department, amount = row
            conn.execute(
                "UPDATE budget_accounts SET reserved = reserved - %s, spent = spent + %s, updated_at = %s "
                "WHERE department = %s AND currency = 'USD'",
                (amount, amount, executed_at, department),
            )
            return True

    def fail_reserved(self, payment_id: str, reason: str, released_at: str) -> bool:
        self._ensure_table()
        with self._connect() as conn:
            # same one-shot guard: the reservation is released exactly once
            row = conn.execute(
                "UPDATE payments SET status = 'failed', failure_reason = %s, released_at = %s, updated_at = %s "
                "WHERE payment_id = %s AND status = 'reserved' RETURNING department, amount_usd",
                (reason, released_at, released_at, payment_id),
            ).fetchone()
            if row is None:
                return False
            department, amount = row
            conn.execute(
                "UPDATE budget_accounts SET reserved = reserved - %s, updated_at = %s "
                "WHERE department = %s AND currency = 'USD'",
                (amount, released_at, department),
            )
            return True

    def mark_result_published(self, payment_id: str, published_at: str) -> None:
        self._ensure_table()
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET result_published_at = %s "
                "WHERE payment_id = %s AND result_published_at IS NULL",
                (published_at, payment_id),
            )

    def find_reserved(self) -> list[dict]:
        self._ensure_table()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT} FROM payments WHERE status = 'reserved'"
            ).fetchall()
        return [_row_to_payment(row) for row in rows]

    def find_unpublished_results(self) -> list[dict]:
        self._ensure_table()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT} FROM payments "
                "WHERE status IN ('paid', 'failed') AND result_published_at IS NULL"
            ).fetchall()
        return [_row_to_payment(row) for row in rows]


def _budgets_path() -> str | None:
    env_path = os.getenv("BUDGETS_PATH")
    if env_path:
        return env_path
    # repo-relative fallback for bare local runs and tests; inside a container
    # the source sits too close to the filesystem root for this to exist
    parents = Path(__file__).resolve().parents
    if len(parents) >= 4:
        return str(parents[3] / "data" / "budgets.json")
    return None


def _load_budget_accounts() -> list[dict]:
    path = _budgets_path()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("accounts", [])
    except OSError:
        return []


_repository = None


def get_repository():
    global _repository
    if _repository is None:
        dsn = os.getenv("DATABASE_URL", "")
        repository = PostgresPaymentRepository(dsn) if dsn else InMemoryPaymentRepository()
        # seed before caching so a failed seed (database not up yet) is retried
        repository.seed_budgets(_load_budget_accounts())
        _repository = repository
    return _repository
