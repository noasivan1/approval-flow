import { useEffect, useState } from "react";
import { getDashboard } from "./api.js";

const rows = [
  ["Total invoices", "total_invoices"],
  ["Auto-approved", "auto_approved_count"],
  ["Human-approved", "human_approved_count"],
  ["Escalated to review", "escalated_count"],
  ["Duplicates", "duplicate_count"],
  ["Paid", "paid_count"],
  ["Payment failed", "payment_failed_count"],
];

export default function Dashboard() {
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  async function refresh() {
    try {
      setSummary(await getDashboard());
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="form">
      <button type="button" onClick={refresh}>
        Refresh
      </button>
      {summary && (
        <div className="stats">
          {rows.map(([label, key]) => (
            <p key={key}>
              {label}: <strong>{summary[key]}</strong>
            </p>
          ))}
          <p>
            Auto-approved amount: <strong>${Number(summary.auto_approved_amount_usd).toFixed(2)}</strong>
          </p>
          <p>
            Human-approved amount: <strong>${Number(summary.human_approved_amount_usd).toFixed(2)}</strong>
          </p>
        </div>
      )}
      {error && <p className="form__error">{error}</p>}
    </div>
  );
}
