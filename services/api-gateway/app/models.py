from pydantic import BaseModel, Field


class InvoiceSubmission(BaseModel):
    vendor: str = Field(min_length=1)
    amount: float = Field(gt=0)
    currency: str = "USD"
    category: str = Field(min_length=1)
    receipt_present: bool
    submitter_email: str = Field(min_length=3)
    note: str | None = None
