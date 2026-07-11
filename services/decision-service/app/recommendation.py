import os

from .models import InvoiceData, Recommendation


class RecommendationProvider:
    """Interface for the recommendation step. The router treats the output as
    one input only — it never decides the route by itself."""

    def recommend(self, invoice: InvoiceData) -> Recommendation:
        raise NotImplementedError


class LocalStubProvider(RecommendationProvider):
    """Deterministic placeholder until a model-backed provider is added.

    Confidence drops on ambiguity signals; free-text fields are never treated
    as instructions.
    """

    def recommend(self, invoice: InvoiceData) -> Recommendation:
        confidence = 0.95
        rationale = "clear category and complete details"
        if invoice.category == "other":
            confidence = 0.60
            rationale = "ambiguous category"
        elif invoice.category == "meals" and invoice.attendees is None:
            confidence = 0.70
            rationale = "meal submission without attendee count"

        suggestion = "approve" if invoice.receipt_present and invoice.amount <= 250 else "review"
        return Recommendation(suggestion=suggestion, confidence=confidence, rationale=rationale)


def get_provider() -> RecommendationProvider:
    name = os.getenv("RECOMMENDATION_PROVIDER", "stub")
    if name == "stub":
        return LocalStubProvider()
    raise ValueError(f"unknown recommendation provider: {name}")
