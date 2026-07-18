import os


class PaymentProvider:
    def execute(self, payment_id: str, invoice_number: str | None, amount_usd) -> tuple[bool, str | None]:
        raise NotImplementedError


class SimulatedPaymentProvider(PaymentProvider):
    """Deterministic payment provider for local runs and tests.

    Configured invoice numbers return a declined result; everything else succeeds.
    """

    def __init__(self, fail_invoice_numbers: list[str]) -> None:
        self._fail_numbers = {number.strip().upper() for number in fail_invoice_numbers if number.strip()}

    def execute(self, payment_id: str, invoice_number: str | None, amount_usd) -> tuple[bool, str | None]:
        normalized = (invoice_number or "").strip().upper()
        if normalized and normalized in self._fail_numbers:
            return False, "provider_declined"
        return True, None


def get_provider() -> PaymentProvider:
    configured = os.getenv("PAYMENT_FAIL_INVOICE_NUMBERS", "RS-90021")
    return SimulatedPaymentProvider(configured.split(","))
