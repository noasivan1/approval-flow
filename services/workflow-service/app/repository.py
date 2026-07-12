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

_EXTRA_COLUMNS = ("details", "decision", "published_at", "revision", "review_requested_at")

_BOOKKEEPING_KEYS = ("decision", "published_at", "revision", "review_requested_at")

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

_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_actions (
    action_id TEXT PRIMARY KEY,
    tracking_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action TEXT NOT NULL,
    reviewer_email TEXT NOT NULL,
    comment TEXT,
    idempotency_key TEXT NOT NULL,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tracking_id, idempotency_key)
)
"""

_REVIEW_COLUMNS = (
    "action_id",
    "tracking_id",
    "revision",
    "action",
    "reviewer_email",
    "comment",
    "idempotency_key",
    "correlation_id",
    "created_at",
)

# additive, safe to run against a table created before these columns existed
_MIGRATIONS = (
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS details JSONB",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS decision JSONB",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS published_at TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS revision INTEGER",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS review_requested_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices (status)",
)

_SELECT = ", ".join(_COLUMNS + _EXTRA_COLUMNS)


def _split(invoice: dict) -> tuple[dict, dict]:
    base = {column: invoice.get(column) for column in _COLUMNS}
    details = {
        key: value
        for key, value in invoice.items()
        if key not in _COLUMNS and key not in _BOOKKEEPING_KEYS
    }
    return base, details


def _row_to_invoice(row) -> dict:
    base = dict(zip(_COLUMNS, row))
    details, decision, published_at, revision, review_requested_at = row[len(_COLUMNS):]
    return {
        **base,
        **(details or {}),
        "decision": decision,
        "published_at": published_at,
        "revision": revision or 1,
        "review_requested_at": review_requested_at,
    }


class InMemoryInvoiceRepository:
    """In-memory store, used when no DATABASE_URL is set."""

    def __init__(self) -> None:
        self._invoices: dict[str, dict] = {}
        self._review_actions: list[dict] = []
        self._lock = threading.Lock()

    def save(self, invoice: dict) -> None:
        with self._lock:
            record = dict(invoice)
            record.setdefault("decision", None)
            record.setdefault("published_at", None)
            record.setdefault("revision", 1)
            record.setdefault("review_requested_at", None)
            self._invoices[invoice["tracking_id"]] = record

    def get(self, tracking_id: str) -> dict | None:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            return dict(invoice) if invoice else None

    def update_decision(
        self, tracking_id: str, status: str, decision: dict, updated_at: str, revision: int
    ) -> bool:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            if (
                invoice is None
                or invoice["status"] != "received"
                or invoice.get("revision", 1) != revision
            ):
                return False
            invoice["status"] = status
            invoice["decision"] = dict(decision)
            invoice["updated_at"] = updated_at
            if status == "pending_review":
                invoice["review_requested_at"] = updated_at
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

    def list_pending_review(self) -> list[dict]:
        with self._lock:
            items = [dict(i) for i in self._invoices.values() if i["status"] == "pending_review"]
            return sorted(items, key=lambda i: i.get("review_requested_at") or i["created_at"])

    def apply_review_action(self, record: dict, new_status: str, updated_at: str) -> str:
        with self._lock:
            for existing in self._review_actions:
                if (
                    existing["tracking_id"] == record["tracking_id"]
                    and existing["idempotency_key"] == record["idempotency_key"]
                ):
                    return "duplicate"
            invoice = self._invoices.get(record["tracking_id"])
            if invoice is None:
                return "not_found"
            if invoice["status"] != "pending_review":
                return "conflict"
            invoice["status"] = new_status
            invoice["updated_at"] = updated_at
            self._review_actions.append({**record, "revision": invoice.get("revision", 1)})
            return "applied"

    def list_review_actions(self, tracking_id: str) -> list[dict]:
        with self._lock:
            actions = [dict(a) for a in self._review_actions if a["tracking_id"] == tracking_id]
            return sorted(actions, key=lambda a: a["created_at"])

    def apply_additional_info(self, tracking_id: str, fields: dict, updated_at: str):
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            if invoice is None:
                return "not_found", None
            if invoice["status"] != "needs_more_info":
                return "conflict", None
            invoice.update(fields)
            invoice["revision"] = invoice.get("revision", 1) + 1
            invoice["status"] = "received"
            invoice["decision"] = None
            invoice["published_at"] = None
            invoice["updated_at"] = updated_at
            return "applied", dict(invoice)


class PostgresInvoiceRepository:
    """PostgreSQL store, used when DATABASE_URL is set.

    Opens a connection per operation and creates/migrates the tables on first
    use. Status transitions are guarded in SQL so concurrent actions cannot
    overwrite each other.
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
                conn.execute(_REVIEW_SCHEMA)
                for migration in _MIGRATIONS:
                    conn.execute(migration)
            self._table_ready = True

    def save(self, invoice: dict) -> None:
        self._ensure_table()
        base, details = _split(invoice)
        columns = ", ".join(_COLUMNS) + ", details, revision"
        placeholders = ", ".join(f"%({column})s" for column in _COLUMNS) + ", %(details)s, %(revision)s"
        query = f"INSERT INTO invoices ({columns}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(
                query,
                {**base, "details": json.dumps(details), "revision": invoice.get("revision", 1)},
            )

    def get(self, tracking_id: str) -> dict | None:
        self._ensure_table()
        query = f"SELECT {_SELECT} FROM invoices WHERE tracking_id = %s"
        with self._connect() as conn:
            row = conn.execute(query, (tracking_id,)).fetchone()
        return _row_to_invoice(row) if row else None

    def update_decision(
        self, tracking_id: str, status: str, decision: dict, updated_at: str, revision: int
    ) -> bool:
        self._ensure_table()
        query = (
            "UPDATE invoices SET status = %s, decision = %s, updated_at = %s, "
            "review_requested_at = CASE WHEN %s = 'pending_review' THEN %s ELSE review_requested_at END "
            "WHERE tracking_id = %s AND status = 'received' AND COALESCE(revision, 1) = %s"
        )
        with self._connect() as conn:
            cursor = conn.execute(
                query,
                (status, json.dumps(decision), updated_at, status, updated_at, tracking_id, revision),
            )
            return cursor.rowcount > 0

    def mark_published(self, tracking_id: str, published_at: str) -> None:
        self._ensure_table()
        query = "UPDATE invoices SET published_at = %s WHERE tracking_id = %s AND published_at IS NULL"
        with self._connect() as conn:
            conn.execute(query, (published_at, tracking_id))

    def find_unpublished(self, created_before: str) -> list[dict]:
        self._ensure_table()
        query = f"SELECT {_SELECT} FROM invoices WHERE published_at IS NULL AND created_at < %s"
        with self._connect() as conn:
            rows = conn.execute(query, (created_before,)).fetchall()
        return [_row_to_invoice(row) for row in rows]

    def list_pending_review(self) -> list[dict]:
        self._ensure_table()
        query = (
            f"SELECT {_SELECT} FROM invoices WHERE status = 'pending_review' "
            "ORDER BY COALESCE(review_requested_at, created_at)"
        )
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [_row_to_invoice(row) for row in rows]

    def apply_review_action(self, record: dict, new_status: str, updated_at: str) -> str:
        self._ensure_table()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM review_actions WHERE tracking_id = %s AND idempotency_key = %s",
                (record["tracking_id"], record["idempotency_key"]),
            ).fetchone()
            if existing:
                return "duplicate"
            row = conn.execute(
                "SELECT revision FROM invoices WHERE tracking_id = %s FOR UPDATE",
                (record["tracking_id"],),
            ).fetchone()
            if row is None:
                return "not_found"
            cursor = conn.execute(
                "UPDATE invoices SET status = %s, updated_at = %s "
                "WHERE tracking_id = %s AND status = 'pending_review'",
                (new_status, updated_at, record["tracking_id"]),
            )
            if cursor.rowcount == 0:
                return "conflict"
            columns = ", ".join(_REVIEW_COLUMNS)
            placeholders = ", ".join(f"%({column})s" for column in _REVIEW_COLUMNS)
            conn.execute(
                f"INSERT INTO review_actions ({columns}) VALUES ({placeholders})",
                {**record, "revision": row[0] or 1},
            )
            return "applied"

    def list_review_actions(self, tracking_id: str) -> list[dict]:
        self._ensure_table()
        columns = ", ".join(_REVIEW_COLUMNS)
        query = f"SELECT {columns} FROM review_actions WHERE tracking_id = %s ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(query, (tracking_id,)).fetchall()
        return [dict(zip(_REVIEW_COLUMNS, row)) for row in rows]

    def apply_additional_info(self, tracking_id: str, fields: dict, updated_at: str):
        self._ensure_table()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_SELECT} FROM invoices WHERE tracking_id = %s FOR UPDATE",
                (tracking_id,),
            ).fetchone()
            if row is None:
                return "not_found", None
            invoice = _row_to_invoice(row)
            if invoice["status"] != "needs_more_info":
                return "conflict", None
            invoice.update(fields)
            invoice["revision"] += 1
            invoice["status"] = "received"
            invoice["decision"] = None
            invoice["published_at"] = None
            invoice["updated_at"] = updated_at
            _, details = _split(invoice)
            conn.execute(
                "UPDATE invoices SET receipt_present = %s, note = %s, details = %s, revision = %s, "
                "status = 'received', decision = NULL, published_at = NULL, updated_at = %s "
                "WHERE tracking_id = %s",
                (
                    invoice["receipt_present"],
                    invoice["note"],
                    json.dumps(details),
                    invoice["revision"],
                    updated_at,
                    tracking_id,
                ),
            )
            return "applied", invoice


_repository = None


def get_repository():
    global _repository
    if _repository is None:
        dsn = os.getenv("DATABASE_URL", "")
        _repository = PostgresInvoiceRepository(dsn) if dsn else InMemoryInvoiceRepository()
    return _repository
