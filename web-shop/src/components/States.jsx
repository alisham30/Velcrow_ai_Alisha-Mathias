import React from "react";

export function SkeletonGrid({ n = 8 }) {
  return (
    <div className="grid grid-cols-2 gap-5 sm:grid-cols-3 lg:grid-cols-4">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="rounded-xl border border-line bg-card p-4">
          <div className="skeleton mb-4 aspect-square w-full" />
          <div className="skeleton mb-2 h-4 w-3/4" />
          <div className="skeleton h-4 w-1/3" />
        </div>
      ))}
    </div>
  );
}

export function ErrorBanner({ text, onRetry }) {
  return (
    <div className="mx-auto my-16 max-w-md rounded-xl border border-danger/30 bg-danger-soft p-6 text-center">
      <p className="font-display text-lg font-semibold text-danger">Something went off the rails</p>
      <p className="mt-2 text-sm text-ink/80">{text}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-4 rounded-lg bg-danger px-4 py-2 text-sm font-semibold text-white hover:opacity-90"
        >
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyBasket({ onShop }) {
  return (
    <div className="flex flex-col items-center py-14 text-center">
      <svg width="72" height="72" viewBox="0 0 72 72" aria-hidden="true">
        <path
          d="M14 30h44l-5 26a4 4 0 0 1-4 3H23a4 4 0 0 1-4-3l-5-26z"
          fill="none" stroke="var(--muted)" strokeWidth="2.5" strokeLinejoin="round"
        />
        <path d="M26 30l8-16m12 16l-8-16" fill="none" stroke="var(--muted)" strokeWidth="2.5" strokeLinecap="round" />
        <circle cx="30" cy="44" r="2" fill="var(--muted)" />
        <circle cx="42" cy="44" r="2" fill="var(--muted)" />
      </svg>
      <p className="mt-4 font-display text-lg font-semibold">Your basket is empty</p>
      <p className="mt-1 text-sm text-muted">Everything you add shows up here with its quantity.</p>
      {onShop && (
        <button
          onClick={onShop}
          className="mt-5 rounded-lg bg-brand px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-deep"
        >
          Browse the shop
        </button>
      )}
    </div>
  );
}
