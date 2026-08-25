import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { shop } from "../api.js";
import { brand } from "../brand.js";
import { rupees } from "../money.js";
import ProductVisual from "../components/ProductVisual.jsx";
import { SkeletonGrid, ErrorBanner } from "../components/States.jsx";

function stockLabel(p) {
  if (p.variants) {
    const total = p.variants.reduce((n, v) => n + v.stock, 0);
    if (total === 0) return { text: "Out of stock", tone: "out" };
    const outCount = p.variants.filter((v) => v.stock === 0).length;
    if (outCount > 0) return { text: `${p.variants.length - outCount} of ${p.variants.length} packs available`, tone: "part" };
    return { text: "In stock", tone: "in" };
  }
  if (p.stock === 0) return { text: p.restock_date ? `Back ${p.restock_date}` : "Out of stock", tone: "out" };
  if (p.stock <= 6) return { text: `Only ${p.stock} left`, tone: "low" };
  return { text: "In stock", tone: "in" };
}

const TONE_CLASS = {
  in: "text-brand",
  low: "text-accent-ink",
  part: "text-muted",
  out: "text-danger",
};

function ProductCard({ p }) {
  const stock = stockLabel(p);
  return (
    <Link
      to={`/product/${p.id}`}
      className="group flex flex-col rounded-xl border border-line bg-card p-4 transition hover:border-brand"
    >
      <ProductVisual product={p} className="mb-4 aspect-square w-full" />
      <h3 className="font-semibold leading-snug group-hover:text-brand">{p.name}</h3>
      <p className="mt-1 line-clamp-2 text-sm text-muted">{p.description}</p>
      <div className="mt-3 flex items-baseline justify-between">
        <span className="font-display text-xl font-bold">{rupees(p.price_paise)}</span>
        <span className={`text-xs font-semibold ${TONE_CLASS[stock.tone]}`}>{stock.text}</span>
      </div>
    </Link>
  );
}

export default function Home() {
  const [products, setProducts] = useState(null);
  const [error, setError] = useState(null);

  const load = () => {
    setError(null);
    setProducts(null);
    shop
      .catalog()
      .then(setProducts)
      .catch((e) => setError(e.why || e.message));
  };

  useEffect(load, []);

  const byCategory = {};
  (products || []).forEach((p) => {
    (byCategory[p.category] = byCategory[p.category] || []).push(p);
  });
  const categories = brand.categoryOrder.filter((c) => byCategory[c]);

  return (
    <main className="mx-auto max-w-6xl px-4">
      <section className="my-8 overflow-hidden rounded-2xl border border-line bg-card">
        <div className="grid gap-6 p-8 sm:p-12 md:grid-cols-[1.3fr_1fr] md:items-center">
          <div>
            <p className="mb-3 inline-block rounded-full bg-accent-soft px-3 py-1 text-xs font-bold uppercase tracking-wider text-brand-deep">
              Free delivery over {rupees(39900)}
            </p>
            <h1 className="font-display text-4xl font-bold leading-tight sm:text-5xl">
              {brand.tagline}
            </h1>
            <p className="mt-4 max-w-md text-muted">{brand.subline}</p>
          </div>
          <div className="hidden gap-3 md:grid md:grid-cols-2">
            {(products || []).slice(0, 4).map((p) => (
              <ProductVisual key={p.id} product={p} className="aspect-square w-full" />
            ))}
          </div>
        </div>
      </section>

      {error && <ErrorBanner text={error} onRetry={load} />}

      {!products && !error && (
        <section className="my-10">
          <div className="skeleton mb-5 h-7 w-48" />
          <SkeletonGrid />
        </section>
      )}

      {products &&
        categories.map((cat) => (
          <section key={cat} className="my-12">
            <div className="mb-5 flex items-baseline justify-between border-b border-line pb-2">
              <h2 className="font-display text-2xl font-semibold">
                {brand.categoryLabels[cat] || cat}
              </h2>
              <span className="text-sm text-muted">{byCategory[cat].length} items</span>
            </div>
            <div className="grid grid-cols-2 gap-5 sm:grid-cols-3 lg:grid-cols-4">
              {byCategory[cat].map((p) => (
                <ProductCard key={p.id} p={p} />
              ))}
            </div>
          </section>
        ))}
    </main>
  );
}
