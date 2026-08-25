import React from "react";
import { brand } from "../brand.js";

// The catalog's image_url files don't exist in this demo, so each product gets
// a deterministic, category-tinted tile with a monogram — designed, not a
// broken <img>. Swap for real photography by serving the image_url paths.
const CATEGORY_TINTS = {
  produce: "#dcead9",
  staples: "#f0e6cc",
  dairy: "#f3ecdd",
  packaged: "#fdf3d7",
  baby: "#f6e3de",
  women: "#e3ded3",
  men: "#d8dcd9",
  unisex: "#e9e2d6",
};

export default function ProductVisual({ product, className = "", style }) {
  const tint = CATEGORY_TINTS[product.category] || "#e8e4d8";
  const editorial = brand.cardStyle === "editorial";
  const initials = product.name
    .split(" ")
    .filter((w) => /^[A-Za-z]/.test(w))
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");
  return (
    <div
      className={`relative flex items-center justify-center overflow-hidden rounded-card ${className}`}
      style={{ background: tint, ...style }}
      aria-hidden="true"
    >
      <svg className="absolute inset-0 h-full w-full opacity-40" aria-hidden="true">
        <defs>
          {editorial ? (
            // woven warp/weft hatch for the handloom shop
            <pattern id={`weave-${product.id}`} width="8" height="8" patternUnits="userSpaceOnUse">
              <path d="M0 0h8v8H0z" fill="none" />
              <path d="M0 4h8M4 0v8" stroke="rgba(0,0,0,0.07)" strokeWidth="1" />
            </pattern>
          ) : (
            <pattern id={`dots-${product.id}`} width="14" height="14" patternUnits="userSpaceOnUse">
              <circle cx="2" cy="2" r="1" fill="rgba(0,0,0,0.10)" />
            </pattern>
          )}
        </defs>
        <rect
          width="100%"
          height="100%"
          fill={`url(#${editorial ? "weave" : "dots"}-${product.id})`}
        />
      </svg>
      <span
        className={`font-display select-none text-ink/45 ${
          editorial ? "brand-label text-2xl" : "text-4xl"
        }`}
      >
        {initials}
      </span>
    </div>
  );
}
