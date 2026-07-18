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

async function errorMessage(response) {
  try {
    const body = await response.json();
    if (body.detail) return typeof body.detail === "string" ? body.detail : "Invalid request";
  } catch {
    // fall through to the generic message
  }
  return `Request failed (${response.status})`;
}

export async function getApprovalsQueue() {
  const response = await fetch(`${API_BASE_URL}/approvals/queue`);
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json();
}

export async function submitReviewAction(trackingId, payload) {
  const response = await fetch(`${API_BASE_URL}/approvals/${encodeURIComponent(trackingId)}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json();
}

export async function getDashboard() {
  const response = await fetch(`${API_BASE_URL}/dashboard`);
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json();
}

export async function getTrail(trackingId) {
  const response = await fetch(`${API_BASE_URL}/invoices/${encodeURIComponent(trackingId)}/trail`);
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json();
}

export async function getNotifications(submitterEmail) {
  const response = await fetch(
    `${API_BASE_URL}/notifications?submitter_email=${encodeURIComponent(submitterEmail)}`
  );
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json();
}

export async function submitAdditionalInfo(trackingId, payload) {
  const response = await fetch(
    `${API_BASE_URL}/invoices/${encodeURIComponent(trackingId)}/additional-info`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json();
}
