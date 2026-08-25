import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { shop, ApiError } from "./api.js";
import { brand } from "./brand.js";

const CartContext = createContext(null);
const CART_KEY = `velcrow-cart-${brand.shopId}`;

export function CartProvider({ children }) {
  const [cart, setCart] = useState(null); // {cart_id, items, subtotal_paise}
  const [quote, setQuote] = useState(null); // coupons evaluation for the cart
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null); // {kind: "error"|"info", text}
  const [caps, setCaps] = useState(null); // what this shop supports (spec 6.6)

  const refreshQuote = useCallback(async (view) => {
    if (!view || view.items.length === 0) {
      setQuote(null);
      return;
    }
    try {
      setQuote(await shop.coupons(view.cart_id));
    } catch {
      setQuote(null); // coupons are a bonus; never block the cart on them
    }
  }, []);

  const ensureCart = useCallback(async () => {
    const saved = localStorage.getItem(CART_KEY);
    if (saved) {
      try {
        const view = await shop.getCart(saved);
        setCart(view);
        refreshQuote(view);
        return view;
      } catch {
        localStorage.removeItem(CART_KEY); // server was reset; start fresh
      }
    }
    const created = await shop.createCart();
    localStorage.setItem(CART_KEY, created.cart_id);
    setCart(created);
    setQuote(null);
    return created;
  }, [refreshQuote]);

  // Ask the shop what it supports before offering capability-gated UI.
  useEffect(() => {
    shop
      .capabilities()
      .then(setCaps)
      .catch(() => setCaps({}));
  }, []);

  useEffect(() => {
    ensureCart().catch(() =>
      setNotice({ kind: "error", text: `Could not reach the ${brand.name} shop API on ${brand.apiBase}.` }),
    );
  }, [ensureCart]);

  // The widget drives this same cart through the shop API, then tells the
  // page to refetch. There is no second cart anywhere (spec 6.3).
  useEffect(() => {
    const onAgentChange = () => {
      const saved = localStorage.getItem(CART_KEY);
      if (!saved) return;
      shop
        .getCart(saved)
        .then((view) => {
          setCart(view);
          refreshQuote(view);
        })
        .catch(() => {});
    };
    window.addEventListener("velcrow:cart-changed", onAgentChange);
    return () => window.removeEventListener("velcrow:cart-changed", onAgentChange);
  }, [refreshQuote]);

  const mutate = useCallback(
    async (op) => {
      setBusy(true);
      try {
        const current = cart || (await ensureCart());
        const view = await shop.patchCart(current.cart_id, op);
        setCart(view);
        refreshQuote(view);
        return view;
      } catch (e) {
        if (e instanceof ApiError && e.code === "OUT_OF_STOCK") {
          const back = e.payload.restock_date ? ` Back on ${e.payload.restock_date}.` : "";
          const left = Number.isInteger(e.payload.in_stock)
            ? ` Only ${e.payload.in_stock} in stock.`
            : "";
          setNotice({ kind: "error", text: `That much isn't available.${left}${back}` });
        } else {
          setNotice({ kind: "error", text: e.why || e.message });
        }
        throw e;
      } finally {
        setBusy(false);
      }
    },
    [cart, ensureCart, refreshQuote],
  );

  const add = useCallback(
    async (itemId, variant, qty) => {
      await mutate({ op: "add", item_id: itemId, variant: variant || "", qty });
      setDrawerOpen(true);
    },
    [mutate],
  );

  const updateQty = useCallback((lineId, qty) => mutate({ op: "update", line_id: lineId, qty }), [mutate]);
  const removeLine = useCallback((lineId) => mutate({ op: "remove", line_id: lineId }), [mutate]);

  const resetCart = useCallback(async () => {
    localStorage.removeItem(CART_KEY);
    setCart(null);
    setQuote(null);
    await ensureCart();
  }, [ensureCart]);

  const count = cart ? cart.items.reduce((n, l) => n + l.qty, 0) : 0;

  return (
    <CartContext.Provider
      value={{ cart, quote, count, busy, caps, drawerOpen, setDrawerOpen, add, updateQty, removeLine, resetCart, notice, setNotice, ensureCart }}
    >
      {children}
    </CartContext.Provider>
  );
}

export function useCart() {
  return useContext(CartContext);
}
