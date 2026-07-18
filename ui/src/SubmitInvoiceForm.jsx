import { useState } from "react";
import { submitInvoice } from "./api.js";

// Example values match INV-1001 from data/sample-invoices.json.
const initialForm = {
  vendor: "Bistro 19",
  amount: "42.00",
  currency: "USD",
  category: "meals",
  receipt_present: true,
  vendor_known: true,
  attendees: "1",
  invoice_number: "",
  submitter_email: "dana.cohen@northwind.example",
  note: "",
};

const categories = ["meals", "travel", "saas", "hardware", "other"];
const currencies = ["USD", "EUR", "GBP"];

export default function SubmitInvoiceForm() {
  const [form, setForm] = useState(initialForm);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  function update(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const response = await submitInvoice({
        ...form,
        amount: Number(form.amount),
        attendees: form.category === "meals" && form.attendees ? Number(form.attendees) : null,
        invoice_number: form.invoice_number.trim() || null,
        note: form.note || null,
      });
      setResult(response);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={handleSubmit}>
      <label>
        Vendor
        <input value={form.vendor} onChange={(e) => update("vendor", e.target.value)} required />
      </label>
      <label>
        Amount
        <input
          type="number"
          step="any"
          min="0.01"
          value={form.amount}
          onChange={(e) => update("amount", e.target.value)}
          required
        />
      </label>
      <label>
        Currency
        <select value={form.currency} onChange={(e) => update("currency", e.target.value)}>
          {currencies.map((currency) => (
            <option key={currency} value={currency}>
              {currency}
            </option>
          ))}
        </select>
      </label>
      <label>
        Category
        <select value={form.category} onChange={(e) => update("category", e.target.value)}>
          {categories.map((category) => (
            <option key={category} value={category}>
              {category}
            </option>
          ))}
        </select>
      </label>
      {form.category === "meals" && (
        <label>
          Attendees
          <input
            type="number"
            step="1"
            min="1"
            value={form.attendees}
            onChange={(e) => update("attendees", e.target.value)}
            required
          />
        </label>
      )}
      <label className="form__checkbox">
        <input
          type="checkbox"
          checked={form.receipt_present}
          onChange={(e) => update("receipt_present", e.target.checked)}
        />
        Receipt attached
      </label>
      <label className="form__checkbox">
        <input
          type="checkbox"
          checked={form.vendor_known}
          onChange={(e) => update("vendor_known", e.target.checked)}
        />
        Known vendor
      </label>
      <label>
        Invoice number (optional)
        <input
          value={form.invoice_number}
          onChange={(e) => update("invoice_number", e.target.value)}
          placeholder="e.g. NW-INV-7781"
        />
      </label>
      <label>
        Submitter email
        <input
          type="email"
          value={form.submitter_email}
          onChange={(e) => update("submitter_email", e.target.value)}
          required
        />
      </label>
      <label>
        Note (optional)
        <input value={form.note} onChange={(e) => update("note", e.target.value)} />
      </label>

      <button type="submit" disabled={busy}>
        {busy ? "Submitting..." : "Submit"}
      </button>

      {result && result.status === "duplicate" && (
        <p className="form__error">
          Detected as a duplicate submission. Tracking ID: <code>{result.tracking_id}</code> — original
          invoice: <code>{result.duplicate_of}</code>
        </p>
      )}
      {result && result.status !== "duplicate" && (
        <p className="form__result">
          Submitted. Tracking ID: <code>{result.tracking_id}</code> (status: {result.status})
        </p>
      )}
      {error && <p className="form__error">{error}</p>}
    </form>
  );
}
