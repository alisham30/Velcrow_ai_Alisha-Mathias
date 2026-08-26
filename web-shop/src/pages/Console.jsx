import React, { useCallback, useEffect, useState } from "react";
import { shop } from "../api.js";
import { brand, variantLabel } from "../brand.js";
import { rupees } from "../money.js";
import { ErrorBanner } from "../components/States.jsx";

/* Merchant console for THIS shop only (spec 6.2, 14).
 *
 * Every request goes to this brand's own apiBase, so there is no code path by
 * which one merchant could see the other's numbers - the separation is
 * structural, not a filter someone remembered to apply.
 *
 * The console is deliberately not an "admin template": it carries the same
 * type, palette and rhythm as the storefront it belongs to, because it is the
 * same business (spec 11).
 */

function pct(rate) {
  return `${Math.round(rate * 100)}%`;
}

function Stat({ label, value, sub, tone }) {
  return (
    <div className="rounded-card border border-line bg-card p-5">
      <p className="brand-label text-xs font-semibold text-muted">{label}</p>
      <p
        className={`mt-2 font-display text-2xl font-semibold ${
          tone === "good" ? "text-brand" : "text-ink"
        }`}
      >
        {value}
      </p>
      {sub && <p className="mt-1 text-xs leading-relaxed text-muted">{sub}</p>}
    </div>
  );
}

function Section({ title, note, children, right }) {
  return (
    <section className="mt-10">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-display text-xl font-semibold">{title}</h2>
        {right}
      </div>
      {note && <p className="mt-1 max-w-2xl text-sm text-muted">{note}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function SkeletonConsole() {
  return (
    <div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="rounded-card border border-line bg-card p-5">
            <div className="skeleton mb-3 h-3 w-24" />
            <div className="skeleton h-7 w-32" />
          </div>
        ))}
      </div>
      <div className="skeleton mt-10 h-40 w-full rounded-card" />
    </div>
  );
}

