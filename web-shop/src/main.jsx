import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { SHOP, brand } from "./brand.js";
import { CartProvider } from "./store.jsx";
import Header from "./components/Header.jsx";
import CartDrawer from "./components/CartDrawer.jsx";
import SplitNotice from "./components/SplitNotice.jsx";
import Home from "./pages/Home.jsx";
import Product from "./pages/Product.jsx";
import Checkout from "./pages/Checkout.jsx";
import Console from "./pages/Console.jsx";
import Orders from "./pages/Orders.jsx";
import "./index.css";

// Each shop loads only its own typefaces, so the brands never share a face.
document.documentElement.dataset.brand = SHOP;
document.title = brand.name;
const fontLink = document.createElement("link");
fontLink.rel = "stylesheet";
fontLink.href = brand.fontHref;
document.head.appendChild(fontLink);

// The shopper's shop: header, cart drawer, the widget in the corner.
function Storefront({ children }) {
  return (
    <CartProvider>
      <Header />
      {children}
      <CartDrawer />
      <SplitNotice />
      <footer className="mt-20 border-t border-line py-8 text-center text-sm text-muted">
        {brand.name} — {brand.footerNote}. Razorpay test mode; no real money moves.
      </footer>
    </CartProvider>
  );
}

// The merchant's console is the same business but not the same room: no
// basket, no cart drawer, nothing that belongs to a shopper (spec 6.2).
function Backoffice({ children }) {
  return (
    <>
      {children}
      <footer className="mt-20 border-t border-line py-8 text-center text-sm text-muted">
        {brand.name} merchant console — your shop's data only.
      </footer>
    </>
  );
}

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-paper text-ink">
        <Routes>
          <Route path="/" element={<Storefront><Home /></Storefront>} />
          <Route path="/product/:id" element={<Storefront><Product /></Storefront>} />
          <Route path="/checkout" element={<Storefront><Checkout /></Storefront>} />
          <Route path="/orders" element={<Storefront><Orders /></Storefront>} />
          <Route path="/console" element={<Backoffice><Console /></Backoffice>} />
        </Routes>
      </div>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
