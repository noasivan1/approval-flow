import { useEffect, useState } from "react";
import { getApprovalsQueue, submitReviewAction } from "./api.js";

export default function ApproverQueue() {
  const [items, setItems] = useState([]);
  const [reviewer, setReviewer] = useState("finance@northwind.example");
  const [comments, setComments] = useState({});
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      setItems(await getApprovalsQueue());
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function act(item, action) {
    const comment = (comments[item.tracking_id] || "").trim();
    if (action === "request_more_info" && !comment) {
      setError("A comment is required when requesting more info.");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await submitReviewAction(item.tracking_id, {
        action,
        reviewer_email: reviewer,
        comment: comment || null,
        idempotency_key: crypto.randomUUID(),
      });
      setMessage(`${item.vendor}: ${result.status}`);
      setComments((current) => ({ ...current, [item.tracking_id]: "" }));
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="form">
      <label>
        Reviewer email
        <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} required />
      </label>

      <button type="button" onClick={refresh} disabled={busy}>
        Refresh queue
      </button>

      {items.length === 0 && <p>No invoices waiting for review.</p>}

      {items.map((item) => (
        <div key={item.tracking_id} className="queue-item">
          <p>
            <strong>{item.vendor}</strong> — {item.amount} {item.currency} ({item.category})
          </p>
          <p className="queue-item__id">
            <code>{item.tracking_id}</code>
          </p>
          {item.recommendation && (
            <p>
              Recommendation: {item.recommendation} (confidence {Number(item.confidence).toFixed(2)})
            </p>
          )}
          <p>{item.plain_language_reason}</p>
          {item.rule_ids?.length > 0 && (
            <p className="decision__rules">
              {item.rule_ids.map((ruleId) => (
                <span key={ruleId} className="decision__rule">
                  {ruleId}
                </span>
              ))}
            </p>
          )}
          <label>
            Comment (required for more info)
            <input
              value={comments[item.tracking_id] || ""}
              onChange={(e) =>
                setComments((current) => ({ ...current, [item.tracking_id]: e.target.value }))
              }
            />
          </label>
          <div className="queue-item__actions">
            <button type="button" onClick={() => act(item, "approve")} disabled={busy}>
              Approve
            </button>
            <button
              type="button"
              className="button--danger"
              onClick={() => act(item, "reject")}
              disabled={busy}
            >
              Reject
            </button>
            <button
              type="button"
              className="button--neutral"
              onClick={() => act(item, "request_more_info")}
              disabled={busy}
            >
              Request more info
            </button>
          </div>
        </div>
      ))}

      {message && <p className="form__result">{message}</p>}
      {error && <p className="form__error">{error}</p>}
    </div>
  );
}
