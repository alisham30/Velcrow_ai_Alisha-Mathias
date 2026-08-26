import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { shop, trust } from "../api.js";
import { shopperKey } from "../shopperKey.js";
import { brand, variantLabel } from "../brand.js";
import { rupees } from "../money.js";
import { useCart } from "../store.jsx";
import ProductVisual from "../components/ProductVisual.jsx";
import QtyStepper from "../components/QtyStepper.jsx";
import { ErrorBanner } from "../components/States.jsx";

export default function Product() {
  const { id } = useParams();
  const { add, busy, caps, cart } = useCart();
  const [product, setProduct] = useState(null);
  const [error, setError] = useState(null);
  const [variant, setVariant] = useState(null);
  const [qty, setQty] = useState(1);
  const [added, setAdded] = useState(false);

  const load = () => {
    setError(null);
    setProduct(null);
    shop
      .product(id)
      .then((p) => {
        setProduct(p);
        if (p.variants) {
          const first = p.variants.find((v) => v.stock > 0) || p.variants[0];
          setVariant(first.label);
        }
      })
      .catch((e) => setError(e.why || e.message));
  };

  useEffect(load, [id]);

  if (error) {
    return (
      <main className="mx-auto max-w-6xl px-4">
        <ErrorBanner text={error} onRetry={load} />
      </main>
    );
  }

  if (!product) {
    return (
      <main className="mx-auto grid max-w-5xl gap-10 px-4 py-10 md:grid-cols-2">
        <div className="skeleton w-full" style={{ aspectRatio: brand.cardAspect }} />
        <div>
          <div className="skeleton mb-4 h-9 w-3/4" />
          <div className="skeleton mb-2 h-4 w-full" />
          <div className="skeleton mb-8 h-4 w-2/3" />
          <div className="skeleton h-12 w-40" />
        </div>
      </main>
    );
  }

  const label = variantLabel(caps);
  const selected = product.variants
    ? product.variants.find((v) => v.label === variant)
    : { stock: product.stock, restock_date: product.restock_date };
  const inStock = selected && selected.stock > 0;
  const maxQty = selected ? selected.stock : 0;

  // The stepper sets the TOTAL for this line, so the button has to say so when
  // some are already in the basket: "Add 9" beside 6 already there reads as
  // fifteen to anyone sensible.
  const inBasket = (cart?.items || [])
    .filter((l) => l.item_id === product.id && (l.variant || "") === (variant || ""))
    .reduce((n, l) => n + l.qty, 0);

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <nav className="brand-label mb-6 text-sm text-muted">
        <Link to="/" className="hover:text-brand">
          {brand.name}
        </Link>
        <span className="mx-2">/</span>
        <span>{brand.categoryLabels[product.category] || product.category}</span>
      </nav>

      <div className="grid gap-10 md:grid-cols-2">
        <ProductVisual product={product} className="w-full" style={{ aspectRatio: brand.cardAspect }} />

        <div>
          <h1 className="font-display text-3xl leading-tight sm:text-4xl">{product.name}</h1>
          <p className="mt-3 leading-relaxed text-muted">{product.description}</p>

          <p className="mt-6 font-display text-3xl">{rupees(product.price_paise)}</p>
          {product.exact_only && (
            <p className="mt-2 inline-block bg-accent-soft px-2 py-1 text-xs font-semibold text-accent-ink">
              Exact item only — no substitutions
            </p>
          )}

          {product.variants && (
            <div className="mt-7">
              <div className="mb-2 flex items-baseline justify-between">
                <p className="brand-label text-sm font-semibold text-muted">{label}</p>
                <p className="text-xs text-muted">
                  {product.variants.filter((v) => v.stock > 0).length} of {product.variants.length}{" "}
                  available
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                {product.variants.map((v) => {
                  const isSel = v.label === variant;
                  const out = v.stock === 0;
                  return (
                    <button
                      key={v.label}
                      onClick={() => {
                        setVariant(v.label);
                        setQty(1);
                      }}
                      aria-pressed={isSel}
                      title={out ? `${label} ${v.label} is out of stock` : `${v.stock} in stock`}
                      className={`min-w-14 rounded-control border px-4 py-2 text-sm font-semibold transition ${
                        isSel
                          ? "border-brand bg-brand text-white"
                          : out
                            ? "border-line bg-paper text-muted line-through decoration-muted/60"
                            : "border-line bg-card hover:border-brand"
                      }`}
                    >
                      {v.label}
                    </button>
                  );
                })}
              </div>
              {brand.variantHelp && <p className="mt-3 text-xs text-muted">{brand.variantHelp}</p>}
            </div>
          )}

          <div className="mt-7 rounded-card border border-line bg-card p-5">
            {inStock ? (
              <>
                <p className="mb-3 text-sm font-semibold text-brand">
                  In stock
                  {maxQty <= 6 ? ` — only ${maxQty} left` : ""}
                </p>
                <div className="flex flex-wrap items-center gap-4">
                  <QtyStepper value={qty} onChange={setQty} disabled={busy} />
                  <button
                    disabled={busy}
                    onClick={() =>
                      add(product.id, variant, qty)
                        .then(() => {
                          setAdded(true);
                          setTimeout(() => setAdded(false), 2500);
                        })
                        .catch(() => {})
                    }
                    className="flex-1 rounded-control bg-brand px-6 py-3 font-semibold text-white hover:bg-brand-deep disabled:opacity-60"
                  >
                    {busy
                      ? "Adding…"
                      : inBasket
                        ? `Update basket to ${qty}`
                        : `Add ${qty} to basket`}
                  </button>
                </div>
                <p className="mt-3 text-sm text-muted">
                  {inBasket > 0 && (
                    <>
                      {inBasket} already in your basket ·{" "}
                    </>
                  )}
                  Line total {rupees(product.price_paise * qty)}
                </p>
                {added && (
                  <p className="mt-3 bg-ok-soft px-3 py-2 text-sm font-medium text-brand-deep">
                    Added to your basket.
                  </p>
                )}
              </>
            ) : (
              <OutOfStock
                product={product}
                variant={variant || ""}
                label={label}
                restockDate={selected && selected.restock_date}
                canReserve={Boolean(caps && caps.reservations)}
                hasVariants={Boolean(product.variants)}
              />
            )}
          </div>
        </div>
      </div>
    </main>
  );
}

