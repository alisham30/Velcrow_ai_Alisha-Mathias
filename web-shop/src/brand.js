// One codebase, two brands (spec 2, 6.2, 11). The brand is selected purely by
// environment; nothing else in the app knows which shop it is. Identity here
// covers typography, copy tone and card rhythm — not just colour — so the two
// shops read as unrelated businesses.
const BRANDS = {
  grocery: {
    shopKey: "grocery",
    shopId: "freshkart",
    name: "FreshKart",
    apiBase: "http://127.0.0.1:8001",
    // typography
    fontHref:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=Public+Sans:wght@400;500;600;700&display=swap",
    // copy
    strapline: "EST. 2026 · CITYWIDE DELIVERY",
    tagline: "Sabzi, staples & the good stuff — picked fresh this morning.",
    subline: "Same-day delivery across the city. No minimums on your first order.",
    heroBadge: (freeOver) => `Free delivery over ${freeOver}`,
    checkoutNote: "No address forms, no card forms. Payment runs through your VelcrowAI mandate.",
    footerNote: "a demo shop on the VelcrowAI trust layer",
    emptyBasketLine: "Everything you add shows up here with its quantity.",
    // rhythm
    cardAspect: "1 / 1",
    gridClass: "grid-cols-2 gap-5 sm:grid-cols-3 lg:grid-cols-4",
    cardStyle: "boxed", // bordered card, image inside
    categoryOrder: ["produce", "staples", "dairy", "packaged", "baby"],
    categoryLabels: {
      produce: "Fresh produce",
      staples: "Staples",
      dairy: "Dairy",
      packaged: "Pantry",
      baby: "Baby care",
    },
    variantLabelFallback: "Pack",
    variantHelp: null,
    unitNoun: "items",
  },
  apparel: {
    shopKey: "apparel",
    shopId: "loomcraft",
    name: "Loomcraft",
    apiBase: "http://127.0.0.1:8002",
    fontHref:
      "https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600&family=Jost:wght@300;400;500;600&display=swap",
    strapline: "HANDLOOM STUDIO · SINCE 2026",
    tagline: "Cloth with a hand in it.",
    subline:
      "Small-batch handloom and natural fibre, cut for real bodies. Woven in limited runs — when a size goes, it goes.",
    heroBadge: () => "New season · limited runs",
    checkoutNote: "No address forms, no card forms. Checkout is settled through your VelcrowAI mandate.",
    footerNote: "a handloom studio running on the VelcrowAI trust layer",
    emptyBasketLine: "Pieces you choose are held here until you check out.",
    cardAspect: "3 / 4",
    gridClass: "grid-cols-2 gap-x-6 gap-y-10 lg:grid-cols-3",
    cardStyle: "editorial", // no border, image leads, quiet type beneath
    categoryOrder: ["women", "men", "unisex"],
    categoryLabels: { women: "Women", men: "Men", unisex: "Everyone" },
    variantLabelFallback: "Size",
    unitNoun: "pieces",
    variantHelp: "Runs true to size. Handloom fabric relaxes slightly after the first wash.",
  },
};

export const SHOP = import.meta.env.VITE_SHOP || "grocery";
export const brand = BRANDS[SHOP];
export const TRUST_BASE = "http://127.0.0.1:8003";

// The shop tells us what it supports (spec 6.6). `variants: "size" | "pack"`
// drives the selector wording, so the same code reads native at either shop.
export function variantLabel(caps) {
  const kind = caps && caps.variants;
  if (kind === "size") return "Size";
  if (kind === "pack") return "Pack";
  return brand.variantLabelFallback;
}
