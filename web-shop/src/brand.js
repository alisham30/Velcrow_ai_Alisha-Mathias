// One codebase, two brands (spec 2, 6.2). The brand is selected purely by
// environment; nothing else in the app knows which shop it is.
const BRANDS = {
  grocery: {
    shopId: "freshkart",
    name: "FreshKart",
    tagline: "Sabzi, staples & the good stuff — picked fresh this morning.",
    subline: "Same-day delivery across the city. No minimums on your first order.",
    apiBase: "http://127.0.0.1:8001",
    categoryOrder: ["produce", "staples", "dairy", "packaged", "baby"],
    categoryLabels: {
      produce: "Fresh produce",
      staples: "Staples",
      dairy: "Dairy",
      packaged: "Pantry",
      baby: "Baby care",
    },
    variantLabel: "Pack",
  },
  apparel: {
    shopId: "loomcraft",
    name: "Loomcraft",
    tagline: "Handloom-first everyday wear.",
    subline: "Small batches, honest fabric, sizes that fit real people.",
    apiBase: "http://127.0.0.1:8002",
    categoryOrder: ["women", "men", "unisex"],
    categoryLabels: { women: "Women", men: "Men", unisex: "Everyone" },
    variantLabel: "Size",
  },
};

export const SHOP = import.meta.env.VITE_SHOP || "grocery";
export const brand = BRANDS[SHOP];
export const TRUST_BASE = "http://127.0.0.1:8003";