// Out of stock is never a dead end where the shop supports reservations
// (spec 7.2): the offer sits inline, in the same card that normally holds
// Add to Basket. Where the shop does not support them, we say so plainly.
function OutOfStock({ product, variant, label, restockDate, canReserve, hasVariants }) {
  const [contact, setContact] = useState("");
  const [qty, setQty] = useState(1);
  const [stage, setStage] = useState("idle"); // idle | sending | done | error
  const [receipt, setReceipt] = useState(null);
  const [error, setError] = useState(null);

  const backOn = restockDate ? `Back on ${restockDate}` : "No restock date scheduled yet";

  async function reserve(e) {
    e.preventDefault();
    if (!contact.trim()) return;
    setStage("sending");
    setError(null);
    try {
      const mandate = await trust.issueMandate([brand.shopId]);
      // Reserving is the one place a shopper already types a contact, so it is
      // where the portable half of their key gets established (spec 7.3). It is
      // remembered here so checkout and the widget never ask for it again.
      shopperKey.remember(contact);
      const res = await shop.reserve(
        {
          item_id: product.id,
          variant,
          contact_ref: contact.trim(),
          qty,
          shopper_ref: shopperKey.ref(),
        },
        mandate.token,
      );
      setReceipt(res);
      setStage("done");
    } catch (err) {
      setError(err.why || err.message);
      setStage("error");
    }
  }

  if (stage === "done" && receipt) {
    return (
      <div>
        <p className="brand-label text-xs font-bold text-brand">Reserved</p>
        <p className="mt-1 font-display text-xl">
          {hasVariants ? `${label} ${variant} is held for you` : "It is held for you"}
        </p>
        <p className="mt-2 text-sm text-muted">
          We will contact {receipt.contact_ref} the moment it is back
          {receipt.restock_date ? ` — expected ${receipt.restock_date}` : ""}. Nothing has been
          charged.
        </p>
        <dl className="mt-4 grid grid-cols-2 gap-1 text-sm">
          <dt className="text-muted">Reservation</dt>
          <dd className="text-right font-mono text-xs">{receipt.res_id}</dd>
          <dt className="text-muted">Held</dt>
          <dd className="text-right font-medium">
            {receipt.qty} × {rupees(receipt.unit_price_paise)}
          </dd>
        </dl>
      </div>
    );
  }

  return (
    <div>
      <p className="font-semibold text-danger">
        {hasVariants ? `${label} ${variant} is out of stock` : "Out of stock"}
      </p>
      <p className="mt-1 text-sm text-muted">{backOn}.</p>

      {!canReserve ? (
        <p className="mt-3 text-sm text-muted">
          {brand.name} does not hold reservations.{" "}
          {hasVariants
            ? `Pick another ${label.toLowerCase()} above, or check back after the restock date.`
            : "Check back after the restock date."}
        </p>
      ) : (
        <form onSubmit={reserve} className="mt-4 border-t border-line pt-4">
          <p className="text-sm font-semibold">Reserve it instead</p>
          <p className="mt-1 text-sm text-muted">
            We will hold one from the next run and tell you when it lands. No payment now — you
            approve the purchase when it is back.
          </p>

          <div className="mt-3 flex flex-wrap items-center gap-3">
            <QtyStepper value={qty} onChange={setQty} disabled={stage === "sending"} compact />
            <label className="sr-only" htmlFor="contact">
              Email or phone
            </label>
            <input
              id="contact"
              value={contact}
              onChange={(e) => setContact(e.target.value)}
              disabled={stage === "sending"}
              placeholder="Email or phone"
              className="min-w-48 flex-1 rounded-control border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-brand"
            />
          </div>

          <button
            type="submit"
            disabled={stage === "sending" || !contact.trim()}
            className="mt-3 w-full rounded-control bg-brand py-3 font-semibold text-white hover:bg-brand-deep disabled:opacity-50"
          >
            {stage === "sending"
              ? "Reserving…"
              : `Reserve ${qty} in ${label.toLowerCase()} ${variant}`}
          </button>

          {error && (
            <p className="mt-3 bg-danger-soft px-3 py-2 text-sm text-danger">{error}</p>
          )}
        </form>
      )}
    </div>
  );
}
