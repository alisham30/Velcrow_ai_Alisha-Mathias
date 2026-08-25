import { brand, TRUST_BASE } from "./brand.js";

// Typed errors from the backend (spec 6.6) surface as ApiError so the UI can
// branch on `code` and show `why` and `available_actions`.
export class ApiError extends Error {
  constructor(payload, status) {
    super(payload.why || `request failed (${status})`);
    this.code = payload.code || "UNKNOWN";
    this.why = payload.why || "";
    this.availableActions = payload.available_actions || [];
    this.payload = payload;
    this.status = status;
  }
}

async function request(base, path, { method = "GET", body, headers = {} } = {}) {
  let resp;
  try {
    resp = await fetch(base + path, {
      method,
      headers: { ...(body !== undefined && { "Content-Type": "application/json" }), ...headers },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(
      { code: "UNREACHABLE", why: `could not reach ${base} — is the service running?` },
      0,
    );
  }
  const text = await resp.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { why: text };
  }
  if (!resp.ok) throw new ApiError(data, resp.status);
  return data;
}

export const shop = {
  catalog: () => request(brand.apiBase, "/catalog"),
  product: (id) => request(brand.apiBase, `/product/${id}`),
  createCart: () => request(brand.apiBase, "/cart", { method: "POST", body: {} }),
  getCart: (cartId) => request(brand.apiBase, `/cart/${cartId}`),
  patchCart: (cartId, op) => request(brand.apiBase, `/cart/${cartId}`, { method: "PATCH", body: op }),
  coupons: (cartId) => request(brand.apiBase, `/cart/${cartId}/coupons`, { method: "POST", body: {} }),
  order: (cartId, mandateToken, idemKey) =>
    request(brand.apiBase, "/order", {
      method: "POST",
      body: { cart_id: cartId },
      headers: { Authorization: `Mandate ${mandateToken}`, "Idempotency-Key": idemKey },
    }),
  getOrder: (txnRef) => request(brand.apiBase, `/order/${txnRef}`),
};

export const trust = {
  issueMandate: (shops) =>
    request(TRUST_BASE, "/mandate", { method: "POST", body: { shops } }),
  pay: (payload) => request(TRUST_BASE, "/pay", { method: "POST", body: payload }),
};