export default function Console() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState("");
  const [flash, setFlash] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [summary, ledger, reservations] = await Promise.all([
        shop.summary(),
        shop.demandLedger(),
        shop.reservations(),
      ]);
      setData({ summary, ledger, reservations });
    } catch (err) {
      setError(err.why || err.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function restock(row) {
    // Cover what was actually lost. Deliberately not a forecast - forecasting
    // arrives with the autonomous merchant agent (spec 7.5), and a number
    // invented here would look like one.
    const qty = Number(row.lost_units || 1);
    setBusy(`${row.item_id}:${row.variant}`);
    setFlash(null);
    try {
      const result = await shop.restock(row.item_id, row.variant, qty);
      const reached = (result.reservations_notified || []).filter((r) => r.offered).length;
      setFlash({
        tone: "good",
        text:
          `Restocked ${row.product_name || row.item_id}` +
          `${row.variant ? ` (${row.variant})` : ""} by ${qty} to ${result.stock} in stock. ` +
          (reached
            ? `${reached} waiting shopper${reached > 1 ? "s were" : " was"} offered it — ` +
              `they now decide, nothing is charged.`
            : "Nobody was waiting on it, so no one was contacted."),
      });
      await load();
    } catch (err) {
      setFlash({ tone: "bad", text: err.why || err.message });
    } finally {
      setBusy("");
    }
  }

  async function toggleCheat(next) {
    setBusy("cheat");
    setFlash(null);
    try {
      await shop.setCheatMode(next);
      setFlash({
        tone: next ? "bad" : "good",
        text: next
          ? "Cheat mode is ON. This shop now quotes more than the basket is worth — the buyer's wallet should refuse it."
          : "Cheat mode is off. Quotes match the basket again.",
      });
      await load();
    } catch (err) {
      setFlash({ tone: "bad", text: err.why || err.message });
    } finally {
      setBusy("");
    }
  }

  return (
    <main className="mx-auto max-w-6xl px-5 py-10">
      <header className="border-b border-line pb-6">
        <p className="brand-label text-xs font-semibold text-muted">Merchant console</p>
        <h1 className="mt-1 font-display text-3xl font-semibold">{brand.name}</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted">
          Your shop only. VelcrowAI is installed here as a layer — these numbers are yours, and
          no other merchant can see them.
        </p>
      </header>

      {error && <ErrorBanner text={error} onRetry={load} />}
      {!data && !error && (
        <div className="mt-8">
          <SkeletonConsole />
        </div>
      )}

      {flash && (
        <p
          className={`mt-6 rounded-card border p-4 text-sm ${
            flash.tone === "bad"
              ? "border-danger/30 bg-danger-soft text-danger"
              : "border-line bg-ok-soft text-ink"
          }`}
        >
          {flash.text}
        </p>
      )}

      {data && (
        <>
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat
              label="Revenue"
              value={rupees(data.summary.revenue_paise)}
              sub={`${data.summary.orders} paid order${data.summary.orders === 1 ? "" : "s"}`}
            />
            <Stat
              label="Average order"
              value={rupees(data.summary.aov_paise)}
              sub={
                data.summary.assisted.orders
                  ? `${rupees(data.summary.assisted.aov_paise)} with the agent vs ` +
                    `${rupees(data.summary.unassisted.aov_paise)} without`
                  : "no agent-assisted orders yet"
              }
            />
            <Stat
              label="Coupons claimed"
              value={pct(data.summary.coupon_claim_rate)}
              sub={`${data.summary.orders_with_coupon} of ${data.summary.orders} orders had a coupon applied`}
            />
            <Stat
              label="Sales rescued"
              value={rupees(data.summary.rescued.revenue_paise)}
              tone={data.summary.rescued.orders ? "good" : undefined}
              sub={
                data.summary.rescued.orders
                  ? `${data.summary.rescued.orders} order${
                      data.summary.rescued.orders === 1 ? "" : "s"
                    } you had already turned away for stock`
                  : "no out-of-stock sales recovered yet"
              }
            />
          </div>

          <Section
            title="With the agent, and without"
            note="Every order is recorded at the moment it is placed as agent-assisted or not, so this split is measured rather than estimated."
          >
            <div className="grid gap-4 sm:grid-cols-2">
              {[
                { key: "assisted", label: "Assisted by VelcrowAI" },
                { key: "unassisted", label: "Shopper on their own" },
              ].map(({ key, label }) => (
                <div key={key} className="rounded-card border border-line bg-card p-5">
                  <p className="brand-label text-xs font-semibold text-muted">{label}</p>
                  <p className="mt-2 font-display text-2xl font-semibold">
                    {rupees(data.summary[key].revenue_paise)}
                  </p>
                  <p className="mt-1 text-sm text-muted">
                    {data.summary[key].orders} order
                    {data.summary[key].orders === 1 ? "" : "s"}
                    {data.summary[key].orders
                      ? ` · ${rupees(data.summary[key].aov_paise)} average`
                      : ""}
                  </p>
                </div>
              ))}
            </div>
          </Section>

          <Section
            title="Lost demand"
            note="What you were asked for and could not sell. Restocking here tells VelcrowAI, which offers it to the shoppers who were turned away — they still approve the purchase themselves."
          >
            {data.ledger.rows.length === 0 ? (
              <p className="rounded-card border border-line bg-card p-6 text-sm text-muted">
                Nothing refused for stock yet. When a shopper asks for something you are out of,
                it lands here with what it cost you.
              </p>
            ) : (
              <div className="overflow-x-auto rounded-card border border-line bg-card">
                <table className="w-full min-w-[46rem] text-sm">
                  <thead>
                    <tr className="border-b border-line text-left">
                      <th className="p-3 font-semibold">Item</th>
                      <th className="p-3 font-semibold">Lost</th>
                      <th className="p-3 font-semibold">Value</th>
                      <th className="p-3 font-semibold">In stock</th>
                      <th className="p-3 font-semibold">Back on</th>
                      <th className="p-3" />
                    </tr>
                  </thead>
                  <tbody>
                    {data.ledger.rows.map((row) => {
                      const id = `${row.item_id}:${row.variant}`;
                      return (
                        <tr key={id} className="border-b border-line last:border-0">
                          <td className="p-3">
                            <span className="font-medium">{row.product_name || row.item_id}</span>
                            {row.variant && (
                              <span className="text-muted">
                                {" "}
                                · {variantLabel()} {row.variant}
                              </span>
                            )}
                          </td>
                          <td className="p-3">{row.lost_units}</td>
                          <td className="p-3 font-medium">{rupees(row.lost_value_paise)}</td>
                          <td className="p-3">
                            {row.in_stock === 0 ? (
                              <span className="text-danger">out</span>
                            ) : (
                              row.in_stock
                            )}
                          </td>
                          <td className="p-3 text-muted">{row.restock_date || "—"}</td>
                          <td className="p-3 text-right">
                            <button
                              onClick={() => restock(row)}
                              disabled={busy === id}
                              className="rounded-control bg-brand px-3 py-2 text-xs font-semibold text-white hover:bg-brand-deep disabled:bg-muted"
                            >
                              {busy === id ? "Restocking…" : `Restock ${row.lost_units}`}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>

          <Section
            title="Reservations"
            note="Shoppers holding a place for something you were out of. Open means they are still waiting on you."
          >
            {data.reservations.reservations.length === 0 ? (
              <p className="rounded-card border border-line bg-card p-6 text-sm text-muted">
                No one is waiting on stock right now.
              </p>
            ) : (
              <div className="overflow-x-auto rounded-card border border-line bg-card">
                <table className="w-full min-w-[42rem] text-sm">
                  <thead>
                    <tr className="border-b border-line text-left">
                      <th className="p-3 font-semibold">Item</th>
                      <th className="p-3 font-semibold">Held for</th>
                      <th className="p-3 font-semibold">Worth</th>
                      <th className="p-3 font-semibold">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.reservations.reservations.map((r) => (
                      <tr key={r.res_id} className="border-b border-line last:border-0">
                        <td className="p-3">
                          {r.product_name}
                          {r.variant && <span className="text-muted"> · {r.variant}</span>}
                          <span className="text-muted"> × {r.qty}</span>
                        </td>
                        <td className="p-3 text-muted">{r.contact_ref}</td>
                        <td className="p-3">{rupees(r.value_paise)}</td>
                        <td className="p-3">
                          {r.status === "converted" ? (
                            <span className="font-medium text-brand">bought</span>
                          ) : r.status === "notified" ? (
                            <span className="text-ink">told it is back</span>
                          ) : (
                            <span className="text-danger">waiting</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Section>

          <Section
            title="Testing"
            note="For demonstrating what happens when a merchant misbehaves. Nothing here touches a real payment."
          >
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-card border border-line bg-card p-5">
              <div className="max-w-xl">
                <p className="font-medium">Quote more than the basket is worth</p>
                <p className="mt-1 text-sm text-muted">
                  With this on, this shop inflates the amount it asks for at checkout. The buyer's
                  wallet compares it against what the shopper approved and refuses to pay. Resets
                  to off whenever the shop restarts.
                </p>
              </div>
              <button
                onClick={() => toggleCheat(!data.summary.cheat_mode)}
                disabled={busy === "cheat"}
                className={`rounded-control px-4 py-2.5 text-sm font-semibold disabled:opacity-60 ${
                  data.summary.cheat_mode
                    ? "bg-danger text-white hover:opacity-90"
                    : "border border-line bg-paper text-ink hover:border-ink"
                }`}
              >
                {data.summary.cheat_mode ? "Turn cheat mode off" : "Turn cheat mode on"}
              </button>
            </div>
          </Section>
        </>
      )}
    </main>
  );
}
