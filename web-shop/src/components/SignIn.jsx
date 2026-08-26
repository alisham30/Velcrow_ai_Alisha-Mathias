import React, { useEffect, useState } from "react";
import { shop } from "../api.js";
import { shopperKey } from "../shopperKey.js";
import { rupees } from "../money.js";

/* Being known to the shop, without an account (spec 7.3, and spec 14 which
 * bans accounts with passwords).
 *
 * A phone number or email, typed once and kept in this browser. It survives
 * refreshes and restarts, and it is what makes a past basket findable on a
 * different device - retype the same contact and the history follows.
 *
 * It is NOT a password login and the UI says so, because a contact is a claim
 * rather than a proof. It unlocks order history; it can never move money,
 * which still needs a mandate, a cart-bound approval and the wallet.
 */
export default function SignIn() {
  const [contact, setContact] = useState(() => shopperKey.contact());
  const [typed, setTyped] = useState("");
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
      .then((who) => (gone ? null : shop.lastOrder(who.contact_key)))
      .then((order) => !gone && setLast(order))
      .catch(() => !gone && setLast(null));
    return () => {
      gone = true;
    };
  }, [contact]);

  async function submit(e) {
    e.preventDefault();
    if (!typed.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const who = await shop.identify(typed);
      shopperKey.remember(who.contact_ref);
      setContact(who.contact_ref);
      setOpen(false);
      setTyped("");
    } catch (err) {
      setError(err.why || err.message);
    } finally {
      setBusy(false);
    }
  }

  function forget() {
    shopperKey.forget();
    setContact("");
    setLast(null);
    setOpen(false);
  }

  if (contact) {
    return (
      <div className="relative">
        <button
          onClick={() => setOpen((v) => !v)}
          className="rounded-control border border-line bg-card px-3 py-2 text-sm font-medium hover:border-brand"
        >
          <span className="hidden sm:inline text-muted">Signed in · </span>
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
            <button
              onClick={forget}
              className="mt-4 w-full rounded-control border border-line px-3 py-2 text-xs font-semibold text-muted hover:border-danger hover:text-danger"
            >
              Forget me on this device
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
        Sign in
      </button>
      {open && (
        <form
          onSubmit={submit}
          className="absolute right-0 z-40 mt-2 w-72 rounded-card border border-line bg-card p-4 shadow-xl"
        >
          <p className="brand-label text-xs font-semibold text-muted">Be remembered</p>
          <p className="mt-2 text-xs leading-relaxed text-muted">
            Your phone number or email. No password — it keeps your orders findable after a
            refresh, and on any other device you type it into.
          </p>
          <input
            autoFocus
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder="Phone or email"
            className="mt-3 w-full rounded-control border border-line px-3 py-2 text-sm outline-none focus:border-brand"
          />
          {error && <p className="mt-2 text-xs text-danger">{error}</p>}
          <button
            type="submit"
            disabled={busy || !typed.trim()}
            className="mt-3 w-full rounded-control bg-brand px-3 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-50"
          >
            {busy ? "Checking…" : "Remember me"}
          </button>
        </form>
      )}
    </div>
  );
}
