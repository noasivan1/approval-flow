const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export async function submitInvoice(invoice) {
  const response = await fetch(`${API_BASE_URL}/invoices`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(invoice),
  });
  if (!response.ok) {
    throw new Error(`Submission failed (${response.status})`);
  }
  return response.json();
}

export async function getInvoice(trackingId) {
  const response = await fetch(`${API_BASE_URL}/invoices/${encodeURIComponent(trackingId)}`);
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`Lookup failed (${response.status})`);
  }
  return response.json();
}
