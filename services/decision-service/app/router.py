from datetime import date

from .models import InvoiceData, Reason, Recommendation
from .policy import PolicyConfig, normalize_vendor

STATUS_BY_ROUTE = {
    "auto_approve": "approved",
    "human_review": "pending_review",
    "reject": "rejected",
}

_MATH_TOLERANCE = 0.01


def _vendor_known(invoice: InvoiceData, policy: PolicyConfig) -> bool:
    # the configured registry is the only authority; any client-supplied
    # vendor_known value is ignored
    return normalize_vendor(invoice.vendor) in policy.known_vendors


def _to_usd(invoice: InvoiceData, policy: PolicyConfig) -> tuple[float, Reason | None]:
    currency = invoice.currency.upper()
    if currency == "USD":
        return invoice.amount, None
    rate = policy.fx_rates.get(currency)
    if rate is None:
        return invoice.amount, Reason(
            rule_id="GLOBAL-FX", code="unknown_fx_rate", detail=f"no FX rate configured for {currency}"
        )
    return round(invoice.amount * rate, 2), None


def _is_weekend(iso_date: str | None) -> bool:
    if not iso_date:
        return False
    try:
        return date.fromisoformat(iso_date).weekday() >= 5
    except ValueError:
        return False


def _alcohol_only(invoice: InvoiceData) -> bool:
    return bool(invoice.line_items) and all(
        "alcohol" in item.description.lower() for item in invoice.line_items
    )


