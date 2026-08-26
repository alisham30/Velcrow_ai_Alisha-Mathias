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

/* What a lost-demand row is now, in the merchant's words rather than the
 * ledger's. "Recovered" is the only one of the four that is money. */
const DEMAND_STATE = {
  recovered: "refused, then bought — money recovered",
  told: "restocked, shopper told, not bought yet",
  lapsed: "restocked, nobody to tell",
};

/* Outstanding demand does not always mean "buy more stock". Where the shelf has
 * already been refilled, what is missing is the message. */
const DEMAND_ACTION = {
  restock: "needs restocking",
  notify: "in stock — they just have not been told",
};

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
      const [summary, ledger, reservations, proposals, strategy] = await Promise.all([
        shop.summary(),
        shop.demandLedger(),
        shop.reservations(),
        shop.proposals("open").catch(() => ({ proposals: [] })),
        shop.strategy().catch(() => null),
      ]);
      setData({ summary, ledger, reservations, proposals, strategy });
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
      const alreadyTold = result.already_notified || 0;
      // "Nobody wanted it" and "everyone who wanted it already knows" are
      // different facts. Reporting the second as the first read as though the
      // restock had reached nobody at all.
      const who = reached
        ? `${reached} waiting shopper${reached > 1 ? "s were" : " was"} offered it — ` +
          `they now decide, nothing is charged.`
        : alreadyTold
          ? `The ${alreadyTold} shopper${alreadyTold > 1 ? "s" : ""} who wanted this ` +
            `${alreadyTold > 1 ? "have" : "has"} already been told, so nobody was contacted again.`
          : "Nobody had asked for this one, so there was no one to contact.";
      setFlash({
        tone: "good",
        text:
          `Restocked ${row.product_name || row.item_id}` +
          `${row.variant ? ` (${row.variant})` : ""} by ${qty} to ${result.stock} in stock. ` +
          who,
      });
      await load();
    } catch (err) {
      setFlash({ tone: "bad", text: err.why || err.message });
    } finally {
      setBusy("");
    }
  }

  async function runGrowthAgent() {
    setBusy("agent");
    setFlash(null);
    try {
      const run = await shop.runGrowthAgent();
      const n = (run.proposals || []).length;
      setFlash({
        tone: "good",
        text:
          n > 0
            ? `The growth agent looked at your numbers and made ${n} proposal${
                n > 1 ? "s" : ""
              }. Nothing has changed — they are below for you to decide on.`
            : `The growth agent looked at your numbers and proposed nothing. ${run.summary}`,
      });
      await load();
    } catch (err) {
      setFlash({ tone: "bad", text: err.why || err.message });
    } finally {
      setBusy("");
    }
  }

  async function decide(prop, decision) {
    setBusy(prop.prop_id);
    setFlash(null);
    try {
      const reason =
        decision === "reject" ? window.prompt("Why are you rejecting it?") ?? "" : "";
      const result = await shop.decideProposal(prop.prop_id, decision, reason);
      const learned = result.learned || {};
      setFlash({
        tone: decision === "approve" ? "good" : "bad",
        text:
          (decision === "approve"
            ? `Applied. ${JSON.stringify(result.applied)}. `
            : "Rejected, and nothing was changed. ") +
          `The agent now rates "${prop.kind}" ${learned.approvals ?? 0} approved to ${
            learned.rejections ?? 0
          } rejected, so it will lead with it ${
            decision === "approve" ? "more" : "less"
          } often.`,
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
            title="Growth agent"
            note="Runs on its own every hour, and on demand here. It reads your sales, your lost demand and your margins, tests an idea against margin before suggesting it, and is allowed to come back with nothing. It cannot change stock or pricing — only you can, below."
            right={
              <button
                onClick={runGrowthAgent}
                disabled={busy === "agent"}
                className="rounded-control bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:bg-muted"
              >
                {busy === "agent" ? "Thinking…" : "Run now"}
              </button>
            }
          >
            {(data.proposals?.proposals || []).length === 0 ? (
              <p className="rounded-card border border-line bg-card p-6 text-sm text-muted">
                Nothing proposed right now. That is a normal outcome — if your stock is healthy
                and nothing is being refused, there is nothing worth doing.
              </p>
            ) : (
              <div className="space-y-3">
                {data.proposals.proposals.map((p) => (
                  <div key={p.prop_id} className="rounded-card border border-line bg-card p-5">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <p className="brand-label text-xs font-bold text-accent-ink">{p.kind}</p>
                      <p className="font-mono text-[11px] text-muted">{p.prop_id}</p>
                    </div>
                    <p className="mt-2 text-sm leading-relaxed">{p.rationale}</p>
                    {Object.keys(p.numbers || {}).length > 0 && (
                      <dl className="mt-3 grid gap-x-6 gap-y-1 text-xs text-muted sm:grid-cols-2">
                        {Object.entries(p.numbers)
                          .filter(([, v]) => typeof v !== "object")
                          .slice(0, 6)
                          .map(([k, v]) => (
                            <div key={k} className="flex justify-between gap-3">
                              <dt>{k.replace(/_/g, " ")}</dt>
                              <dd className="font-medium text-ink">{String(v)}</dd>
                            </div>
                          ))}
                      </dl>
                    )}
                    <div className="mt-4 flex gap-3">
                      <button
                        onClick={() => decide(p, "approve")}
                        disabled={busy === p.prop_id}
                        className="rounded-control bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:bg-muted"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => decide(p, "reject")}
                        disabled={busy === p.prop_id}
                        className="rounded-control border border-line px-4 py-2 text-sm font-semibold text-muted hover:border-danger hover:text-danger disabled:opacity-50"
                      >
                        Reject
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {data.strategy && (
              <div className="mt-4 rounded-card border border-line bg-card p-4">
                <p className="brand-label text-xs font-semibold text-muted">
                  What it has learned from you
                </p>
                <div className="mt-3 space-y-1.5">
                  {Object.entries(data.strategy.arms).map(([arm, a]) => (
                    <div key={arm} className="flex items-center gap-3 text-xs">
                      <span className="w-24 text-ink">{arm.replace(/_/g, " ")}</span>
                      <div className="h-1.5 flex-1 bg-line">
                        <div className="h-full bg-brand" style={{ width: `${a.mean * 100}%` }} />
                      </div>
                      <span className="w-28 text-right text-muted">
                        {a.approvals} yes · {a.rejections} no
                      </span>
                    </div>
                  ))}
                </div>
                <p className="mt-2 text-[11px] leading-relaxed text-muted">
                  Every decision you make here shifts which strategy it tries first next time.
                </p>
              </div>
            )}
          </Section>

          <Section
            title="Lost demand"
            note="What you are STILL being asked for. The number on the left counts only what is unresolved — a refusal the shopper came back and bought shows as bought back and stops being counted, and so does one they have been told about. Each row says whether it needs stock or only a message; restocking tells VelcrowAI, which offers it to the shoppers who were turned away, and they still approve the purchase themselves."
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
                          <td className="p-3">
                            {row.outstanding_units}
                            {row.recovered_units > 0 && (
                              <span className="font-medium text-brand">
                                {" "}
                                (+{row.recovered_units} bought back)
                              </span>
                            )}
                            {row.told_units > 0 && (
                              <span className="text-muted"> (+{row.told_units} told)</span>
                            )}
                          </td>
                          <td className="p-3 font-medium">
                            {rupees(row.outstanding_value_paise)}
                            {row.state !== "outstanding" ? (
                              <span className="block text-xs font-normal text-muted">
                                {DEMAND_STATE[row.state] || row.state}
                              </span>
                            ) : (
                              DEMAND_ACTION[row.action] && (
                                <span className="block text-xs font-normal text-muted">
                                  {DEMAND_ACTION[row.action]}
                                </span>
                              )
                            )}
                          </td>
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
