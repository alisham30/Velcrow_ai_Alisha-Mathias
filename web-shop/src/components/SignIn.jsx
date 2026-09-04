import React, { useEffect, useState } from "react";
import { shop, trust } from "../api.js";
import { shopperKey } from "../shopperKey.js";
import { rupees } from "../money.js";
import { brand } from "../brand.js";

/* One person, one basket. If the agent already holds a basket for this
 * contact (built over WhatsApp, say) and this browser's cart is empty or
 * absent, adopt the agent's cart so both surfaces show the same goods. */
async function joinKnownBasket(contactKey) {
  try {
    const known = await trust.activeBasket(contactKey);
    if (!known.cart_id) return;
    const key = `velcrow-cart-${brand.shopId}`;
    const local = localStorage.getItem(key);
    if (local === known.cart_id) return;
    let adopt = !local;
    if (!adopt) {
      const view = await shop.getCart(local).catch(() => null);
      adopt = !view || view.items.length === 0;
    }
    if (adopt) {
      localStorage.setItem(key, known.cart_id);
      window.dispatchEvent(new Event("velcrow:cart-changed"));
    }
  } catch {
    /* a missing basket lookup must never break login */
  }
}

/* Login the way a real Indian shop does it: phone, six digits, in - and the
 * code arrives on WhatsApp from the SAME agent that will message about carts
 * and restocks. Never a password (spec 14's actual concern: nothing to
 * breach), and deliberately not a key to money: verified or not, paying still
 * takes the exact-amount approval through the wallet.
 *
 * An email still works as the old no-password "remember me" - there is no
 * WhatsApp to deliver a code to.
 */
