import React from "react";
import { Link } from "react-router-dom";
import { brand } from "../brand.js";
import { useCart } from "../store.jsx";
import SignIn from "./SignIn.jsx";

export default function Header() {
  const { count, setDrawerOpen } = useCart();
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-paper/95 backdrop-blur-none">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
        <Link to="/" className="flex items-baseline gap-2">
          <span className="font-display text-2xl text-brand">{brand.name}</span>
          <span className="brand-label hidden text-xs font-medium text-muted sm:inline">
            {brand.strapline}
          </span>
        </Link>
        <div className="flex items-center gap-2">
        <SignIn />
        <button
          onClick={() => setDrawerOpen(true)}
          className="relative flex items-center gap-2 rounded-control border border-line bg-card px-4 py-2 text-sm font-semibold hover:border-brand"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M4 8h16l-1.5 11a2 2 0 0 1-2 1.8h-9A2 2 0 0 1 5.5 19L4 8zM8 8l2.8-5M16 8l-2.8-5"
              stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
            />
          </svg>
          Basket
          {count > 0 && (
            <span className="absolute -right-2 -top-2 flex h-5 min-w-5 items-center justify-center rounded-full bg-accent px-1 text-xs font-bold text-brand-deep">
              {count}
            </span>
          )}
        </button>
        </div>
      </div>
    </header>
  );
}
