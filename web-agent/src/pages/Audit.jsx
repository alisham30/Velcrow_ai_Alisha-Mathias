import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { audit as api, buyer as buyerApi } from "../api.js";
import { rupees } from "../money.js";

/* The evidence room (spec 9).
 *
 * Everything a sceptic would want to do at the end of the demo: verify both
 * chains, break one on purpose and watch it go red at the exact index, put
 * the two sides of a disputed transaction beside each other, read the tools
 * the agent actually chose, and look at the measured revenue difference -
 * including what the merchant gave away, not only what it gained.
 *
 * Deliberately plain. This is the screen where the claims get checked, so it
 * should look like a record rather than a pitch.
 */

const TABS = [
  ["chains", "Chains"],
  ["trace", "Trace"],
  ["dispute", "Dispute"],
  ["lab", "Revenue Lab"],
  ["trust", "Trust"],
];

function Mark() {
  return (
    <svg width="18" height="18" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M2 5.2h7.2a2.6 2.6 0 1 1 0 5.2H2" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="square" />
      <path d="M14 10.8H6.8a2.6 2.6 0 1 1 0-5.2H14" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="square" />
    </svg>
  );
}

function Stat({ label, value, sub, tone }) {
  return (
    <div className="border border-line-soft bg-card p-4">
      <p className="label text-muted">{label}</p>
      <p className={`mt-1.5 text-xl font-semibold ${tone === "good" ? "text-good" : "text-ink"}`}>
        {value}
      </p>
      {sub && <p className="mt-1 text-xs leading-relaxed text-muted">{sub}</p>}
    </div>
  );
}

function Chains() {
  const [data, setData] = useState(null);
  const [verify, setVerify] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const [chains, v] = await Promise.all([api.chains(30), api.verify()]);
      setData(chains);
      setVerify(v);
      setErr(null);
    } catch (e) {
      setErr(e.why || e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 8000); // tailing, per spec 9
    return () => clearInterval(t);
  }, [load]);

  async function tamper(actor) {
    setBusy(true);
    try {
      const entries = data.chains[actor] || [];
      await api.tamper(actor, Math.max(0, entries.length - 4));
      await load();
    } catch (e) {
      setErr(e.why || e.message);
    } finally {
      setBusy(false);
    }
  }

  if (err) return <p className="border border-danger/40 bg-danger-soft p-4 text-sm">{err}</p>;
  if (!data || !verify) return <p className="text-sm text-muted">Reading the chains…</p>;

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <span
          className={`px-3 py-1.5 text-sm font-semibold ${
            verify.all_ok ? "bg-good-soft text-good" : "bg-danger-soft text-danger"
          }`}
        >
          {verify.all_ok ? "All chains verify" : "A chain has been altered"}
        </span>
        <button
          onClick={load}
          className="border border-line px-3 py-1.5 text-xs font-semibold hover:border-ink"
        >
          Re-verify
        </button>
      </div>

      <p className="mt-3 max-w-3xl text-sm text-muted">
        Each entry is hashed over the one before it, so altering any of them breaks every link
        after it. The <em>why</em> on each line is the part a person reads; the hash is the part
        that proves nobody edited it afterwards.
      </p>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        {data.actors.map((actor) => {
          const v = verify.chains[actor];
          const entries = data.chains[actor] || [];
          return (
            <div key={actor} className="border border-line-soft bg-card">
              <div className="flex items-center justify-between border-b border-line-soft p-3">
                <div>
                  <p className="label">{actor}</p>
                  <p className="mt-0.5 text-xs text-muted">
                    {v.entries} entries ·{" "}
                    {v.ok ? (
                      <span className="text-good">verified</span>
                    ) : (
                      <span className="text-danger">broken at #{v.first_bad_index}</span>
                    )}
                  </p>
                </div>
                <button
                  onClick={() => tamper(actor)}
                  disabled={busy || entries.length < 2}
                  className="border border-line px-2 py-1 text-[11px] text-muted hover:border-danger hover:text-danger disabled:opacity-40"
                  title="Rewrite one entry on disk, the way an attacker would"
                >
                  Tamper
                </button>
              </div>
              <ol className="max-h-96 overflow-y-auto">
                {entries.slice().reverse().map((e) => (
                  <li key={e.i} className="border-b border-line-soft p-3 last:border-0">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-mono text-[11px] text-muted">#{e.i}</span>
                      <span className="label text-[10px] text-ink-soft">{e.event}</span>
                    </div>
                    <p className="mt-1 text-xs leading-relaxed">{e.why}</p>
                  </li>
                ))}
                {entries.length === 0 && (
                  <li className="p-4 text-xs text-muted">Nothing recorded yet.</li>
                )}
              </ol>
            </div>
          );
        })}
      </div>
    </>
  );
}

