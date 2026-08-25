import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { shop } from "../api.js";
import { brand } from "../brand.js";
import { rupees } from "../money.js";
import { useCart } from "../store.jsx";
import ProductVisual from "../components/ProductVisual.jsx";
import QtyStepper from "../components/QtyStepper.jsx";
import { ErrorBanner } from "../components/States.jsx";

export default function Product() {
  const { id } = useParams();
  const { add, busy } = useCart();
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
        <div className="skeleton aspect-square w-full" />
        <div>
          <div className="skeleton mb-4 h-9 w-3/4" />
          <div className="skeleton mb-2 h-4 w-full" />
          <div className="skeleton mb-8 h-4 w-2/3" />
          <div className="skeleton h-12 w-40" />
        </div>
      </main>
    );
  }

  const selected = product.variants
    ? product.variants.find((v) => v.label === variant)
    : { stock: product.stock, restock_date: product.restock_date };
  const inStock = selected && selected.stock > 0;
  const maxQty = selected ? selected.stock : 0;

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <nav className="mb-6 text-sm text-muted">
        <Link to="/" className="hover:text-brand">
          {brand.name}
        </Link>
        <span className="mx-2">/</span>
        <span>{brand.categoryLabels[product.category] || product.category}</span>
      </nav>

      <div className="grid gap-10 md:grid-cols-2">
        <ProductVisual product={product} className="aspect-square w-full" />

        <div>
          <h1 className="font-display text-3xl font-bold leading-tight sm:text-4xl">
            {product.name}
          </h1>
          <p className="mt-3 leading-relaxed text-muted">{product.description}</p>

          <p className="mt-6 font-display text-3xl font-bold">{rupees(product.price_paise)}</p>
          {product.exact_only && (
            <p className="mt-2 inline-block rounded-md bg-accent-soft px-2 py-1 text-xs font-semibold text-brand-deep">
              Exact item only — no substitutions
            </p>
          )}

          {product.variants && (
            <div className="mt-7">
              <p className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted">
                {brand.variantLabel}
              </p>
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
                      className={`min-w-14 rounded-lg border px-4 py-2 text-sm font-semibold transition ${
                        isSel
                          ? "border-brand bg-brand text-white"
                          : out
                            ? "border-line bg-paper text-muted line-through"
                            : "border-line bg-card hover:border-brand"
                      }`}
                    >
                      {v.label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          <div className="mt-7 rounded-xl border border-line bg-card p-5">
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
                    className="flex-1 rounded-lg bg-brand px-6 py-3 font-semibold text-white hover:bg-brand-deep disabled:opacity-60"
                  >
                    {busy ? "Adding…" : `Add ${qty} to basket`}
                  </button>
                </div>
                <p className="mt-3 text-sm text-muted">
                  Line total {rupees(product.price_paise * qty)}
                </p>
                {added && (
                  <p className="mt-3 rounded-lg bg-ok-soft px-3 py-2 text-sm font-medium text-brand-deep">
                    Added to your basket.
                  </p>
                )}
              </>
            ) : (
              <div>
                <p className="font-semibold text-danger">
                  {brand.variantLabel} {variant || ""} is out of stock
                </p>
                <p className="mt-1 text-sm text-muted">
                  {selected && selected.restock_date
                    ? `Expected back on ${selected.restock_date}.`
                    : "No restock date scheduled yet."}
                </p>
                {product.variants && (
                  <p className="mt-3 text-sm text-muted">
                    Pick another {brand.variantLabel.toLowerCase()} above to continue.
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </main>
  );
}
