import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// One codebase, two brands: VITE_SHOP=grocery -> FreshKart :5173,
// VITE_SHOP=apparel -> Loomcraft :5174 (spec 2).
const shop = process.env.VITE_SHOP || "grocery";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: shop === "apparel" ? 5174 : 5173,
    strictPort: true,
  },
});
