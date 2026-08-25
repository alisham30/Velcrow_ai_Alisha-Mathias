import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { shop, trust } from "../api.js";
import { brand } from "../brand.js";
import { rupees } from "../money.js";
import { useCart } from "../store.jsx";
import { EmptyBasket } from "../components/States.jsx";

// Checkout stages: browsing -> quoting (order placed, price locked) ->
// paying (the human tapped Approve) -> done. That tap is what signs the
// cart-bound approval; nothing moves money before it.
export default function Checkout() {
  const { cart, quote, resetCart } = useCart();
  const navigate = useNavigate();
  const [stage, setStage] = useState("browsing");
  const [order, setOrder] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [error, setError] = useState(null);

  if (receipt) {
    return <Receipt receipt={receipt} onDone={() => resetCart().then(() => navigate("/"))} />;
  }

  if (!cart || cart.items.length === 0) {
    return (
      <main className="mx-auto max-w-2xl px-4 py-10">
        <EmptyBasket onShop={() => navigate("/")} />
      </main>
    );
  }

  const best = quote ? quote.best : null;
  const nearMiss = quote ? quote.near_miss : null;
  const payable = best ? best.net_total_paise : cart.subtotal_paise;

  // Step 1: place the order. The shop verifies the mandate, applies the best
  // coupon set, holds stock and locks the price for five minutes.
  async function startCheckout() {
    setError(null);
    setStage("quoting");
    try {
      const mandate = await trust.issueMandate([brand.shopId]);
      const placed = await shop.order(cart.cart_id, mandate.token, crypto.randomUUID());
      setOrder({ ...placed, mandateToken: mandate.token, mandate });
    } catch (e) {
      setError(e);
      setStage("browsing");
    }
  }

  // Step 2: the human taps Approve. :8003 signs the cart-bound approval over
  // exactly this basket and amount, runs the wallet, then confirms with the shop.
  async function approveAndPay() {
    setError(null);
    setStage("paying");
    try {
      const paid = await trust.pay({
        shop_id: brand.shopId,
        shop_url: brand.apiBase,
        txn_ref: order.txn_ref,
        mandate_token: order.mandateToken,
        approved_amount_paise: order.charge_amount,
        approved_items: order.line_items,
      });
      setReceipt({ ...paid, order });
    } catch (e) {
      setError(e);
      setStage("quoting");
    }
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="font-display text-3xl font-bold">Checkout</h1>
      <p className="mt-1 text-muted">
        No address forms, no card forms. Payment runs through your VelcrowAI mandate.
      </p>

      {error && <PaymentError error={error} onDismiss={() => setError(null)} />}

      <section className="mt-8 rounded-xl border border-line bg-card">
        <h2 className="border-b border-line px-5 py-3 font-display text-lg font-semibold">
          Your order
        </h2>
        <ul className="divide-y divide-line px-5">
          {cart.items.map((l) => (
            <li key={l.line_id} className="flex items-baseline justify-between gap-4 py-3">
              <div className="min-w-0">
                <p className="truncate font-medium">
                  {l.name}
                  {l.variant ? ` · ${l.variant}` : ""}
                </p>
                <p className="text-sm text-muted">
                  {l.qty} × {rupees(l.unit_price_paise)}
                </p>
              </div>
              <span className="whitespace-nowrap font-semibold">
                {rupees(l.unit_price_paise * l.qty)}
              </span>
            </li>
          ))}
        </ul>

        <div className="border-t border-line px-5 py-4">
          <div className="flex justify-between text-sm">
            <span className="text-muted">Subtotal</span>
            <span>{rupees(cart.subtotal_paise)}</span>
          </div>

          {best && best.codes.length > 0 ? (
            <div className="mt-3 rounded-lg bg-ok-soft px-4 py-3">
              <p className="text-sm font-bold text-brand-deep">
                Best coupon applied automatically: {best.codes.join(" + ")}
              </p>
              <p className="mt-1 font-mono text-sm text-ink/80">{best.arithmetic}</p>
              <p className="mt-1 text-sm font-semibold text-brand">
                You save {rupees(best.discount_paise)}.
              </p>
            </div>
          ) : (
            <p className="mt-3 rounded-lg bg-paper px-4 py-3 text-sm text-muted">
              No coupon applies to this basket yet.
            </p>
          )}

          {nearMiss && (
            <div className="mt-3 rounded-lg border border-accent/40 bg-accent-soft px-4 py-3">
              <p className="text-sm font-bold text-brand-deep">
                Add {rupees(nearMiss.add_paise)} more and pay less overall
              </p>
              <p className="mt-1 text-sm text-ink/80">{nearMiss.math}</p>
              <Link
                to="/"
                className="mt-2 inline-block text-sm font-semibold text-brand underline underline-offset-2"
              >
                Add something small
              </Link>
            </div>
          )}

          <div className="mt-4 flex items-baseline justify-between border-t border-line pt-4">
            <span className="font-display text-xl font-semibold">Total payable</span>
            <span className="font-display text-2xl font-bold">{rupees(payable)}</span>
          </div>
        </div>
      </section>

      {stage === "browsing" && (
        <button
          onClick={startCheckout}
          className="mt-6 w-full rounded-xl bg-brand py-4 font-semibold text-white hover:bg-brand-deep"
        >
          Pay with VelcrowAI
        </button>
      )}

      {stage === "quoting" && !order && (
        <p className="mt-6 rounded-xl border border-line bg-card py-4 text-center text-muted">
          Locking your price…
        </p>
      )}

      {order && (stage === "quoting" || stage === "paying") && (
        <ApprovalSheet
          order={order}
          busy={stage === "paying"}
          onApprove={approveAndPay}
          onCancel={() => {
            setOrder(null);
            setStage("browsing");
          }}
        />
      )}
    </main>
  );
}

