import { useState } from "react";
import { getInvoice } from "./api.js";
import AdditionalInfoForm from "./AdditionalInfoForm.jsx";

// server timestamps are UTC; show them in the viewer's local time
function formatDate(isoString) {
  const date = new Date(isoString);
  return isNaN(date) ? isoString : date.toLocaleString();
}

function lastInfoRequest(invoice) {
  const requests = (invoice.review_actions || []).filter((a) => a.action === "request_more_info");
  return requests.length ? requests[requests.length - 1] : null;
}

export default function StatusLookup() {
  const [trackingId, setTrackingId] = useState("");
  const [invoice, setInvoice] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function lookup(id) {
    setBusy(true);
    setError(null);
    setNotFound(false);
    try {
      const result = await getInvoice(id);
      if (result === null) {
        setInvoice(null);
        setNotFound(true);
      } else {
        setInvoice(result);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleLookup(event) {
    event.preventDefault();
    setInvoice(null);
    await lookup(trackingId.trim());
  }

  const infoRequest = invoice ? lastInfoRequest(invoice) : null;

  return (
    <form className="form" onSubmit={handleLookup}>
      <label>
        Tracking ID
        <input
          value={trackingId}
          onChange={(e) => setTrackingId(e.target.value)}
          placeholder="paste a tracking id"
          required
        />
      </label>

      <button type="submit" disabled={busy}>
        {busy ? "Checking..." : "Check status"}
      </button>

      {invoice && (
        <div className="form__result">
          <p>
            Status: <strong>{invoice.status}</strong>
          </p>
          <p>
            {invoice.vendor} — {invoice.amount} {invoice.currency} ({invoice.category})
          </p>
          <p>Submitted: {formatDate(invoice.created_at)}</p>
          {invoice.duplicate_of && (
            <p>
              Duplicate of: <code>{invoice.duplicate_of}</code>
            </p>
          )}
          {invoice.decision && (
            <div className="decision">
              <p>
                Route: <strong>{invoice.decision.route}</strong>
              </p>
              <p>{invoice.decision.plain_language_reason}</p>
              {invoice.decision.rule_ids?.length > 0 && (
                <p className="decision__rules">
                  {invoice.decision.rule_ids.map((ruleId) => (
                    <span key={ruleId} className="decision__rule">
                      {ruleId}
                    </span>
                  ))}
                </p>
              )}
              {invoice.decision.recommendation && (
                <p>
                  Recommendation: {invoice.decision.recommendation} (confidence{" "}
                  {Number(invoice.decision.confidence).toFixed(2)})
                </p>
              )}
            </div>
          )}
          {!invoice.decision && invoice.status === "received" && <p>Decision pending.</p>}
          {invoice.status === "payment_requested" && <p>Payment processing…</p>}

          {invoice.payment && (
            <div className="decision">
              <p>
                <strong>Payment</strong>
              </p>
              {invoice.payment.status === "paid" && (
                <p>
                  Paid — payment <code>{invoice.payment.payment_id}</code>
                  {invoice.payment.executed_at && ` at ${formatDate(invoice.payment.executed_at)}`}
                </p>
              )}
              {invoice.payment.status === "payment_failed" && (
                <>
                  <p>Payment failed: {invoice.payment.failure_reason}</p>
                  {invoice.payment.reservation_released && <p>Budget reservation was released.</p>}
                </>
              )}
            </div>
          )}

          {invoice.review_actions?.length > 0 && (
            <div className="decision">
              <p>
                <strong>Review history</strong>
              </p>
              {invoice.review_actions.map((action) => (
                <p key={action.action_id}>
                  {formatDate(action.created_at)} — {action.action} by {action.reviewer_email}
                  {action.comment ? `: ${action.comment}` : ""}
                </p>
              ))}
            </div>
          )}

          {invoice.status === "needs_more_info" && (
            <div className="decision">
              {infoRequest && (
                <p>
                  <strong>Information requested:</strong> {infoRequest.comment}
                </p>
              )}
              <AdditionalInfoForm
                trackingId={invoice.tracking_id}
                onSubmitted={() => lookup(invoice.tracking_id)}
              />
            </div>
          )}
        </div>
      )}
      {notFound && <p className="form__error">No invoice found for this tracking id.</p>}
      {error && <p className="form__error">{error}</p>}
    </form>
  );
}
