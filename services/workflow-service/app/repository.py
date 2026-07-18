import json
import os
import threading
from decimal import ROUND_HALF_UP, Decimal

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

_EXTRA_COLUMNS = (
    "details",
    "decision",
    "published_at",
    "revision",
    "review_requested_at",
    "duplicate_key",
    "duplicate_of",
    "payment",
    "payment_flow_version",
    "payment_requested_at",
    "payment_published_at",
)

_BOOKKEEPING_KEYS = (
    "decision",
    "published_at",
    "revision",
    "review_requested_at",
    "duplicate_key",
    "duplicate_of",
    "payment",
    "payment_flow_version",
    "payment_requested_at",
    "payment_published_at",
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
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS duplicate_key TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS duplicate_of TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment JSONB",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_flow_version INTEGER",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_requested_at TEXT",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_published_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices (status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_duplicate_key"
    " ON invoices (duplicate_key) WHERE duplicate_key IS NOT NULL",
)

_SELECT = ", ".join(_COLUMNS + _EXTRA_COLUMNS)

_INSERT_COLUMNS = _COLUMNS + ("details", "revision", "decision", "published_at", "duplicate_key", "duplicate_of")


def duplicate_key_for(invoice: dict) -> str | None:
    """Business duplicate identity: vendor + invoice_number + currency + amount.

    Without an invoice number there is no reliable identity, so no key.
    """
    invoice_number = (invoice.get("invoice_number") or "").strip()
    if not invoice_number:
        return None
    amount = Decimal(str(invoice["amount"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "|".join(
        [
            invoice["vendor"].strip().lower(),
            invoice_number.upper(),
            (invoice.get("currency") or "USD").strip().upper(),
            str(amount),
        ]
    )


def _duplicate_decision(original_tracking_id: str, evaluated_at: str) -> dict:
    # no recommendation/confidence: decision-service is never called for duplicates
    return {
        "route": "duplicate",
        "status": "duplicate",
        "rule_ids": ["GLOBAL-DUP"],
        "reasons": [
            {
                "rule_id": "GLOBAL-DUP",
                "code": "duplicate_invoice",
                "detail": f"same vendor, invoice number, currency, and amount as invoice {original_tracking_id}",
            }
        ],
        "plain_language_reason": (
            "Rejected as a duplicate: an invoice with the same vendor, invoice number, "
            "currency, and amount was already submitted."
        ),
        "evaluated_at": evaluated_at,
    }


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
    extras = dict(zip(_EXTRA_COLUMNS, row[len(_COLUMNS):]))
    details = extras.pop("details") or {}
    invoice = {**base, **details, **extras}
    invoice["revision"] = invoice.get("revision") or 1
    return invoice


class InMemoryInvoiceRepository:
    """In-memory store, used when no DATABASE_URL is set."""

    def __init__(self) -> None:
        self._invoices: dict[str, dict] = {}
        self._review_actions: list[dict] = []
        self._dup_index: dict[str, str] = {}
        self._lock = threading.Lock()

    def _store(self, invoice: dict) -> None:
        record = dict(invoice)
        record.setdefault("decision", None)
        record.setdefault("published_at", None)
        record.setdefault("revision", 1)
        record.setdefault("review_requested_at", None)
        record.setdefault("duplicate_key", None)
        record.setdefault("duplicate_of", None)
        record.setdefault("payment", None)
        record.setdefault("payment_flow_version", None)
        record.setdefault("payment_requested_at", None)
        record.setdefault("payment_published_at", None)
        self._invoices[invoice["tracking_id"]] = record

    def create_invoice(self, invoice: dict) -> tuple[str, dict]:
        with self._lock:
            record = dict(invoice)
            key = duplicate_key_for(record)
            original_id = self._dup_index.get(key) if key else None
            if original_id is None:
                record["duplicate_key"] = key
                self._store(record)
                if key:
                    self._dup_index[key] = record["tracking_id"]
                return "created", dict(self._invoices[record["tracking_id"]])
            record["duplicate_key"] = None
            record["duplicate_of"] = original_id
            record["status"] = "duplicate"
            record["decision"] = _duplicate_decision(original_id, record["created_at"])
            record["published_at"] = record["created_at"]
            self._store(record)
            return "duplicate", dict(self._invoices[record["tracking_id"]])

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
            if status == "approved":
                invoice["payment_flow_version"] = 1
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
                if invoice.get("published_at") is None
                and invoice["status"] == "received"
                and invoice["created_at"] < created_before
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
            if new_status == "approved":
                invoice["payment_flow_version"] = 1
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
            if invoice.get("duplicate_key") is None:
                new_key = duplicate_key_for(invoice)
                if new_key:
                    original_id = self._dup_index.get(new_key)
                    if original_id and original_id != tracking_id:
                        # the supplied invoice number reveals an existing original
                        invoice["status"] = "duplicate"
                        invoice["duplicate_of"] = original_id
                        invoice["decision"] = _duplicate_decision(original_id, updated_at)
                        invoice["updated_at"] = updated_at
                        return "duplicate", dict(invoice)
                    invoice["duplicate_key"] = new_key
                    self._dup_index[new_key] = tracking_id
            invoice["revision"] = invoice.get("revision", 1) + 1
            invoice["status"] = "received"
            invoice["decision"] = None
            invoice["published_at"] = None
            invoice["updated_at"] = updated_at
            return "applied", dict(invoice)

    def start_payment(self, tracking_id: str, requested_at: str) -> bool:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            if (
                invoice is None
                or invoice["status"] != "approved"
                or invoice.get("payment_flow_version") != 1
            ):
                return False
            invoice["status"] = "payment_requested"
            invoice["payment_requested_at"] = requested_at
            invoice["payment_published_at"] = None
            invoice["updated_at"] = requested_at
            return True

    def mark_payment_published(self, tracking_id: str, published_at: str) -> None:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            if invoice is not None and invoice.get("payment_published_at") is None:
                invoice["payment_published_at"] = published_at

    def find_approved_for_payment(self, updated_before: str) -> list[dict]:
        with self._lock:
            return [
                dict(invoice)
                for invoice in self._invoices.values()
                if invoice["status"] == "approved"
                and invoice.get("payment_flow_version") == 1
                and invoice["updated_at"] < updated_before
            ]

    def find_payment_unpublished(self, requested_before: str) -> list[dict]:
        with self._lock:
            return [
                dict(invoice)
                for invoice in self._invoices.values()
                if invoice["status"] == "payment_requested"
                and invoice.get("payment_published_at") is None
                and (invoice.get("payment_requested_at") or "") < requested_before
            ]

    def apply_payment_result(self, tracking_id: str, status: str, summary: dict, updated_at: str) -> bool:
        with self._lock:
            invoice = self._invoices.get(tracking_id)
            if invoice is None or invoice["status"] != "payment_requested":
                return False
            invoice["status"] = status
            invoice["payment"] = dict(summary)
            invoice["updated_at"] = updated_at
            return True


class PostgresInvoiceRepository:
    """PostgreSQL store, used when DATABASE_URL is set.

    Opens a connection per operation and creates/migrates the tables on first
    use. Status transitions are guarded in SQL, and the partial unique index on
    duplicate_key is the arbiter for concurrent duplicate submissions.
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

    def _insert(self, conn, invoice: dict, skip_on_conflict: bool) -> bool:
        base, details = _split(invoice)
        params = {
            **base,
            "details": json.dumps(details),
            "revision": invoice.get("revision", 1),
            "decision": json.dumps(invoice["decision"]) if invoice.get("decision") else None,
            "published_at": invoice.get("published_at"),
            "duplicate_key": invoice.get("duplicate_key"),
            "duplicate_of": invoice.get("duplicate_of"),
        }
        columns = ", ".join(_INSERT_COLUMNS)
        placeholders = ", ".join(f"%({column})s" for column in _INSERT_COLUMNS)
        query = f"INSERT INTO invoices ({columns}) VALUES ({placeholders})"
        if skip_on_conflict:
            query += " ON CONFLICT (duplicate_key) WHERE duplicate_key IS NOT NULL DO NOTHING"
        cursor = conn.execute(query, params)
        return cursor.rowcount > 0

    def create_invoice(self, invoice: dict) -> tuple[str, dict]:
        self._ensure_table()
        record = dict(invoice)
        record["duplicate_key"] = duplicate_key_for(record)
        record["duplicate_of"] = None
        with self._connect() as conn:
            if self._insert(conn, record, skip_on_conflict=True):
                return "created", record
            # the index rejected the insert, so an original with this key exists
            row = conn.execute(
                "SELECT tracking_id FROM invoices WHERE duplicate_key = %s",
                (record["duplicate_key"],),
            ).fetchone()
            original_id = row[0]
            record["duplicate_key"] = None
            record["duplicate_of"] = original_id
            record["status"] = "duplicate"
            record["decision"] = _duplicate_decision(original_id, record["created_at"])
            record["published_at"] = record["created_at"]
            self._insert(conn, record, skip_on_conflict=False)
            return "duplicate", record

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
            "review_requested_at = CASE WHEN %s = 'pending_review' THEN %s ELSE review_requested_at END, "
            "payment_flow_version = CASE WHEN %s = 'approved' THEN 1 ELSE payment_flow_version END "
            "WHERE tracking_id = %s AND status = 'received' AND COALESCE(revision, 1) = %s"
        )
        with self._connect() as conn:
            cursor = conn.execute(
                query,
                (status, json.dumps(decision), updated_at, status, updated_at, status, tracking_id, revision),
            )
            return cursor.rowcount > 0

    def mark_published(self, tracking_id: str, published_at: str) -> None:
        self._ensure_table()
        query = "UPDATE invoices SET published_at = %s WHERE tracking_id = %s AND published_at IS NULL"
        with self._connect() as conn:
            conn.execute(query, (published_at, tracking_id))

    def find_unpublished(self, created_before: str) -> list[dict]:
        self._ensure_table()
        query = (
            f"SELECT {_SELECT} FROM invoices "
            "WHERE published_at IS NULL AND status = 'received' AND created_at < %s"
        )
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
                "UPDATE invoices SET status = %s, updated_at = %s, "
                "payment_flow_version = CASE WHEN %s = 'approved' THEN 1 ELSE payment_flow_version END "
                "WHERE tracking_id = %s AND status = 'pending_review'",
                (new_status, updated_at, new_status, record["tracking_id"]),
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
        import psycopg

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
            if invoice.get("duplicate_key") is None:
                invoice["duplicate_key"] = duplicate_key_for(invoice)
            invoice["revision"] += 1
            invoice["status"] = "received"
            invoice["decision"] = None
            invoice["published_at"] = None
            invoice["updated_at"] = updated_at
            _, details = _split(invoice)
            # savepoint so a duplicate-key violation does not abort the transaction
            conn.execute("SAVEPOINT apply_info")
            try:
                conn.execute(
                    "UPDATE invoices SET receipt_present = %s, note = %s, details = %s, revision = %s, "
                    "status = 'received', decision = NULL, published_at = NULL, updated_at = %s, "
                    "duplicate_key = %s WHERE tracking_id = %s",
                    (
                        invoice["receipt_present"],
                        invoice["note"],
                        json.dumps(details),
                        invoice["revision"],
                        updated_at,
                        invoice["duplicate_key"],
                        tracking_id,
                    ),
                )
                conn.execute("RELEASE SAVEPOINT apply_info")
                return "applied", invoice
            except psycopg.errors.UniqueViolation:
                conn.execute("ROLLBACK TO SAVEPOINT apply_info")
                original = conn.execute(
                    "SELECT tracking_id FROM invoices WHERE duplicate_key = %s",
                    (invoice["duplicate_key"],),
                ).fetchone()
                original_id = original[0]
                decision = _duplicate_decision(original_id, updated_at)
                conn.execute(
                    "UPDATE invoices SET status = 'duplicate', duplicate_of = %s, decision = %s, "
                    "details = %s, updated_at = %s WHERE tracking_id = %s",
                    (original_id, json.dumps(decision), json.dumps(details), updated_at, tracking_id),
                )
                invoice["status"] = "duplicate"
                invoice["duplicate_of"] = original_id
                invoice["duplicate_key"] = None
                invoice["decision"] = decision
                invoice["revision"] -= 1  # the rolled-back update had bumped it
                return "duplicate", invoice

    def start_payment(self, tracking_id: str, requested_at: str) -> bool:
        self._ensure_table()
        query = (
            "UPDATE invoices SET status = 'payment_requested', payment_requested_at = %s, "
            "payment_published_at = NULL, updated_at = %s "
            "WHERE tracking_id = %s AND status = 'approved' AND payment_flow_version = 1"
        )
        with self._connect() as conn:
            cursor = conn.execute(query, (requested_at, requested_at, tracking_id))
            return cursor.rowcount > 0

    def mark_payment_published(self, tracking_id: str, published_at: str) -> None:
        self._ensure_table()
        query = (
            "UPDATE invoices SET payment_published_at = %s "
            "WHERE tracking_id = %s AND payment_published_at IS NULL"
        )
        with self._connect() as conn:
            conn.execute(query, (published_at, tracking_id))

    def find_approved_for_payment(self, updated_before: str) -> list[dict]:
        self._ensure_table()
        # payment_flow_version is stamped on approval since the payment flow
        # shipped; rows approved before it stay NULL and are never swept here
        query = (
            f"SELECT {_SELECT} FROM invoices "
            "WHERE status = 'approved' AND payment_flow_version = 1 AND updated_at < %s"
        )
        with self._connect() as conn:
            rows = conn.execute(query, (updated_before,)).fetchall()
        return [_row_to_invoice(row) for row in rows]

    def find_payment_unpublished(self, requested_before: str) -> list[dict]:
        self._ensure_table()
        query = (
            f"SELECT {_SELECT} FROM invoices "
            "WHERE status = 'payment_requested' AND payment_published_at IS NULL "
            "AND payment_requested_at < %s"
        )
        with self._connect() as conn:
            rows = conn.execute(query, (requested_before,)).fetchall()
        return [_row_to_invoice(row) for row in rows]

    def apply_payment_result(self, tracking_id: str, status: str, summary: dict, updated_at: str) -> bool:
        self._ensure_table()
        query = (
            "UPDATE invoices SET status = %s, payment = %s, updated_at = %s "
            "WHERE tracking_id = %s AND status = 'payment_requested'"
        )
        with self._connect() as conn:
            cursor = conn.execute(query, (status, json.dumps(summary), updated_at, tracking_id))
            return cursor.rowcount > 0


_repository = None


def get_repository():
    global _repository
    if _repository is None:
        dsn = os.getenv("DATABASE_URL", "")
        _repository = PostgresInvoiceRepository(dsn) if dsn else InMemoryInvoiceRepository()
    return _repository