export default function SignIn() {
  const [contact, setContact] = useState(() => shopperKey.contact());
  const [verified, setVerified] = useState(() => shopperKey.verified());
  const [typed, setTyped] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState("contact"); // contact -> code
  const [sendMode, setSendMode] = useState(""); // sent | outbox | failed
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [last, setLast] = useState(null);

  // Restore on every load, so a refresh keeps you known rather than anonymous.
  useEffect(() => {
    if (!contact) return;
    let gone = false;
    shop
      .identify(contact)
      .then((who) => {
        if (gone) return null;
        joinKnownBasket(who.contact_key);
        return shop.lastOrder(who.contact_key);
      })
      .then((order) => !gone && setLast(order))
      .catch(() => !gone && setLast(null));
    return () => {
      gone = true;
    };
  }, [contact]);

  function reset() {
    setStep("contact");
    setTyped("");
    setCode("");
    setError(null);
    setSendMode("");
  }

  async function sendCode(e) {
    e.preventDefault();
    const text = typed.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (text.includes("@")) {
        // Email: the claim-only path. Honest label, no fake OTP.
        const who = await shop.identify(text);
        shopperKey.remember(who.contact_ref);
        setContact(who.contact_ref);
        setVerified(false);
        setOpen(false);
        reset();
        return;
      }
      const out = await trust.loginStart(text);
      setSendMode(out.mode);
      setStep("code");
    } catch (err) {
      setError(err.why || err.message);
    } finally {
      setBusy(false);
    }
  }

  async function verifyCode(e) {
    e.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await trust.loginVerify(typed.trim(), code.trim());
      const who = await shop.identify(typed.trim());
      shopperKey.remember(who.contact_ref);
      shopperKey.rememberVerified();
      joinKnownBasket(who.contact_key);
      setContact(who.contact_ref);
      setVerified(true);
      setOpen(false);
      reset();
    } catch (err) {
      setError(err.why || err.message);
    } finally {
      setBusy(false);
    }
  }

  function forget() {
    shopperKey.forget();
    setContact("");
    setVerified(false);
    setLast(null);
    setOpen(false);
    reset();
  }

  if (contact) {
    return (
      <div className="relative">
        <button
          onClick={() => setOpen((v) => !v)}
          className="rounded-control border border-line bg-card px-3 py-2 text-sm font-medium hover:border-brand"
        >
          <span className="hidden sm:inline text-muted">
            {verified ? "✓ Logged in · " : "Signed in · "}
          </span>
          {contact}
        </button>
        {open && (
          <div className="absolute right-0 z-40 mt-2 w-72 rounded-card border border-line bg-card p-4 shadow-xl">
            <p className="brand-label text-xs font-semibold text-muted">Your orders</p>
            {last && last.lines ? (
              <>
                <p className="mt-2 text-sm">
                  Last order · {last.lines.length} item{last.lines.length === 1 ? "" : "s"}
                </p>
                <ul className="mt-2 space-y-1 text-xs text-muted">
                  {last.lines.slice(0, 4).map((l) => (
                    <li key={l.item_id + l.variant}>
                      {l.name}
                      {l.variant ? ` · ${l.variant}` : ""} × {l.qty}
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-sm font-semibold">
                  {rupees(last.now_subtotal_paise)} at today&rsquo;s prices
                </p>
              </>
            ) : (
              <p className="mt-2 text-sm text-muted">
                Nothing bought under this contact here yet. Once you buy something it stays
                findable, on this device or any other.
              </p>
            )}
            <p className="mt-3 text-xs leading-relaxed text-muted">
              {verified
                ? "Number verified by WhatsApp code. That unlocks history and reminders — money still needs your approval on an exact amount."
                : "Contact remembered without verification. It unlocks history; it can never move money."}
            </p>
            <button
              onClick={forget}
              className="mt-4 w-full rounded-control border border-line px-3 py-2 text-xs font-semibold text-muted hover:border-danger hover:text-danger"
            >
              Log out on this device
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="rounded-control border border-line bg-card px-3 py-2 text-sm font-medium text-muted hover:border-brand hover:text-ink"
      >
        Login
      </button>
      {open && step === "contact" && (
        <form
          onSubmit={sendCode}
          className="absolute right-0 z-40 mt-2 w-72 rounded-card border border-line bg-card p-4 shadow-xl"
        >
          <p className="brand-label text-xs font-semibold text-muted">Login</p>
          <p className="mt-2 text-xs leading-relaxed text-muted">
            Phone number gets a 6-digit code on WhatsApp — no password, ever. An email is
            simply remembered.
          </p>
          <input
            autoFocus
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder="Phone number"
            className="mt-3 w-full rounded-control border border-line px-3 py-2 text-sm outline-none focus:border-brand"
          />
          {error && <p className="mt-2 text-xs text-danger">{error}</p>}
          <button
            type="submit"
            disabled={busy || !typed.trim()}
            className="mt-3 w-full rounded-control bg-brand px-3 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-50"
          >
            {busy ? "Sending…" : "Send code on WhatsApp"}
          </button>
        </form>
      )}
      {open && step === "code" && (
        <form
          onSubmit={verifyCode}
          className="absolute right-0 z-40 mt-2 w-72 rounded-card border border-line bg-card p-4 shadow-xl"
        >
          <p className="brand-label text-xs font-semibold text-muted">Enter the code</p>
          <p className="mt-2 text-xs leading-relaxed text-muted">
            {sendMode === "sent"
              ? `Sent to ${typed} on WhatsApp. It expires in 5 minutes.`
              : sendMode === "outbox"
                ? "Message transport is not configured, so the code landed in the merchant outbox (honestly undelivered) — this demo reads it from there."
                : "The send failed — the code may not arrive. You can go back and retry."}
          </p>
          <input
            autoFocus
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="6-digit code"
            inputMode="numeric"
            maxLength={6}
            className="mt-3 w-full rounded-control border border-line px-3 py-2 text-center text-lg tracking-[0.4em] outline-none focus:border-brand"
          />
          {error && <p className="mt-2 text-xs text-danger">{error}</p>}
          <button
            type="submit"
            disabled={busy || code.trim().length < 6}
            className="mt-3 w-full rounded-control bg-brand px-3 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-50"
          >
            {busy ? "Checking…" : "Verify and login"}
          </button>
          <button
            type="button"
            onClick={reset}
            className="mt-2 w-full rounded-control border border-line px-3 py-2 text-xs font-semibold text-muted hover:border-brand"
          >
            Different number
          </button>
        </form>
      )}
    </div>
  );
}
