import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { buyer as api, ApiError } from "../api.js";
import { rupees } from "../money.js";

/* The consumer buyer agent (spec 8).
 *
 * Intent in, ranked options out, across shops that do not know about each
 * other. Two rules shape most of what is below:
 *
 *   - An option that breaks a rule the shopper set is SHOWN, greyed, with the
 *     reason. It is never hidden and never selectable. The server refuses it
 *     too, so this is not styling standing in for a check.
 *   - A red Blocked card means over-cap, an invalid mandate, a price mismatch
 *     or an injection attempt. Not knowing someone's budget is a question,
 *     not a refusal, and renders as an ordinary card.
 */

const EXAMPLES = [
  "cotton kurti size M under 1500",
  "2 kg lemons under 100",
  "a linen kurta size XL under 1500",
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

function Card({ tone = "plain", children }) {
  const tones = {
    plain: "border-line-soft bg-card",
    blocked: "border-danger/40 bg-danger-soft",
    good: "border-good/30 bg-good-soft",
    warn: "border-warn-line bg-warn-soft",
  };
  return <div className={`border ${tones[tone]} p-4 text-sm leading-relaxed`}>{children}</div>;
}

function ScoreBar({ parts }) {
  const order = [
    ["price", "price"],
    ["rule_fit", "fits your rules"],
    ["trust", "shop trust"],
    ["availability", "in stock"],
  ];
  const total = order.reduce((s, [k]) => s + parts[k], 0) || 1;
  return (
    <div className="mt-3">
      <div className="flex h-1.5 w-full overflow-hidden bg-line-soft">
        {order.map(([k], i) => (
          <div
            key={k}
            title={`${order[i][1]}: ${parts[k]}`}
            style={{ width: `${(parts[k] / total) * 100}%` }}
            className={
              ["bg-ink", "bg-ink-soft", "bg-muted", "bg-line"][i]
            }
          />
        ))}
      </div>
      <p className="mt-1.5 text-[11px] text-muted">
        {order.map(([k, label]) => `${label} ${parts[k]}`).join(" · ")}
      </p>
    </div>
  );
}

function OptionCard({ option, onChoose, busy }) {
  const blocked = !option.selectable;
  return (
    <div
      className={`border p-4 ${
        blocked ? "border-line-soft bg-paper opacity-70" : "border-line bg-card"
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className={`font-semibold ${blocked ? "text-muted" : "text-ink"}`}>
            {option.name}
            {option.variant && <span className="text-muted"> · {option.variant}</span>}
          </p>
          <p className="mt-0.5 text-xs text-muted">{option.shop_name}</p>
        </div>
        <p className={`text-lg font-semibold ${blocked ? "text-muted" : "text-ink"}`}>
          {option.price_display}
        </p>
      </div>

      <p className="mt-2 text-xs text-muted">{option.why}</p>

      {blocked ? (
        <ul className="mt-3 space-y-1">
          {option.breaks_rules.map((r) => (
            <li key={r} className="text-xs text-danger">
              Cannot buy this: {r}
            </li>
          ))}
        </ul>
      ) : (
        <>
          <ScoreBar parts={option.score_parts} />
          <button
            onClick={() => onChoose(option)}
            disabled={busy}
            className="mt-3 w-full bg-ink px-4 py-2.5 text-sm font-semibold text-white hover:bg-ink-soft disabled:bg-muted"
          >
            {busy
              ? "Working…"
              : option.in_stock
                ? `Choose this — ${option.price_display}`
                : "Reserve this"}
          </button>
        </>
      )}
    </div>
  );
}

export default function Buyer() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [goal, setGoal] = useState("");
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [trust, setTrust] = useState(null);
  const bottom = useRef(null);

  const loadTrust = useCallback(async () => {
    try {
      setTrust(await api.trust());
    } catch {
      /* the panel is informative, not load-bearing */
    }
  }, []);

  // The run id lives in the URL, so a refresh or a shared link restores the
  // whole thread rather than starting over (spec 8).
  useEffect(() => {
    if (!runId) {
      setRun(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const state = await api.get(runId);
        if (!cancelled) setRun(state);
      } catch (err) {
        if (!cancelled) setError(err.why || err.message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  useEffect(() => {
    loadTrust();
  }, [loadTrust, run?.status]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [run?.messages?.length, run?.status]);

  async function start(text) {
    const stated = (text ?? goal).trim();
    if (!stated || busy) return;
    setBusy(true);
    setError(null);
    try {
      const state = await api.start(stated);
      setRun(state);
      setGoal("");
      navigate(`/run/${state.run_id}`);
    } catch (err) {
      setError(err.why || err.message);
    } finally {
      setBusy(false);
    }
  }

  async function choose(option) {
    setBusy(true);
    setError(null);
    try {
      setRun(await api.choose(run.run_id, option.option_id, "buyer@example.com"));
    } catch (err) {
      setError(err.why || err.message);
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    setBusy(true);
    setError(null);
    try {
      setRun(await api.approve(run.run_id));
    } catch (err) {
      setError(err.why || err.message);
    } finally {
      setBusy(false);
    }
  }

  const messages = run?.messages || [];
  const options = run?.options || [];

  return (
    <div className="mx-auto max-w-3xl px-5 py-8">
      <header className="flex items-center justify-between border-b border-line-soft pb-5">
        <div className="flex items-center gap-2.5">
          <Mark />
          <div>
            <p className="label">VelcrowAI</p>
            <p className="text-xs text-muted">Buying for you, across shops</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {run && (
            <button
              onClick={() => {
                setRun(null);
                navigate("/");
              }}
              className="border border-line px-3 py-1.5 text-xs font-semibold hover:border-ink"
            >
              New search
            </button>
          )}
          <Link
            to="/audit"
            className="border border-line px-3 py-1.5 text-xs font-semibold text-muted hover:border-ink hover:text-ink"
          >
            Audit
          </Link>
        </div>
      </header>

      {!run && (
        <section className="mt-10">
          <h1 className="text-2xl font-semibold">What are you looking for?</h1>
          <p className="mt-2 text-sm text-muted">
            Tell me the thing and what you will spend. I check every shop, rank what I find,
            and show you anything that breaks your rules rather than quietly dropping it.
            Nothing is bought without your approval.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              start();
            }}
            className="mt-6 flex gap-2"
          >
            <input
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="cotton kurti size M under 1500"
              className="flex-1 border border-line bg-card px-4 py-3 text-sm outline-none focus:border-ink"
            />
            <button
              type="submit"
              disabled={busy || !goal.trim()}
              className="bg-ink px-6 py-3 text-sm font-semibold text-white hover:bg-ink-soft disabled:bg-muted"
            >
              {busy ? "Looking…" : "Find it"}
            </button>
          </form>
          <div className="mt-4 flex flex-wrap gap-2">
            {EXAMPLES.map((e) => (
              <button
                key={e}
                onClick={() => start(e)}
                disabled={busy}
                className="border border-line px-3 py-1.5 text-xs text-muted hover:border-ink hover:text-ink disabled:opacity-50"
              >
                {e}
              </button>
            ))}
          </div>
        </section>
      )}

      {error && (
        <div className="mt-6">
          <Card tone="blocked">
            <p className="font-semibold text-danger">That did not work</p>
            <p className="mt-1 text-ink">{error}</p>
          </Card>
        </div>
      )}

      {run && (
        <section className="mt-8 space-y-4">
          {messages.map((m, i) => {
            if (m.kind === "goal") {
              return (
                <div key={i} className="flex justify-end">
                  <p className="bg-ink px-4 py-2.5 text-sm text-white">{m.text}</p>
                </div>
              );
            }
            if (m.kind === "blocked") {
              return (
                <Card key={i} tone="blocked">
                  <p className="label text-danger">Blocked</p>
                  <p className="mt-1.5 text-ink">{m.text}</p>
                  {m.code && <p className="mt-2 font-mono text-[11px] text-muted">{m.code}</p>}
                  {m.trust && (
                    <p className="mt-2 text-xs text-muted">
                      {m.trust.shop_id} trust {m.trust.score_before} → {m.trust.score_after}. No
                      money moved.
                    </p>
                  )}
                </Card>
              );
            }
            if (m.kind === "receipt") {
              return (
                <Card key={i} tone="good">
                  <p className="label text-good">Paid</p>
                  <p className="mt-1.5 text-ink">{m.text}</p>
                  {run.receipt && (
                    <dl className="mt-3 space-y-1 font-mono text-[11px] text-muted">
                      <div className="flex justify-between">
                        <dt>Reference</dt>
                        <dd>{run.receipt.txn_ref}</dd>
                      </div>
                      <div className="flex justify-between">
                        <dt>Razorpay test order</dt>
                        <dd>{run.receipt.razorpay_order_id}</dd>
                      </div>
                    </dl>
                  )}
                </Card>
              );
            }
            if (m.kind === "reserved") {
              return (
                <Card key={i} tone="warn">
                  <p className="label text-warn-ink">Reserved</p>
                  <p className="mt-1.5 text-ink">{m.text}</p>
                </Card>
              );
            }
            if (m.kind === "approve") {
              return null; // rendered as the approval card below, once
            }
            return (
              <Card key={i}>
                <p className="text-ink">{m.text}</p>
                {m.kind === "ask" && (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      start(`${run.goal} ${goal}`.trim());
                    }}
                    className="mt-3 flex gap-2"
                  >
                    <input
                      value={goal}
                      onChange={(e) => setGoal(e.target.value)}
                      placeholder="under 1500"
                      className="flex-1 border border-line bg-card px-3 py-2 text-sm outline-none focus:border-ink"
                    />
                    <button
                      type="submit"
                      disabled={busy || !goal.trim()}
                      className="bg-ink px-4 py-2 text-sm font-semibold text-white disabled:bg-muted"
                    >
                      Go on
                    </button>
                  </form>
                )}
              </Card>
            );
          })}

          {options.length > 0 && run.status !== "paid" && run.status !== "reserved" && (
            <div className="space-y-3">
              {options.map((o) => (
                <OptionCard key={o.option_id} option={o} onChoose={choose} busy={busy} />
              ))}
            </div>
          )}

          {run.status === "awaiting_approval" && run.quote && (
            <Card>
              <p className="label text-muted">Approve this payment</p>
              <div className="mt-3 space-y-1.5">
                {run.quote.line_items.map((li, i) => (
                  <div key={i} className="flex justify-between">
                    <span>
                      {run.chosen.name} × {li.qty}
                    </span>
                    <span>{rupees(li.unit_price_paise * li.qty)}</span>
                  </div>
                ))}
                {run.quote.coupon?.codes?.length > 0 && (
                  <div className="flex justify-between text-muted">
                    <span>Coupons {run.quote.coupon.codes.join(", ")}</span>
                    <span>− {rupees(run.quote.coupon.discount_paise)}</span>
                  </div>
                )}
              </div>
              <div className="mt-3 flex items-baseline justify-between border-t border-line-soft pt-3 text-base font-semibold">
                <span>Total</span>
                <span>{run.quote.charge_display}</span>
              </div>
              <button
                onClick={approve}
                disabled={busy}
                className="mt-4 w-full bg-ink px-4 py-3 text-sm font-semibold text-white hover:bg-ink-soft disabled:bg-muted"
              >
                {busy ? "Paying…" : `Approve and pay ${run.quote.charge_display}`}
              </button>
              <p className="mt-2 text-[11px] leading-relaxed text-muted">
                This signs exactly this basket at exactly this price to {run.quote.shop_name}. If
                they charge anything else, the payment is refused.
              </p>
            </Card>
          )}
          <div ref={bottom} />
        </section>
      )}

      {trust?.scores && Object.keys(trust.scores).length > 0 && (
        <section className="mt-12 border-t border-line-soft pt-5">
          <p className="label text-muted">Shop trust</p>
          <p className="mt-1 text-xs text-muted">
            Starts at 0.70. A clean deal earns +0.05; being caught charging more than you approved
            halves it. It is 20% of how options are ranked.
          </p>
          <div className="mt-3 space-y-1.5">
            {Object.entries(trust.scores).map(([shop, s]) => (
              <div key={shop} className="flex items-center gap-3 text-xs">
                <span className="w-24 text-ink">{shop}</span>
                <div className="h-1.5 flex-1 bg-line-soft">
                  <div className="h-full bg-ink" style={{ width: `${s.score * 100}%` }} />
                </div>
                <span className="w-10 text-right font-mono text-muted">
                  {s.score.toFixed(2)}
                </span>
                <span className="w-28 text-muted">
                  {s.deals} clean · {s.violations} caught
                </span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
