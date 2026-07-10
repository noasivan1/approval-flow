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


class InMemoryInvoiceRepository:
    """In-memory store, used when no DATABASE_URL is set."""

    def __init__(self) -> None:
        self._invoices: dict[str, dict] = {}
        self._lock = threading.Lock()

    def save(self, invoice: dict) -> None:
        with self._lock:
            self._invoices[invoice["tracking_id"]] = dict(invoice)

    def get(self, tracking_id: str) -> dict | None:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            return dict(invoice) if invoice else None


class PostgresInvoiceRepository:
    """PostgreSQL store, used when DATABASE_URL is set.

    Opens a connection per operation and creates the table on first use.
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
            self._table_ready = True

    def save(self, invoice: dict) -> None:
        self._ensure_table()
        placeholders = ", ".join(f"%({column})s" for column in _COLUMNS)
        query = f"INSERT INTO invoices ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(query, invoice)

    def get(self, tracking_id: str) -> dict | None:
        self._ensure_table()
        query = f"SELECT {', '.join(_COLUMNS)} FROM invoices WHERE tracking_id = %s"
        with self._connect() as conn:
            row = conn.execute(query, (tracking_id,)).fetchone()
        if row is None:
            return None
        return dict(zip(_COLUMNS, row))


_repository = None


def get_repository():
    global _repository
    if _repository is None:
        dsn = os.getenv("DATABASE_URL", "")
        _repository = PostgresInvoiceRepository(dsn) if dsn else InMemoryInvoiceRepository()
    return _repository
