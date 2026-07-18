import { useEffect, useState } from "react";
import { getNotifications } from "./api.js";

const POLL_MS = 15000;

function formatDate(isoString) {
  const date = new Date(isoString);
  return isNaN(date) ? isoString : date.toLocaleString();
}

export default function Notifications() {
  const [email, setEmail] = useState("dana.cohen@northwind.example");
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    let active = true;

    async function load() {
      if (!email.trim()) return;
      try {
        const result = await getNotifications(email.trim());
        if (active) {
          setItems(result);
          setError(null);
        }
      } catch (err) {
        if (active) setError(err.message);
      }
    }

    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [email]);

  return (
    <div className="form">
      <label>
        Submitter email
        <input value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      {items.length === 0 && <p>No notifications yet.</p>}
      {items.map((item) => (
        <div key={item.notification_id} className="queue-item">
          <p>{item.message}</p>
          <p className="queue-item__id">
            {formatDate(item.created_at)} — <code>{item.tracking_id}</code>
          </p>
        </div>
      ))}
      {error && <p className="form__error">{error}</p>}
    </div>
  );
}
