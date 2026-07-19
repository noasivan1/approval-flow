from pydantic import BaseModel, Field


class PaymentRequest(BaseModel):
    """Payload of a payment.requested event. amount_usd may be missing; the
    payment then fails safely as missing_usd_amount instead of guessing FX."""

    tracking_id: str = Field(min_length=1)
    correlation_id: str = "-"
    amount_usd: float | None = None
    currency: str = "USD"
    amount: float | None = None
    department: str | None = None
    vendor: str | None = None
    invoice_number: str | None = None
    requested_at: str | None = None