function Trace() {
  const [turns, setTurns] = useState(null);
  useEffect(() => {
    api.traces().then((d) => setTurns(d.turns)).catch(() => setTurns([]));
  }, []);

  if (!turns) return <p className="text-sm text-muted">Reading traces…</p>;
  if (!turns.length) {
    return (
      <p className="border border-line-soft bg-card p-6 text-sm text-muted">
        No agent turns recorded yet. Shop with the widget in either storefront and they will
        appear here.
      </p>
    );
  }

  return (
    <>
      <p className="max-w-3xl text-sm text-muted">
        What the agent chose, in order, for each turn. The tools and their number vary with what
        it found — the same sentence against different stock produces a visibly different trace,
        which is the difference between an agent and a script.
      </p>
      <div className="mt-6 space-y-4">
        {turns.map((t) => (
          <div key={t.run_id} className="border border-line-soft bg-card p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="font-semibold">{t.asked}</p>
              <p className="font-mono text-[11px] text-muted">
                {t.shop_id} · {t.steps.length} step{t.steps.length === 1 ? "" : "s"}
              </p>
            </div>
            <ol className="mt-3 space-y-2">
              {t.steps.map((s) => (
                <li
                  key={s.i}
                  className={`border-l-2 pl-3 ${s.ok ? "border-ink" : "border-danger"}`}
                >
                  <p className="font-mono text-[11px] text-ink">
                    {s.tool}({Object.entries(s.args || {})
                      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                      .join(", ")})
                  </p>
                  <p className="mt-0.5 text-xs text-muted">{s.why}</p>
                </li>
              ))}
              {t.steps.length === 0 && (
                <li className="text-xs text-muted">
                  Answered without calling a tool.
                </li>
              )}
            </ol>
          </div>
        ))}
      </div>
    </>
  );
}

