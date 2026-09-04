import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { shop } from "../api.js";
import { shopperKey } from "../shopperKey.js";
import { rupees } from "../money.js";

/* Every paid order this shopper made here, newest first - and, the point of
 * the page, WHERE each one came from. The same person buys through the
 * storefront form, the corner widget, WhatsApp, or someone else's ACP client,
 * and every door leads to the same wallet; this page is where that stops
 * being a claim and becomes a list. */

const SOURCE = {
  whatsapp: { label: "WhatsApp", cls: "bg-green-100 text-green-900 border-green-300" },
  widget: { label: "Agent widget", cls: "bg-amber-100 text-amber-900 border-amber-300" },
  storefront: { label: "Website", cls: "bg-slate-100 text-slate-800 border-slate-300" },
  acp: { label: "ACP client", cls: "bg-indigo-100 text-indigo-900 border-indigo-300" },
  mcp: { label: "AI assistant (MCP)", cls: "bg-purple-100 text-purple-900 border-purple-300" },
  buyer_app: { label: "Buyer app", cls: "bg-rose-100 text-rose-900 border-rose-300" },
  other: { label: "Other", cls: "bg-slate-100 text-slate-800 border-slate-300" },
};

function when(ts) {
  return new Date(ts * 1000).toLocaleString("en-IN", {
    day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
  });
}

export default function Orders() {
  const [orders, setOrders] = useState(null);
  const [error, setError] = useState(null);
  const contact = shopperKey.contact();

  useEffect(() => {
    if (!contact) return;
    shop
      .identify(contact)
      .then((who) => shop.orderHistory(who.contact_key))
      .then((r) => setOrders(r.orders))
      .catch((e) => setError(e.why || e.message));
  }, [contact]);

  if (!contact) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-12">
        <h1 className="font-display text-3xl">Your orders</h1>
        <p className="mt-4 text-muted">
          Log in with your phone number (top right) and your orders follow you here —
          whichever door they came through.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-12">
      <h1 className="font-display text-3xl">Your orders</h1>
      <p className="mt-2 text-sm text-muted">
        Every paid order under {contact}, and the door it came through. Website, agent
        widget, WhatsApp or a standards client — every door leads to the same wallet.
      </p>
      {error && <p className="mt-6 text-sm text-danger">{error}</p>}
      {orders && orders.length === 0 && (
        <p className="mt-6 text-muted">Nothing paid under this contact here yet.</p>
      )}
      <div className="mt-6 space-y-4">
        {(orders || []).map((o) => {
          const src = SOURCE[o.source] || SOURCE.other;
          return (
            <div key={o.txn_ref} className="rounded-card border border-line bg-card p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className={`rounded-full border px-2.5 py-0.5 text-xs font-semibold ${src.cls}`}>
                    {src.label}
                  </span>
                  {o.rescued && (
                    <span className="rounded-full border border-emerald-300 bg-emerald-50 px-2.5 py-0.5 text-xs font-semibold text-emerald-900">
                      rescued sale
                    </span>
                  )}
                  <span className="text-xs text-muted">{when(o.created_ts)}</span>
                </div>
                <span className="font-semibold">{rupees(o.charge_amount)}</span>
              </div>
              <ul className="mt-3 space-y-1 text-sm">
                {o.line_items.map((l) => (
                  <li key={l.item_id + l.variant} className="flex justify-between">
                    <span>
                      {l.name}
                      {l.variant ? ` · ${l.variant}` : ""} × {l.qty}
                    </span>
                    <span className="text-muted">
                      {rupees(l.unit_price_paise * l.qty)}
                    </span>
                  </li>
                ))}
              </ul>
              <div className="mt-2 flex items-center justify-between text-xs text-muted">
                <span className="font-mono">{o.txn_ref}</span>
                {o.coupon_codes.length > 0 && <span>coupon {o.coupon_codes.join("+")}</span>}
              </div>
            </div>
          );
        })}
      </div>
      <p className="mt-8 text-xs text-muted">
        <Link to="/" className="underline">Back to the shop</Link>
      </p>
    </main>
  );
}
