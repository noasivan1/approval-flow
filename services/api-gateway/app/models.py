from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LineItem(BaseModel):
    description: str = ""
    quantity: float = 1
    unit_price: float = 0


# must mirror the workflow-service model: the gateway forwards model_dump(),
# so a field missing here would be silently stripped
class InvoiceSubmission(BaseModel):
    vendor: str = Field(min_length=1)
    amount: float = Field(gt=0)
    currency: str = "USD"
    category: str = Field(min_length=1)
    receipt_present: bool
    submitter_email: str = Field(min_length=3)
    note: str | None = None
    invoice_number: str | None = None
    department: str = Field(min_length=1)
    attendees: int | None = Field(default=None, ge=1)
    line_items: list[LineItem] = []
    tax_amount: float | None = None
    business_justification: str | None = None
    client_name: str | None = None
    travel_class: str | None = None
    date: str | None = None


class SubmissionResult(BaseModel):
    """Reply to an invoice submission; duplicate_of appears only for duplicates."""

    tracking_id: str
    status: str
    duplicate_of: str | None = None


class ReviewActionResult(BaseModel):
    tracking_id: str
    status: str
    result: str


class AdditionalInfoResult(BaseModel):
    """Reply to an additional-info submission. revision is set when the invoice
    was sent for re-evaluation; duplicate_of when the new details revealed a
    duplicate instead."""

    tracking_id: str
    status: str
    revision: int | None = None
    duplicate_of: str | None = None


class ReviewActionRequest(BaseModel):
    action: Literal["approve", "reject", "request_more_info"]
    reviewer_email: str = Field(min_length=3)
    comment: str | None = None
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def _comment_required_for_more_info(self):
        if self.action == "request_more_info" and not (self.comment or "").strip():
            raise ValueError("comment is required for request_more_info")
        return self


class AdditionalInfo(BaseModel):
    attendees: int | None = Field(default=None, ge=1)
    receipt_present: bool | None = None
    business_justification: str | None = None
    client_name: str | None = None
    travel_class: str | None = None
    line_items: list[LineItem] | None = None
    tax_amount: float | None = None
    date: str | None = None
    note: str | None = None
    invoice_number: str | None = None
    department: str | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one field must be provided")
        return self
