import React from "react";

// The catalog's image_url files don't exist in this demo, so each product
// gets a deterministic, category-tinted tile with a monogram — designed, not
// a broken <img>. Swap for real photography by serving the image_url paths.
const CATEGORY_TINTS = {
  produce: "#dcead9",
  staples: "#f0e6cc",
  dairy: "#f3ecdd",
  packaged: "#fdf3d7",
  baby: "#f6e3de",
  women: "#ecdfe3",
  men: "#dde4e8",
  unisex: "#e7e3da",
};

export default function ProductVisual({ product, className = "" }) {
  const tint = CATEGORY_TINTS[product.category] || "#e8e4d8";
  const initials = product.name
    .split(" ")
    .filter((w) => /^[A-Za-z]/.test(w))
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");
  return (
    <div
      className={`relative flex items-center justify-center overflow-hidden rounded-lg ${className}`}
      style={{ background: tint }}
      aria-hidden="true"
    >
      <svg className="absolute inset-0 h-full w-full opacity-40" aria-hidden="true">
        <defs>
          <pattern id={`dots-${product.id}`} width="14" height="14" patternUnits="userSpaceOnUse">
            <circle cx="2" cy="2" r="1" fill="rgba(0,0,0,0.10)" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill={`url(#dots-${product.id})`} />
      </svg>
      <span className="font-display text-4xl font-semibold text-ink/45 select-none">{initials}</span>
    </div>
  );
}
