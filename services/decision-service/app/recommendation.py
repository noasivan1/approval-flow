import logging
import os

from .models import InvoiceData, Recommendation

logger = logging.getLogger("decision-service.recommendation")


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


class UnsupportedProvider(RecommendationProvider):
    """Misconfigured provider name: every invoice escalates instead of crashing."""

    def __init__(self, name: str) -> None:
        self._name = name

    def recommend(self, invoice: InvoiceData) -> Recommendation:
        return Recommendation(
            suggestion="review",
            confidence=0.0,
            rationale=f"recommendation provider '{self._name}' is not supported; requires human review",
            provider=self._name,
            provider_error_category="unsupported_provider",
        )


def get_provider() -> RecommendationProvider:
    name = os.getenv("RECOMMENDATION_PROVIDER", "stub")
    if name == "stub":
        return LocalStubProvider()
    if name == "gemini":
        # imported here so stub-only runs never load the gemini module
        from .gemini import GeminiRecommendationProvider

        return GeminiRecommendationProvider()
    logger.error("unknown recommendation provider %r; all invoices will escalate", name)
    return UnsupportedProvider(name)