def evaluate(invoice: InvoiceData, policy: PolicyConfig, recommendation: Recommendation) -> dict:
    """Deterministic router. The recommendation is one input; these rules decide."""
    reasons: list[Reason] = []
    reject_reasons: list[Reason] = []

    amount_usd, fx_error = _to_usd(invoice, policy)
    if fx_error:
        reasons.append(fx_error)

    foreign = invoice.currency.upper() != "USD"
    if foreign and not fx_error and (
        amount_usd > policy.fx_hard_stop_usd or amount_usd > policy.autonomy_ceiling_usd
    ):
        reasons.append(Reason(
            rule_id="GLOBAL-FX", code="fx_hard_stop",
            detail=f"foreign-currency amount converts to ${amount_usd:,.2f}",
        ))

    if not invoice.receipt_present and amount_usd > policy.receipt_required_over_usd:
        reasons.append(Reason(
            rule_id="GLOBAL-RECEIPT", code="missing_receipt",
            detail=f"receipt required for expenses over ${policy.receipt_required_over_usd:,.0f}",
        ))

    vendor_known = _vendor_known(invoice, policy)
    if not vendor_known:
        reasons.append(Reason(
            rule_id="GLOBAL-VENDOR", code="unknown_vendor",
            detail=f"vendor '{invoice.vendor}' is not a known vendor",
        ))

    if invoice.line_items:
        expected = sum(item.quantity * item.unit_price for item in invoice.line_items)
        expected += invoice.tax_amount or 0
        if abs(expected - invoice.amount) > _MATH_TOLERANCE:
            reasons.append(Reason(
                rule_id="GLOBAL-MATH", code="totals_mismatch",
                detail=f"line items + tax = {expected:,.2f} but total = {invoice.amount:,.2f}",
            ))

    # fraud heuristic: only deterministic signals; date is day-granular so the
    # time-based signal is limited to weekend submissions
    if not vendor_known:
        signals = []
        if invoice.amount >= 1000 and invoice.amount % 100 == 0:
            signals.append("round-number amount")
        if _is_weekend(invoice.date):
            signals.append("weekend submission")
        if not invoice.line_items:
            signals.append("no line-item detail")
        if len(signals) >= 2:
            reasons.append(Reason(
                rule_id="GLOBAL-FRAUD", code="fraud_signals",
                detail="new vendor with " + ", ".join(signals),
            ))

    if invoice.category == "meals":
        if _alcohol_only(invoice):
            reject_reasons.append(Reason(
                rule_id="MEAL-03", code="alcohol_only",
                detail="alcohol-only receipts are not reimbursable",
            ))
        if invoice.attendees is None:
            reasons.append(Reason(
                rule_id="MEAL-01", code="missing_attendees",
                detail="meal submissions must include an attendee count",
            ))
        elif amount_usd / invoice.attendees > policy.meal_per_attendee_usd:
            reasons.append(Reason(
                rule_id="MEAL-01", code="per_attendee_cap",
                detail=f"${amount_usd / invoice.attendees:,.2f} per attendee exceeds "
                       f"${policy.meal_per_attendee_usd:,.0f}",
            ))
        if amount_usd > policy.entertainment_justification_over_usd and (
            not invoice.business_justification or not invoice.client_name
        ):
            reasons.append(Reason(
                rule_id="MEAL-02", code="missing_entertainment_info",
                detail=f"entertainment over ${policy.entertainment_justification_over_usd:,.0f} "
                       "requires a business justification and client name",
            ))

    if invoice.category == "saas" and amount_usd > policy.saas_monthly_cap_usd:
        reasons.append(Reason(
            rule_id="SAAS-01", code="saas_cap",
            detail=f"subscription exceeds ${policy.saas_monthly_cap_usd:,.0f}/month cap",
        ))

    if invoice.category == "hardware" and amount_usd > policy.hardware_capital_over_usd:
        reasons.append(Reason(
            rule_id="HW-02", code="capital_expense",
            detail=f"hardware over ${policy.hardware_capital_over_usd:,.0f} is a capital expense",
        ))

    if invoice.category == "travel":
        if amount_usd > policy.travel_manager_approval_over_usd:
            reasons.append(Reason(
                rule_id="TRAVEL-02", code="manager_approval",
                detail=f"single travel expense over ${policy.travel_manager_approval_over_usd:,.0f} "
                       "requires manager approval",
            ))
        if invoice.travel_class and invoice.travel_class.lower() in {"business", "first"}:
            reasons.append(Reason(
                rule_id="TRAVEL-03", code="premium_travel_class",
                detail=f"{invoice.travel_class} class travel always requires approval",
            ))

    if amount_usd > policy.autonomy_ceiling_usd:
        reasons.append(Reason(
            rule_id="AUTONOMY-CEILING", code="over_ceiling",
            detail=f"${amount_usd:,.2f} exceeds the ${policy.autonomy_ceiling_usd:,.0f} autonomy ceiling",
        ))

    if recommendation.provider_error_category:
        reasons.append(Reason(
            rule_id="AI-PROVIDER-ERROR", code=recommendation.provider_error_category,
            detail=f"recommendation provider failed ({recommendation.provider_error_category}); "
                   "escalated to human review",
        ))

    if recommendation.confidence < policy.autonomy_confidence:
        reasons.append(Reason(
            rule_id="AUTONOMY-CONFIDENCE", code="low_confidence",
            detail=f"confidence {recommendation.confidence:.2f} is below "
                   f"{policy.autonomy_confidence:.2f} ({recommendation.rationale})",
        ))

    # reject outranks escalation; anything flagged goes to a human, and only
    # a fully clean invoice can auto-approve
    if reject_reasons:
        route = "reject"
        all_reasons = reject_reasons + reasons
        plain = "Rejected: " + reject_reasons[0].detail + "."
    elif reasons:
        route = "human_review"
        all_reasons = reasons
        plain = "Escalated for human review: " + "; ".join(r.detail for r in reasons[:3]) + "."
    else:
        route = "auto_approve"
        all_reasons = []
        plain = "Approved automatically: within policy and autonomy thresholds."

    return {
        "route": route,
        "status": STATUS_BY_ROUTE[route],
        "rule_ids": [r.rule_id for r in all_reasons],
        "reasons": [r.model_dump() for r in all_reasons],
        "plain_language_reason": plain,
        "amount_usd": amount_usd,
    }
