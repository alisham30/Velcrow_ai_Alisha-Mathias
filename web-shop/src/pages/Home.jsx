import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { shop } from "../api.js";
import { brand, variantLabel } from "../brand.js";
import { rupees } from "../money.js";
import { useCart } from "../store.jsx";
import ProductVisual from "../components/ProductVisual.jsx";
import { SkeletonGrid, ErrorBanner } from "../components/States.jsx";

function stockLabel(p, label) {
  if (p.variants) {
    const inStock = p.variants.filter((v) => v.stock > 0);
    if (inStock.length === 0) return { text: "Sold out", tone: "out" };
    if (inStock.length < p.variants.length)
      return { text: `${inStock.map((v) => v.label).join(" ")} left`, tone: "part" };
    return { text: `All ${label.toLowerCase()}s`, tone: "in" };
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

function ProductCard({ p, label }) {
  const stock = stockLabel(p, label);
  const editorial = brand.cardStyle === "editorial";
  return (
    <Link
      to={`/product/${p.id}`}
      className={
        editorial
          ? "group flex flex-col"
          : "group flex flex-col rounded-card border border-line bg-card p-4 transition hover:border-brand"
      }
    >
      <ProductVisual
        product={p}
        className={`w-full ${editorial ? "mb-3" : "mb-4"}`}
        style={{ aspectRatio: brand.cardAspect }}
      />
      {editorial ? (
        <>
          <h3 className="font-display text-lg leading-snug group-hover:text-accent">{p.name}</h3>
          <div className="mt-1 flex items-baseline justify-between">
            <span className="text-sm">{rupees(p.price_paise)}</span>
            <span className={`brand-label text-xs ${TONE_CLASS[stock.tone]}`}>{stock.text}</span>
          </div>
        </>
      ) : (
        <>
          <h3 className="font-semibold leading-snug group-hover:text-brand">{p.name}</h3>
          <p className="mt-1 line-clamp-2 text-sm text-muted">{p.description}</p>
          <div className="mt-3 flex items-baseline justify-between">
            <span className="font-display text-xl">{rupees(p.price_paise)}</span>
            <span className={`text-xs font-semibold ${TONE_CLASS[stock.tone]}`}>{stock.text}</span>
          </div>
        </>
      )}
    </Link>
  );
}

export default function Home() {
  const { caps } = useCart();
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

  const label = variantLabel(caps);
  const byCategory = {};
  (products || []).forEach((p) => {
    (byCategory[p.category] = byCategory[p.category] || []).push(p);
  });
  const categories = brand.categoryOrder.filter((c) => byCategory[c]);
  const editorial = brand.cardStyle === "editorial";

  return (
    <main className="mx-auto max-w-6xl px-4">
      <section
        className={
          editorial
            ? "my-10 border-b border-line pb-10"
            : "my-8 overflow-hidden rounded-card border border-line bg-card"
        }
      >
        <div
          className={
            editorial
              ? "grid gap-8 md:grid-cols-[1fr_1fr] md:items-end"
              : "grid gap-6 p-8 sm:p-12 md:grid-cols-[1.3fr_1fr] md:items-center"
          }
        >
          <div>
            <p
              className={
                editorial
                  ? "brand-label mb-4 text-xs font-semibold text-accent"
                  : "brand-label mb-3 inline-block rounded-control bg-accent-soft px-3 py-1 text-xs font-bold text-accent-ink"
              }
            >
              {brand.heroBadge(rupees(39900))}
            </p>
            <h1
              className={`font-display leading-tight ${
                editorial ? "text-5xl sm:text-6xl" : "text-4xl sm:text-5xl"
              }`}
            >
              {brand.tagline}
            </h1>
            <p className="mt-4 max-w-md text-muted">{brand.subline}</p>
          </div>
          <div className={editorial ? "grid grid-cols-2 gap-4" : "hidden gap-3 md:grid md:grid-cols-2"}>
            {(products || []).slice(0, editorial ? 2 : 4).map((p) => (
              <ProductVisual
                key={p.id}
                product={p}
                className="w-full"
                style={{ aspectRatio: brand.cardAspect }}
              />
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
            <div
              className={`mb-5 flex items-baseline justify-between pb-2 ${
                editorial ? "" : "border-b border-line"
              }`}
            >
              <h2
                className={
                  editorial
                    ? "brand-label text-sm font-semibold"
                    : "font-display text-2xl font-semibold"
                }
              >
                {brand.categoryLabels[cat] || cat}
              </h2>
              <span className="text-sm text-muted">{byCategory[cat].length} {brand.unitNoun}</span>
            </div>
            <div className={`grid ${brand.gridClass}`}>
              {byCategory[cat].map((p) => (
                <ProductCard key={p.id} p={p} label={label} />
              ))}
            </div>
          </section>
        ))}
    </main>
  );
}