function Dispute() {
  const [txn, setTxn] = useState("");
  const [result, setResult] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  async function check(e) {
    e.preventDefault();
    if (!txn.trim()) return;
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      setResult(await api.dispute(txn.trim()));
    } catch (ex) {
      setErr(ex.why || ex.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p className="max-w-3xl text-sm text-muted">
        Both sides wrote their own record of every transaction, independently, and neither can
        edit theirs without it showing. Give a reference and this puts them side by side and
        names any disagreement with the entries that prove it.
      </p>
      <form onSubmit={check} className="mt-5 flex gap-2">
        <input
          value={txn}
          onChange={(e) => setTxn(e.target.value)}
          placeholder="txn_..."
          className="flex-1 border border-line bg-card px-4 py-2.5 font-mono text-sm outline-none focus:border-ink"
        />
        <button
          type="submit"
          disabled={busy}
          className="bg-ink px-5 py-2.5 text-sm font-semibold text-white disabled:bg-muted"
        >
          {busy ? "Checking…" : "Check"}
        </button>
      </form>

      {err && <p className="mt-4 border border-danger/40 bg-danger-soft p-4 text-sm">{err}</p>}

      {result && (
        <div className="mt-6">
          <p
            className={`px-3 py-1.5 text-sm font-semibold ${
              result.agreed ? "bg-good-soft text-good" : "bg-danger-soft text-danger"
            }`}
          >
            {result.agreed ? "Both sides agree" : "The two records disagree"}
          </p>
          <ul className="mt-3 space-y-2">
            {result.findings.map((f, i) => (
              <li key={i} className="border border-line-soft bg-card p-3 text-sm leading-relaxed">
                {f}
              </li>
            ))}
          </ul>
          <div className="mt-5 grid gap-4 md:grid-cols-2">
            {[["Buyer's record", result.buyer], [`${result.shop_id} record`, result.shop]].map(
              ([title, rows]) => (
                <div key={title} className="border border-line-soft bg-card">
                  <p className="label border-b border-line-soft p-3">{title}</p>
                  <ol>
                    {rows.map((e) => (
                      <li key={e.i} className="border-b border-line-soft p-3 last:border-0">
                        <span className="font-mono text-[11px] text-muted">#{e.i}</span>{" "}
                        <span className="label text-[10px] text-ink-soft">{e.event}</span>
                        <p className="mt-1 text-xs leading-relaxed">{e.why}</p>
                      </li>
                    ))}
                    {rows.length === 0 && (
                      <li className="p-3 text-xs text-muted">Nothing recorded on this side.</li>
                    )}
                  </ol>
                </div>
              ),
            )}
          </div>
        </div>
      )}
    </>
  );
}

function Lab() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    api.revenueLab().then(setD).catch((e) => setErr(e.why || e.message));
  }, []);

  if (err) return <p className="border border-danger/40 bg-danger-soft p-4 text-sm">{err}</p>;
  if (!d) return <p className="text-sm text-muted">Reading the orders…</p>;

  const rows = [
    ["Orders", d.unassisted.orders, d.assisted.orders],
    ["Revenue", d.unassisted.revenue_display, d.assisted.revenue_display],
    ["Average order", d.unassisted.aov_display, d.assisted.aov_display],
    ["Units sold", d.unassisted.units, d.assisted.units],
    ["Orders with a coupon", d.unassisted.coupon_orders, d.assisted.coupon_orders],
    [
      "Coupon claim rate",
      `${Math.round(d.unassisted.claim_rate * 100)}%`,
      `${Math.round(d.assisted.claim_rate * 100)}%`,
    ],
    ["Discount given away", d.unassisted.discount_display, d.assisted.discount_display],
    ["Sales rescued", d.unassisted.rescued_orders, d.assisted.rescued_orders],
    [
      "Rescued revenue",
      d.unassisted.rescued_revenue_display,
      d.assisted.rescued_revenue_display,
    ],
  ];

  return (
    <>
      <p className="max-w-3xl text-sm text-muted">
        Every figure here comes from orders that were really placed and really paid for, read out
        of the two shops&rsquo; own databases. Each order was flagged as agent-assisted or not at
        the moment it was created, so the split is recorded rather than reconstructed.
      </p>

      {d.total_orders === 0 ? (
        <p className="mt-6 border border-line-soft bg-card p-6 text-sm text-muted">
          No paid orders yet. Buy something in a storefront on your own, then buy something
          through the widget, and the comparison fills in.
        </p>
      ) : (
        <>
          <div className="mt-6 overflow-x-auto border border-line-soft bg-card">
            <table className="w-full min-w-[34rem] text-sm">
              <thead>
                <tr className="border-b border-line-soft text-left">
                  <th className="p-3" />
                  <th className="p-3 font-semibold">Without the agent</th>
                  <th className="p-3 font-semibold">With the agent</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([label, a, b]) => (
                  <tr key={label} className="border-b border-line-soft last:border-0">
                    <td className="p-3 text-muted">{label}</td>
                    <td className="p-3">{a}</td>
                    <td className="p-3 font-medium">{b}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {d.comparable && (
            <div className="mt-5 grid gap-4 sm:grid-cols-2">
              <Stat
                label="Average order, difference"
                value={d.aov_delta_display}
                sub="assisted minus unassisted, on real orders"
                tone={d.aov_delta_paise > 0 ? "good" : undefined}
              />
              <Stat
                label="Rescued revenue"
                value={d.assisted.rescued_revenue_display}
                sub="orders that closed a reservation the shop had refused"
                tone={d.assisted.rescued_revenue > 0 ? "good" : undefined}
              />
            </div>
          )}
        </>
      )}

      {d.notes?.length > 0 && (
        <ul className="mt-5 space-y-2">
          {d.notes.map((n, i) => (
            <li
              key={i}
              className="border border-line-soft bg-card p-3 text-xs leading-relaxed text-muted"
            >
              {n}
            </li>
          ))}
        </ul>
      )}

      <p className="mt-5 max-w-3xl border border-line-soft bg-card p-4 text-xs leading-relaxed text-muted">
        <strong className="text-ink">Nothing here is modelled.</strong> An earlier version of this
        page ran 20 scripted goals through a simulation and multiplied them by assumed
        follow-through rates, then printed the result beside real rupee figures. It was removed:
        a scoreboard whose comparison column is a guess is worse than no scoreboard. Where a
        counterfactual is unavoidable — what a basket would have cost had nobody claimed its
        coupon — it is computed from that same real order by adding its own discount back.
      </p>
    </>
  );
}

