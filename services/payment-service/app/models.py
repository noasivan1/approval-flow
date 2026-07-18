from pydantic import BaseModel, Field


class PaymentRequest(BaseModel):
    tracking_id: str = Field(min_length=1)
    correlation_id: str = "-"
    amount_usd: float | None = None
    currency: str = "USD"
    amount: float | None = None
    department: str | None = None
    vendor: str | None = None
    invoice_number: str | None = None
    requested_at: str | None = None