function ApprovalSheet({ order, busy, onApprove, onCancel }) {
  return (
    <section className="mt-6 rounded-xl border-2 border-brand bg-card p-5">
      <p className="text-xs font-bold uppercase tracking-wider text-brand">Approval required</p>
      <h2 className="mt-1 font-display text-xl font-semibold">
        Approve this basket at {rupees(order.charge_amount)}?
      </h2>
      <p className="mt-2 text-sm text-muted">
        You are approving exactly these lines, from {brand.name}, at this price. The approval is
        signed over this basket alone and expires in five minutes.
      </p>

      <ul className="mt-4 divide-y divide-line rounded-lg border border-line">
        {order.line_items.map((li) => (
          <li key={`${li.item_id}-${li.variant}`} className="flex justify-between px-4 py-2 text-sm">
            <span>
              {li.item_id}
              {li.variant ? ` · ${li.variant}` : ""} × {li.qty}
            </span>
            <span className="font-medium">{rupees(li.unit_price_paise * li.qty)}</span>
          </li>
        ))}
      </ul>

      <dl className="mt-4 grid grid-cols-2 gap-2 text-sm">
        <dt className="text-muted">Merchant</dt>
        <dd className="text-right font-medium">{brand.name}</dd>
        <dt className="text-muted">Reference</dt>
        <dd className="text-right font-mono text-xs">{order.txn_ref}</dd>
        <dt className="text-muted">Session cap</dt>
        <dd className="text-right font-medium">
          {rupees(order.mandate.max_per_txn_paise)} per payment
        </dd>
      </dl>

      <div className="mt-5 flex gap-3">
        <button
          onClick={onCancel}
          disabled={busy}
          className="rounded-lg border border-line px-5 py-3 font-semibold text-muted hover:text-ink disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={onApprove}
          disabled={busy}
          className="flex-1 rounded-lg bg-brand py-3 font-semibold text-white hover:bg-brand-deep disabled:opacity-60"
        >
          {busy ? "Paying…" : `Approve and pay ${rupees(order.charge_amount)}`}
        </button>
      </div>
    </section>
  );
}

function PaymentError({ error, onDismiss }) {
  const ACTION_TEXT = {
    REQUOTE: "The price changed — start checkout again for a fresh quote.",
    CANCEL: "Cancel and keep shopping.",
    RESERVE: "Reserve this item instead.",
    SELECT_ALTERNATIVE: "Choose a different item or quantity.",
    OBTAIN_MANDATE: "A fresh session mandate is needed.",
    REDUCE_AMOUNT: "Lower the basket total and try again.",
    USE_NEW_KEY: "Start a new checkout.",
    VIEW_COUPONS: "Review the coupons on this basket.",
  };
  return (
    <div className="mt-6 rounded-xl border border-danger/40 bg-danger-soft p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-display text-lg font-bold text-danger">Payment blocked</p>
          <p className="mt-1 text-sm text-ink/85">{error.why || error.message}</p>
          <p className="mt-2 font-mono text-xs text-muted">{error.code}</p>
          {(error.availableActions || []).length > 0 && (
            <ul className="mt-3 space-y-1 text-sm text-ink/80">
              {error.availableActions.map((a) => (
                <li key={a}>· {ACTION_TEXT[a] || a}</li>
              ))}
            </ul>
          )}
        </div>
        <button onClick={onDismiss} className="text-sm font-semibold text-muted hover:text-ink">
          Dismiss
        </button>
      </div>
    </div>
  );
}

function Receipt({ receipt, onDone }) {
  return (
    <main className="mx-auto max-w-2xl px-4 py-12">
      <div className="rounded-xl border border-line bg-card p-8 text-center">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-ok-soft">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M5 13l4 4L19 7"
              stroke="var(--brand)"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <h1 className="font-display text-3xl font-bold">Paid {rupees(receipt.amount_paise)}</h1>
        <p className="mt-2 text-muted">
          {brand.name} has confirmed your order. Razorpay test mode — no real money moved.
        </p>

        <dl className="mx-auto mt-6 grid max-w-sm grid-cols-2 gap-2 text-left text-sm">
          <dt className="text-muted">Order reference</dt>
          <dd className="text-right font-mono text-xs">{receipt.txn_ref}</dd>
          <dt className="text-muted">Razorpay order</dt>
          <dd className="text-right font-mono text-xs">{receipt.razorpay_order_id}</dd>
          <dt className="text-muted">Payment reference</dt>
          <dd className="text-right font-mono text-xs">{receipt.payment_ref}</dd>
          <dt className="text-muted">Shop confirmation</dt>
          <dd className="text-right font-medium">
            {receipt.confirmed ? "Confirmed" : "Pending at merchant"}
          </dd>
        </dl>

        <button
          onClick={onDone}
          className="mt-8 rounded-lg bg-brand px-6 py-3 font-semibold text-white hover:bg-brand-deep"
        >
          Continue shopping
        </button>
      </div>
    </main>
  );
}
