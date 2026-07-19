from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str = ""
    quantity: float = 1
    unit_price: float = 0


class InvoiceData(BaseModel):
    tracking_id: str | None = None
    vendor: str = Field(min_length=1)
    amount: float = Field(gt=0)
    currency: str = "USD"
    category: str = Field(min_length=1)
    receipt_present: bool = False
    submitter_email: str | None = None
    note: str | None = None
    invoice_number: str | None = None
    department: str | None = None
    vendor_known: bool | None = None
    attendees: int | None = None
    line_items: list[LineItem] = []
    tax_amount: float | None = None
    business_justification: str | None = None
    client_name: str | None = None
    travel_class: str | None = None
    date: str | None = None


class Recommendation(BaseModel):
    suggestion: str
    confidence: float
    rationale: str
    provider: str = "stub"
    model: str | None = None
    provider_error_category: str | None = None


class Reason(BaseModel):
    rule_id: str
    code: str
    detail: str
