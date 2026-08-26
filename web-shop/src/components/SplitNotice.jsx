import React, { useEffect, useRef } from "react";
import { useCart } from "../store.jsx";
import { brand, variantLabel } from "../brand.js";
import { rupees } from "../money.js";

/* Shown when a request was part-filled (spec 7.2).
 *
 * A shopper who asks for 16 and silently receives 12 discovers it at the till.
 * This says what went in, what was held, and when the rest is back - and it is
 * a dialog rather than a toast because it needs acknowledging, not glimpsing.
 */
export default function SplitNotice() {
  const { split, setSplit, setDrawerOpen } = useCart();
  const closeRef = useRef(null);

  useEffect(() => {
    if (split) closeRef.current?.focus();
  }, [split]);

  useEffect(() => {
    if (!split) return undefined;
    const onKey = (e) => e.key === "Escape" && setSplit(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [split, setSplit]);

  if (!split) return null;

  const held = split.reserved > 0;
  const none = split.added === 0;
  const alreadyHad = split.already_in_cart > 0;
  const label = `${split.product_name}${split.variant ? ` · ${variantLabel()} ${split.variant}` : ""}`;

  // Four different things can have happened, and the copy has to match the one
  // that did. Saying "part in your basket" when nothing went in - or "we only
  // had 0, so those are in your basket" - reads as broken, because it is.
  const heading = none
    ? held
      ? "None left — all held for you"
      : "None available right now"
    : held
      ? "Part in your basket, part held"
      : "Only some were available";

  // Why there was nothing to add: an empty shelf, or a basket that already
  // holds everything the shelf had.
  const reason = none
    ? alreadyHad
      ? `Your basket already has all ${split.already_in_cart} we had, so none could be added.`
      : "There were none left on the shelf."
    : `We only had ${split.added} more to give, so those are in your basket.`;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
      onClick={() => setSplit(null)}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="split-title"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-card border border-line bg-card p-6 shadow-xl"
      >
        <p className="brand-label text-xs font-bold text-accent-ink">{heading}</p>
        <h2 id="split-title" className="mt-2 font-display text-xl font-semibold">
          {label}
        </h2>

        <dl className="mt-4 space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-muted">{alreadyHad ? "You wanted, in total" : "You asked for"}</dt>
            <dd className="font-medium">{split.requested}</dd>
          </div>
          {alreadyHad && (
            <div className="flex justify-between">
              <dt className="text-muted">Already in your basket</dt>
              <dd className="font-medium">{split.already_in_cart}</dd>
            </div>
          )}
          <div className="flex justify-between">
            <dt className="text-muted">Added just now</dt>
            <dd className={`font-medium ${none ? "text-muted" : ""}`}>{split.added}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted">{held ? "Held for you" : "Not available"}</dt>
            <dd className={`font-medium ${held ? "text-brand" : "text-danger"}`}>
              {split.shortfall}
            </dd>
          </div>
          <div className="flex justify-between border-t border-line pt-2">
            <dt className="text-muted">{held ? "Held, not charged" : "Could not sell you"}</dt>
            <dd className="font-medium">
              {rupees(split.shortfall * split.unit_price_paise)}
            </dd>
          </div>
        </dl>

        <p className="mt-4 text-sm leading-relaxed text-muted">
          {held ? (
            <>
              {reason} {alreadyHad ? "The remaining" : none ? "All" : "The other"}{" "}
              {split.shortfall}{" "}
              {split.shortfall === 1 ? "is" : "are"} held in your name
              {split.restock_date ? ` and expected back on ${split.restock_date}` : ""}. Nothing
              has been charged for {split.shortfall === 1 ? "it" : "them"} — VelcrowAI will offer{" "}
              {split.shortfall === 1 ? "it" : "them"} to you the moment{" "}
              {split.shortfall === 1 ? "it lands" : "they land"}.
            </>
          ) : (
            <>
              {reason} {brand.name} cannot hold {none ? "any" : `the other ${split.shortfall}`} for
              you, but we have recorded that you wanted{" "}
              {split.shortfall === 1 ? "it" : "them"}.
            </>
          )}
        </p>

        <div className="mt-6 flex gap-3">
          <button
            ref={closeRef}
            onClick={() => {
              setSplit(null);
              setDrawerOpen(true);
            }}
            className="flex-1 rounded-control bg-brand px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-deep"
          >
            See my basket
          </button>
          <button
            onClick={() => setSplit(null)}
            className="rounded-control border border-line px-4 py-2.5 text-sm font-semibold text-muted hover:border-ink hover:text-ink"
          >
            Keep shopping
          </button>
        </div>
      </div>
    </div>
  );
}
