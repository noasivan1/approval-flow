import { useState } from "react";
import { submitAdditionalInfo } from "./api.js";

export default function AdditionalInfoForm({ trackingId, onSubmitted }) {
  const [form, setForm] = useState({
    attendees: "",
    receipt_present: false,
    business_justification: "",
    client_name: "",
    note: "",
  });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  function update(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const payload = {};
    if (form.attendees) payload.attendees = Number(form.attendees);
    if (form.receipt_present) payload.receipt_present = true;
    if (form.business_justification.trim()) payload.business_justification = form.business_justification.trim();
    if (form.client_name.trim()) payload.client_name = form.client_name.trim();
    if (form.note.trim()) payload.note = form.note.trim();
    if (Object.keys(payload).length === 0) {
      setError("Provide at least one field.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await submitAdditionalInfo(trackingId, payload);
      onSubmitted();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={handleSubmit}>
      <p>
        <strong>Provide additional information</strong>
      </p>
      <label>
        Attendees
        <input
          type="number"
          step="1"
          min="1"
          value={form.attendees}
          onChange={(e) => update("attendees", e.target.value)}
        />
      </label>
      <label className="form__checkbox">
        <input
          type="checkbox"
          checked={form.receipt_present}
          onChange={(e) => update("receipt_present", e.target.checked)}
        />
        Receipt attached
      </label>
      <label>
        Business justification
        <input
          value={form.business_justification}
          onChange={(e) => update("business_justification", e.target.value)}
        />
      </label>
      <label>
        Client name
        <input value={form.client_name} onChange={(e) => update("client_name", e.target.value)} />
      </label>
      <label>
        Note
        <input value={form.note} onChange={(e) => update("note", e.target.value)} />
      </label>
      <button type="submit" disabled={busy}>
        {busy ? "Sending..." : "Send for re-evaluation"}
      </button>
      {error && <p className="form__error">{error}</p>}
    </form>
  );
}
