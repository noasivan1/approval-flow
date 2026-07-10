import { useState } from "react";
import { getInvoice } from "./api.js";

// server timestamps are UTC; show them in the viewer's local time
function formatDate(isoString) {
  const date = new Date(isoString);
  return isNaN(date) ? isoString : date.toLocaleString();
}

export default function StatusLookup() {
  const [trackingId, setTrackingId] = useState("");
  const [invoice, setInvoice] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleLookup(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setInvoice(null);
    setNotFound(false);
    try {
      const result = await getInvoice(trackingId.trim());
      if (result === null) {
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
        </div>
      )}
      {notFound && <p className="form__error">No invoice found for this tracking id.</p>}
      {error && <p className="form__error">{error}</p>}
    </form>
  );
}
