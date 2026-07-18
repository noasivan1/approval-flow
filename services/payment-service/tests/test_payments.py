import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from fastapi.testclient import TestClient

from app import events, main
from app.main import app
from app.repository import get_repository, money

client = TestClient(app)

ENGINEERING = "engineering-2026Q2"


def payment_event(tracking_id=None, **overrides):
    data = {
        "event_id": str(uuid.uuid4()),
        "tracking_id": tracking_id or str(uuid.uuid4()),
        "correlation_id": "corr-pay",
        "amount_usd": 42.0,
        "currency": "USD",
        "amount": 42.0,
        "department": ENGINEERING,
        "vendor": "Bistro 19",
        "invoice_number": f"NW-{uuid.uuid4().hex[:8]}",
        "requested_at": "2026-01-01T00:00:00+00:00",
    }
    data.update(overrides)
    return {"data": data}


def patch_publisher(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append((topic, data))

    monkeypatch.setattr(events, "publish", fake_publish)
    return published


def budget(department=ENGINEERING):
    return get_repository().get_budget(department)


def test_valid_request_creates_one_paid_payment(monkeypatch):
    published = patch_publisher(monkeypatch)
    before = budget()
    event = payment_event()

    response = client.post("/events/payment-requested", json=event)
    assert response.json() == {"status": "SUCCESS"}

    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["status"] == "paid"
    assert payment["executed_at"]
    assert payment["result_published_at"]

    after = budget()
    assert after["spent"] == before["spent"] + money(42)
    assert after["reserved"] == before["reserved"]

    assert len(published) == 1
    topic, result = published[0]
    assert topic == "payment.result"
    assert result["status"] == "paid"
    assert result["reservation_released"] is False
    assert result["correlation_id"] == "corr-pay"


def test_replay_creates_no_second_payment(monkeypatch):
    published = patch_publisher(monkeypatch)
    before = budget()
    event = payment_event()

    first = client.post("/events/payment-requested", json=event)
    second = client.post("/events/payment-requested", json=event)
    assert first.json() == {"status": "SUCCESS"}
    assert second.json() == {"status": "SUCCESS"}

    after = budget()
    assert after["spent"] == before["spent"] + money(42)  # moved once
    # both publishes carry the identical stable event
    assert published[0][1]["event_id"] == published[1][1]["event_id"]
    assert published[0][1]["payment_id"] == published[1][1]["payment_id"]


def test_insufficient_budget_fails_without_compensation(monkeypatch):
    published = patch_publisher(monkeypatch)
    get_repository().seed_budgets(
        [{"budget_id": "tiny-1", "department": "tiny-dept-1", "currency": "USD", "total": 10}]
    )
    event = payment_event(department="tiny-dept-1", amount_usd=50.0)

    client.post("/events/payment-requested", json=event)
    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["status"] == "failed"
    assert payment["failure_reason"] == "insufficient_budget"
    assert payment["released_at"] is None

    account = get_repository().get_budget("tiny-dept-1")
    assert account["reserved"] == Decimal("0.00")
    assert published[-1][1]["status"] == "payment_failed"
    assert published[-1][1]["reservation_released"] is False


def test_missing_department_fails_safely(monkeypatch):
    published = patch_publisher(monkeypatch)
    event = payment_event(department=None)
    client.post("/events/payment-requested", json=event)
    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["status"] == "failed"
    assert payment["failure_reason"] == "missing_department"
    assert published[-1][1]["status"] == "payment_failed"


def test_unknown_department_fails_safely(monkeypatch):
    patch_publisher(monkeypatch)
    event = payment_event(department="no-such-dept")
    client.post("/events/payment-requested", json=event)
    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["failure_reason"] == "budget_not_found"


def test_missing_usd_amount_fails_safely(monkeypatch):
    patch_publisher(monkeypatch)
    event = payment_event(amount_usd=None, currency="EUR", amount=100.0)
    client.post("/events/payment-requested", json=event)
    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["failure_reason"] == "missing_usd_amount"


def test_configured_invoice_number_declines_and_releases_once(monkeypatch):
    published = patch_publisher(monkeypatch)
    before = budget()
    event = payment_event(invoice_number="RS-90021", amount_usd=9500.0)

    client.post("/events/payment-requested", json=event)
    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["status"] == "failed"
    assert payment["failure_reason"] == "provider_declined"
    assert payment["released_at"] is not None

    after = budget()
    assert after["reserved"] == before["reserved"]  # released exactly once
    assert after["spent"] == before["spent"]

    result = published[-1][1]
    assert result["status"] == "payment_failed"
    assert result["reservation_released"] is True

    # compensation replay is harmless
    client.post("/events/payment-requested", json=event)
    assert budget()["reserved"] == before["reserved"]


def test_failure_number_matches_verification_suffix(monkeypatch):
    published = patch_publisher(monkeypatch)
    before = budget()
    event = payment_event(invoice_number="RS-90021-ABC123", amount_usd=9500.0)

    client.post("/events/payment-requested", json=event)
    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["status"] == "failed"
    assert payment["failure_reason"] == "provider_declined"
    assert budget()["reserved"] == before["reserved"]

    # unrelated numbers still succeed
    ok_event = payment_event(invoice_number="RS-99999")
    client.post("/events/payment-requested", json=ok_event)
    assert get_repository().get_payment(ok_event["data"]["tracking_id"])["status"] == "paid"


def test_redelivery_while_reserved_resumes_without_second_reservation(monkeypatch):
    patch_publisher(monkeypatch)
    before = budget()
    event = payment_event()
    tracking_id = event["data"]["tracking_id"]

    # simulate a crash after reservation: create the reserved payment directly
    repository = get_repository()
    record = main._new_payment_record(
        main.PaymentRequest(**{k: event["data"].get(k) for k in main.PaymentRequest.model_fields}),
        main._utc_now(),
    )
    outcome, payment = repository.create_payment(record, None)
    assert outcome == "created" and payment["status"] == "reserved"
    assert budget()["reserved"] == before["reserved"] + money(42)

    # redelivery resumes execution instead of reserving again
    client.post("/events/payment-requested", json=event)
    payment = repository.get_payment(tracking_id)
    assert payment["status"] == "paid"
    after = budget()
    assert after["reserved"] == before["reserved"]
    assert after["spent"] == before["spent"] + money(42)


def test_recovery_resumes_reserved_and_republishes_results(monkeypatch):
    import asyncio

    published = patch_publisher(monkeypatch)
    repository = get_repository()

    # a reserved payment left behind by a crash
    event = payment_event()
    record = main._new_payment_record(
        main.PaymentRequest(**{k: event["data"].get(k) for k in main.PaymentRequest.model_fields}),
        main._utc_now(),
    )
    repository.create_payment(record, None)

    # a terminal payment whose result was never published
    done_event = payment_event()
    client.post("/events/payment-requested", json=done_event)
    done = repository.get_payment(done_event["data"]["tracking_id"])
    done_before = len(published)
    repository._payments[done["tracking_id"]]["result_published_at"] = None  # simulate publish failure

    count = asyncio.run(main.recover_pending())
    assert count == 2
    assert repository.get_payment(event["data"]["tracking_id"])["status"] == "paid"
    assert len(published) == done_before + 2


def test_concurrent_reservations_cannot_overspend(monkeypatch):
    patch_publisher(monkeypatch)
    get_repository().seed_budgets(
        [{"budget_id": "race-1", "department": "race-dept", "currency": "USD", "total": 300}]
    )
    events_pair = [payment_event(department="race-dept", amount_usd=250.0) for _ in range(2)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda e: client.post("/events/payment-requested", json=e), events_pair))

    payments = [get_repository().get_payment(e["data"]["tracking_id"]) for e in events_pair]
    statuses = sorted(p["status"] for p in payments)
    assert statuses == ["failed", "paid"]
    account = get_repository().get_budget("race-dept")
    assert account["spent"] == money(250)
    assert account["reserved"] == Decimal("0.00")


