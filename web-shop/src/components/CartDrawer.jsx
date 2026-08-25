import React from "react";
import { useNavigate } from "react-router-dom";
import { useCart } from "../store.jsx";
import { rupees } from "../money.js";
import { brand } from "../brand.js";
import QtyStepper from "./QtyStepper.jsx";
import { EmptyBasket } from "./States.jsx";

export default function CartDrawer() {
  const { cart, quote, drawerOpen, setDrawerOpen, updateQty, removeLine, busy } = useCart();
  const navigate = useNavigate();
  if (!drawerOpen) return null;

  const items = cart ? cart.items : [];
  const best = quote ? quote.best : null;
  const nearMiss = quote ? quote.near_miss : null;

  return (
    <div className="fixed inset-0 z-40 flex">
      <div
        className="flex-1 bg-ink/35"
        onClick={() => setDrawerOpen(false)}
        aria-label="close basket"
        role="button"
      />
      <aside className="flex h-full w-full max-w-md flex-col border-l border-line bg-paper shadow-xl">
        <div className="flex items-center justify-between border-b border-line px-5 py-4">
          <h2 className="font-display text-xl font-semibold">Your basket</h2>
          <button
            onClick={() => setDrawerOpen(false)}
            className="rounded-md p-1 text-muted hover:text-ink"
            aria-label="close"
          >
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5">
          {items.length === 0 ? (
            <EmptyBasket
              onShop={() => {
                setDrawerOpen(false);
                navigate("/");
              }}
            />
          ) : (
            <ul className="divide-y divide-line">
              {items.map((line) => (
                <li key={line.line_id} className="flex gap-3 py-4">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-semibold">{line.name}</p>
                    {line.variant && (
                      <p className="mt-0.5 text-xs uppercase tracking-wide text-muted">
                        {brand.variantLabel}: {line.variant}
                      </p>
                    )}
                    <p className="mt-1 text-sm text-muted">
                      {rupees(line.unit_price_paise)} each
                    </p>
                    <div className="mt-2 flex items-center gap-3">
                      <QtyStepper
                        compact
                        value={line.qty}
                        disabled={busy}
                        onChange={(q) => updateQty(line.line_id, q).catch(() => {})}
                      />
                      <button
                        onClick={() => removeLine(line.line_id).catch(() => {})}
                        disabled={busy}
                        className="text-sm font-medium text-muted underline underline-offset-2 hover:text-danger"
                      >
                        Remove
                      </button>
                    </div>
                  </div>
                  <p className="whitespace-nowrap font-display text-lg font-semibold">
                    {rupees(line.unit_price_paise * line.qty)}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>

        {items.length > 0 && (
          <div className="border-t border-line bg-card px-5 py-4">
            <div className="flex justify-between text-sm">
              <span className="text-muted">Subtotal</span>
              <span className="font-semibold">{rupees(cart.subtotal_paise)}</span>
            </div>
            {best && best.codes.length > 0 && (
              <div className="mt-1 flex justify-between text-sm">
                <span className="text-brand">Coupons ({best.codes.join(" + ")})</span>
                <span className="font-semibold text-brand">
                  &minus;{rupees(best.discount_paise)}
                </span>
              </div>
            )}
            {best && (
              <div className="mt-2 flex justify-between border-t border-line pt-2 font-display text-lg font-semibold">
                <span>Total</span>
                <span>{rupees(best.net_total_paise)}</span>
              </div>
            )}
            {nearMiss && (
              <p className="mt-3 rounded-lg bg-accent-soft px-3 py-2 text-xs leading-relaxed text-ink/80">
                Add {rupees(nearMiss.add_paise)} more to unlock {nearMiss.code} and save{" "}
                {rupees(nearMiss.saves_paise)}.
              </p>
            )}
            <button
              onClick={() => {
                setDrawerOpen(false);
                navigate("/checkout");
              }}
              className="mt-4 w-full rounded-lg bg-brand py-3 font-semibold text-white hover:bg-brand-deep"
            >
              Go to checkout
            </button>
          </div>
        )}
      </aside>
    </div>
  );
}
