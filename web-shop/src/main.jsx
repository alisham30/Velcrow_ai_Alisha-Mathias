import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { SHOP, brand } from "./brand.js";
import { CartProvider } from "./store.jsx";
import Header from "./components/Header.jsx";
import CartDrawer from "./components/CartDrawer.jsx";
import Home from "./pages/Home.jsx";
import Product from "./pages/Product.jsx";
import Checkout from "./pages/Checkout.jsx";
import "./index.css";

// Each shop loads only its own typefaces, so the brands never share a face.
document.documentElement.dataset.brand = SHOP;
document.title = brand.name;
const fontLink = document.createElement("link");
fontLink.rel = "stylesheet";
fontLink.href = brand.fontHref;
document.head.appendChild(fontLink);

function App() {
  return (
    <BrowserRouter>
      <CartProvider>
        <div className="min-h-screen bg-paper text-ink">
          <Header />
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/product/:id" element={<Product />} />
            <Route path="/checkout" element={<Checkout />} />
          </Routes>
          <CartDrawer />
          <footer className="mt-20 border-t border-line py-8 text-center text-sm text-muted">
            {brand.name} — {brand.footerNote}. Razorpay test mode; no real money moves.
          </footer>
        </div>
      </CartProvider>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