def test_malformed_event_is_dropped(monkeypatch):
    async def fake_publish(topic, data):
        raise AssertionError("must not publish for malformed events")

    monkeypatch.setattr(events, "publish", fake_publish)
    assert client.post("/events/payment-requested", json={"id": "x"}).json() == {"status": "DROP"}
    assert (
        client.post("/events/payment-requested", json={"data": {"event_id": "e1"}}).json()
        == {"status": "DROP"}
    )


def test_publish_failure_returns_retry_then_recovers(monkeypatch):
    calls = []

    async def failing_publish(topic, data):
        calls.append(data)
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(events, "publish", failing_publish)
    event = payment_event()
    response = client.post("/events/payment-requested", json=event)
    assert response.json() == {"status": "RETRY"}

    # payment is durable and terminal; redelivery just republishes
    payment = get_repository().get_payment(event["data"]["tracking_id"])
    assert payment["status"] == "paid"
    assert payment["result_published_at"] is None

    published = patch_publisher(monkeypatch)
    response = client.post("/events/payment-requested", json=event)
    assert response.json() == {"status": "SUCCESS"}
    assert len(published) == 1
    assert get_repository().get_payment(event["data"]["tracking_id"])["result_published_at"]


def test_subscription_lists_payment_topic():
    assert client.get("/dapr/subscribe").json() == [
        {"pubsubname": "pubsub", "topic": "payment.requested", "route": "/events/payment-requested"}
    ]