function Trust() {
  const [d, setD] = useState(null);
  useEffect(() => {
    buyerApi.trust().then(setD).catch(() => setD({ scores: {}, recent: [] }));
  }, []);
  if (!d) return <p className="text-sm text-muted">Loading…</p>;
  const shops = Object.entries(d.scores);

  return (
    <>
      <p className="max-w-3xl text-sm text-muted">
        Per-shop trust, earned slowly and lost fast: a clean deal adds 0.05, being caught charging
        more than was approved halves it. It is 20% of how the buyer ranks options, so a merchant
        that cheats is outranked afterwards rather than merely noted.
      </p>
      {shops.length === 0 ? (
        <p className="mt-5 border border-line-soft bg-card p-6 text-sm text-muted">
          No shop has transacted yet.
        </p>
      ) : (
        <div className="mt-5 space-y-2">
          {shops.map(([shop, s]) => (
            <div key={shop} className="flex items-center gap-3 border border-line-soft bg-card p-3 text-sm">
              <span className="w-28 font-medium">{shop}</span>
              <div className="h-2 flex-1 bg-line-soft">
                <div
                  className={`h-full ${s.score < 0.5 ? "bg-danger" : "bg-ink"}`}
                  style={{ width: `${s.score * 100}%` }}
                />
              </div>
              <span className="w-12 text-right font-mono">{s.score.toFixed(2)}</span>
              <span className="w-32 text-xs text-muted">
                {s.deals} clean · {s.violations} caught
              </span>
            </div>
          ))}
        </div>
      )}

      {d.recent?.length > 0 && (
        <ol className="mt-6 space-y-2">
          {d.recent.map((e, i) => (
            <li key={i} className="border border-line-soft bg-card p-3 text-xs leading-relaxed">
              <span className="label text-[10px] text-ink-soft">{e.shop_id} · {e.kind}</span>
              <p className="mt-1">{e.why}</p>
              <p className="mt-1 font-mono text-[11px] text-muted">
                {e.score_before} → {e.score_after}
              </p>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

export default function Audit() {
  const [tab, setTab] = useState("chains");

  return (
    <div className="mx-auto max-w-6xl px-5 py-8">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-line-soft pb-5">
        <div className="flex items-center gap-2.5">
          <Mark />
          <div>
            <p className="label">VelcrowAI · Audit</p>
            <p className="text-xs text-muted">Every rule that stopped the agent lives in code</p>
          </div>
        </div>
        <Link to="/" className="border border-line px-3 py-1.5 text-xs font-semibold hover:border-ink">
          Back to buying
        </Link>
      </header>

      <nav className="mt-6 flex flex-wrap gap-2">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-4 py-2 text-sm font-semibold ${
              tab === key ? "bg-ink text-white" : "border border-line text-muted hover:border-ink hover:text-ink"
            }`}
          >
            {label}
          </button>
        ))}
      </nav>

      <section className="mt-7">
        {tab === "chains" && <Chains />}
        {tab === "trace" && <Trace />}
        {tab === "dispute" && <Dispute />}
        {tab === "lab" && <Lab />}
        {tab === "trust" && <Trust />}
      </section>
    </div>
  );
}
