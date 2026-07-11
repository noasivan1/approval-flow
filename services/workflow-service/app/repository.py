import json
import os
import threading

_COLUMNS = (
    "tracking_id",
    "vendor",
    "amount",
    "currency",
    "category",
    "receipt_present",
    "submitter_email",
    "note",
    "status",
    "created_at",
    "updated_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    tracking_id TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    currency TEXT NOT NULL,
    category TEXT NOT NULL,
    receipt_present BOOLEAN NOT NULL,
    submitter_email TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# additive, safe to run against a table created before these columns existed
_MIGRATIONS = (
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS details JSONB",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS decision JSONB",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS published_at TEXT",
)


def _split(invoice: dict) -> tuple[dict, dict]:
    base = {column: invoice.get(column) for column in _COLUMNS}
    details = {
        key: value
        for key, value in invoice.items()
        if key not in _COLUMNS and key not in ("decision", "published_at")
    }
    return base, details


class InMemoryInvoiceRepository:
    """In-memory store, used when no DATABASE_URL is set."""

    def __init__(self) -> None:
        self._invoices: dict[str, dict] = {}
        self._lock = threading.Lock()

    def save(self, invoice: dict) -> None:
        with self._lock:
            record = dict(invoice)
            record.setdefault("decision", None)
            record.setdefault("published_at", None)
            self._invoices[invoice["tracking_id"]] = record

    def get(self, tracking_id: str) -> dict | None:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            return dict(invoice) if invoice else None

    def update_decision(self, tracking_id: str, status: str, decision: dict, updated_at: str) -> bool:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            if invoice is None or invoice["status"] != "received":
                return False
            invoice["status"] = status
            invoice["decision"] = dict(decision)
            invoice["updated_at"] = updated_at
            return True

    def mark_published(self, tracking_id: str, published_at: str) -> None:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            if invoice is not None and invoice.get("published_at") is None:
                invoice["published_at"] = published_at

    def find_unpublished(self, created_before: str) -> list[dict]:
        with self._lock:
            return [
                dict(invoice)
                for invoice in self._invoices.values()
                if invoice.get("published_at") is None and invoice["created_at"] < created_before
            ]


class PostgresInvoiceRepository:
    """PostgreSQL store, used when DATABASE_URL is set.

    Opens a connection per operation and creates/migrates the table on first use.
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
                conn.execute(_SCHEMA)
                for migration in _MIGRATIONS:
                    conn.execute(migration)
            self._table_ready = True

    def save(self, invoice: dict) -> None:
        self._ensure_table()
        base, details = _split(invoice)
        columns = ", ".join(_COLUMNS) + ", details"
        placeholders = ", ".join(f"%({column})s" for column in _COLUMNS) + ", %(details)s"
        query = f"INSERT INTO invoices ({columns}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(query, {**base, "details": json.dumps(details)})

    def get(self, tracking_id: str) -> dict | None:
        self._ensure_table()
        columns = ", ".join(_COLUMNS) + ", details, decision, published_at"
        query = f"SELECT {columns} FROM invoices WHERE tracking_id = %s"
        with self._connect() as conn:
            row = conn.execute(query, (tracking_id,)).fetchone()
        if row is None:
            return None
        base = dict(zip(_COLUMNS, row[: len(_COLUMNS)]))
        details, decision, published_at = row[len(_COLUMNS):]
        return {**base, **(details or {}), "decision": decision, "published_at": published_at}

    def update_decision(self, tracking_id: str, status: str, decision: dict, updated_at: str) -> bool:
        self._ensure_table()
        query = (
            "UPDATE invoices SET status = %s, decision = %s, updated_at = %s "
            "WHERE tracking_id = %s AND status = 'received'"
        )
        with self._connect() as conn:
            cursor = conn.execute(query, (status, json.dumps(decision), updated_at, tracking_id))
            return cursor.rowcount > 0

    def mark_published(self, tracking_id: str, published_at: str) -> None:
        self._ensure_table()
        query = "UPDATE invoices SET published_at = %s WHERE tracking_id = %s AND published_at IS NULL"
        with self._connect() as conn:
            conn.execute(query, (published_at, tracking_id))

    def find_unpublished(self, created_before: str) -> list[dict]:
        self._ensure_table()
        columns = ", ".join(_COLUMNS) + ", details, decision, published_at"
        query = f"SELECT {columns} FROM invoices WHERE published_at IS NULL AND created_at < %s"
        with self._connect() as conn:
            rows = conn.execute(query, (created_before,)).fetchall()
        results = []
        for row in rows:
            base = dict(zip(_COLUMNS, row[: len(_COLUMNS)]))
            details, decision, published_at = row[len(_COLUMNS):]
            results.append({**base, **(details or {}), "decision": decision, "published_at": published_at})
        return results


_repository = None


def get_repository():
    global _repository
    if _repository is None:
        dsn = os.getenv("DATABASE_URL", "")
        _repository = PostgresInvoiceRepository(dsn) if dsn else InMemoryInvoiceRepository()
    return _repository
