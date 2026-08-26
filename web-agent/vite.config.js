import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The consumer app (spec 2): buyer chat at / and, from Phase 8, the audit view
// at /audit. It belongs to the shopper, so it is not branded as either shop.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5175, strictPort: true },
});
