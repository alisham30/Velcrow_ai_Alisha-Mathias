import { brand, TRUST_BASE } from "./brand.js";
import { shopperKey } from "./shopperKey.js";

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
  // The storefront asks the shop what it supports rather than assuming
  // (spec 6.6). Reservations and variant wording both come from here.
  capabilities: () =>
    request(brand.apiBase, "/agent/capabilities", {
      method: "POST",
      body: { capabilities: { reservations: true, discounts: true, human_approval: true } },
    }).then((r) => r.capabilities),
  reserve: (payload, mandateToken) =>
    request(brand.apiBase, "/reserve", {
      method: "POST",
      body: payload,
      headers: { Authorization: `Mandate ${mandateToken}` },
    }),
  // Merchant console (spec 6.2). Every call goes to THIS shop's apiBase, so a
  // console can only ever read its own merchant's data (spec 14).
  demandLedger: () => request(brand.apiBase, "/merchant/demand-ledger"),
  summary: () => request(brand.apiBase, "/merchant/summary"),
  reservations: () => request(brand.apiBase, "/merchant/reservations"),
  restock: (itemId, variant, qty) =>
    request(brand.apiBase, "/admin/restock", {
      method: "POST",
      body: { item_id: itemId, variant, qty },
    }),
  setCheatMode: (on) =>
    request(brand.apiBase, "/admin/cheat-mode", { method: "POST", body: { on } }),
  catalog: () => request(brand.apiBase, "/catalog"),
  product: (id) => request(brand.apiBase, `/product/${id}`),
  createCart: () => request(brand.apiBase, "/cart", { method: "POST", body: {} }),
  getCart: (cartId) => request(brand.apiBase, `/cart/${cartId}`),
  patchCart: (cartId, op) => request(brand.apiBase, `/cart/${cartId}`, { method: "PATCH", body: op }),
  // Take what is in stock and hold the rest (spec 7.2). The shop verifies the
  // mandate before touching stock, so this carries one like any other write.
  fulfil: (cartId, line, mandateToken) =>
    request(brand.apiBase, `/cart/${cartId}/fulfil`, {
      method: "POST",
      body: { ...line, contact: shopperKey.contact(), shopper_ref: shopperKey.ref(),
              contact_ref: shopperKey.contact() },
      headers: { Authorization: `Mandate ${mandateToken}` },
    }),
  coupons: (cartId) => request(brand.apiBase, `/cart/${cartId}/coupons`, { method: "POST", body: {} }),
  // The shopper key travels with every order (spec 7.3). Without it an order
  // is anonymous forever and reorder can never find it - which is exactly how
  // purchases made through this checkout used to vanish from "my usual order".
  order: (cartId, mandateToken, idemKey) =>
    request(brand.apiBase, "/order", {
      method: "POST",
      body: {
        cart_id: cartId,
        shopper_ref: shopperKey.ref(),
        contact: shopperKey.contact(),
      },
      headers: { Authorization: `Mandate ${mandateToken}`, "Idempotency-Key": idemKey },
    }),
  identify: (contactText) =>
    request(brand.apiBase, "/shopper/identify", {
      method: "POST",
      body: { contact: contactText, shopper_ref: shopperKey.ref() },
    }),
  getOrder: (txnRef) => request(brand.apiBase, `/order/${txnRef}`),
};

export const trust = {
  issueMandate: (shops) =>
    request(TRUST_BASE, "/mandate", { method: "POST", body: { shops } }),
  pay: (payload) => request(TRUST_BASE, "/pay", { method: "POST", body: payload }),
};
