/* VelcrowAI widget — one bundle, served by :8003, embedded by both shops with
 * a single script tag (spec 2, 6.3):
 *
 *   <script src="http://localhost:8003/velcrow.js" data-shop="grocery"></script>
 *
 * It is cart-aware in the strict sense: it reads the cart id the host page is
 * already using and drives that same cart through the shop's own endpoints.
 * There is no second cart inside this widget.
 *
 * Identity is VelcrowAI's own and identical at both shops (spec 11), so it
 * reads as an installed third-party layer rather than part of either brand.
 * No gradients, no glow, no sparkle or robot iconography, no "AI Assistant".
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var AGENT = new URL(script.src).origin;
  var SHOP = script.getAttribute("data-shop") || "grocery";

  // Where the widget may appear is the merchant's call, not ours. They list
  // path prefixes to stay off - their back office, typically - on the same tag
  // they installed us with (spec 6.3: one tag, no other configuration).
  var excluded = (script.getAttribute("data-exclude") || "")
    .split(",")
    .map(function (p) {
      return p.trim();
    })
    .filter(Boolean);
  for (var i = 0; i < excluded.length; i++) {
    if (location.pathname.indexOf(excluded[i]) === 0) return;
  }

  // Stamped on the root element and logged at boot. A demo that is showing a
  // stale cached bundle is otherwise indistinguishable from a broken one.
  var BUILD = "phase5";

  var cfg = null;
  var cartId = null;
  var shopperRef = "";
  var contactKey = "";     // normalised by the shop; the portable half of identity
  var mandateToken = null;
  var history = [];
  var busy = false;

  // ---- styles: scoped, deliberately not the host brand ---------------------
  var CSS = [
    ".vc-root{position:fixed;right:20px;bottom:20px;z-index:2147483000;",
    "font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;color:#14161a}",
    ".vc-launch{display:flex;align-items:center;gap:8px;background:#14161a;color:#fff;border:0;",
    "border-radius:2px;padding:12px 16px;font-size:14px;font-weight:600;cursor:pointer;",
    "box-shadow:0 2px 10px rgba(0,0,0,.18);letter-spacing:.02em}",
    ".vc-launch:hover{background:#2a2e35}",
    // something is waiting that the shopper did not ask for
    ".vc-launch[data-waiting]{position:relative}",
    ".vc-launch[data-waiting]::after{content:'';position:absolute;top:-3px;right:-3px;",
    "width:9px;height:9px;border-radius:50%;background:#c8622f;border:2px solid #fff}",
    ".vc-mark{width:16px;height:16px;flex:0 0 16px}",
    ".vc-panel{width:376px;max-width:calc(100vw - 32px);height:540px;max-height:calc(100vh - 40px);",
    "background:#fff;border:1px solid #d9dbe0;border-radius:2px;display:flex;flex-direction:column;",
    "box-shadow:0 6px 30px rgba(0,0,0,.16);overflow:hidden}",
    ".vc-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;",
    "border-bottom:1px solid #e6e7ea;background:#14161a;color:#fff}",
    ".vc-title{display:flex;align-items:center;gap:8px;font-weight:700;font-size:13px;letter-spacing:.08em}",
    ".vc-host{font-size:11px;color:#a8adb6;letter-spacing:.04em}",
    ".vc-x{background:none;border:0;color:#a8adb6;font-size:18px;cursor:pointer;line-height:1;padding:2px 4px}",
    ".vc-x:hover{color:#fff}",
    ".vc-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px;background:#f7f8f9}",
    ".vc-msg{font-size:14px;line-height:1.5;max-width:92%}",
    ".vc-user{align-self:flex-end;background:#14161a;color:#fff;padding:8px 12px;border-radius:2px}",
    ".vc-bot{align-self:flex-start;background:#fff;border:1px solid #e6e7ea;padding:9px 12px;border-radius:2px}",
    ".vc-hint{font-size:12px;color:#6b7280;line-height:1.5}",
    ".vc-trace{align-self:flex-start;width:100%;font-size:12px}",
    ".vc-trace summary{cursor:pointer;color:#6b7280;padding:2px 0;list-style:none;",
    "border-bottom:1px dotted #c9ccd2;display:inline-block}",
    ".vc-trace summary::-webkit-details-marker{display:none}",
    ".vc-trace summary:hover{color:#14161a}",
    ".vc-steps{margin:8px 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:6px}",
    ".vc-step{background:#fff;border:1px solid #e6e7ea;border-left:2px solid #14161a;padding:6px 9px}",
    ".vc-step.bad{border-left-color:#b3402a}",
    ".vc-step code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#14161a}",
    ".vc-step .vc-why{color:#6b7280;display:block;margin-top:3px;line-height:1.45}",
    ".vc-step .vc-out{color:#3f4650;display:block;margin-top:3px}",
    ".vc-live{align-self:flex-start;font-size:12px;color:#6b7280;display:flex;align-items:center;gap:7px}",
    ".vc-dot{width:6px;height:6px;background:#14161a;border-radius:50%;animation:vc-blink 1s infinite}",
    "@keyframes vc-blink{0%,100%{opacity:.25}50%{opacity:1}}",
    ".vc-warn{background:#fdf1e7;border:1px solid #e6c9a8;color:#7a4a12;padding:8px 10px;font-size:12px;line-height:1.5}",
    // the approval gate and its outcomes (spec 5.1, 6.3)
    ".vc-card{align-self:flex-start;width:100%;background:#fff;border:1px solid #d9dbe0;",
    "border-top:2px solid #14161a;padding:12px;box-sizing:border-box}",
    ".vc-card h4{margin:0 0 9px;font-size:11px;letter-spacing:.09em;text-transform:uppercase;",
    "color:#6b7280;font-weight:700}",
    ".vc-rows{width:100%;border-collapse:collapse;font-size:13px}",
    ".vc-rows td{padding:3px 0;vertical-align:top;color:#14161a}",
    ".vc-rows td.r{text-align:right;white-space:nowrap;padding-left:10px}",
    ".vc-rows td.vc-sub{color:#6b7280;font-size:12px}",
    ".vc-tot{display:flex;justify-content:space-between;align-items:baseline;",
    "border-top:1px solid #e6e7ea;margin-top:7px;padding-top:8px;font-weight:700;font-size:15px}",
    ".vc-acts{display:flex;gap:8px;margin-top:12px}",
    ".vc-ok{flex:1;background:#14161a;color:#fff;border:0;border-radius:2px;padding:10px;",
    "font:inherit;font-weight:700;font-size:13px;cursor:pointer}",
    ".vc-ok:disabled{background:#9aa0a8;cursor:default}",
    ".vc-no{background:#fff;color:#6b7280;border:1px solid #d9dbe0;border-radius:2px;",
    "padding:10px 14px;font:inherit;font-size:13px;cursor:pointer}",
    ".vc-no:disabled{color:#b9bdc4;cursor:default}",
    ".vc-gate{font-size:11px;color:#6b7280;margin-top:9px;line-height:1.45}",
    ".vc-paid{border-top-color:#1c5d3c}",
    ".vc-paid .vc-tot{color:#1c5d3c}",
    ".vc-blocked{border-top-color:#b3402a}",
    ".vc-blocked h4{color:#b3402a}",
    ".vc-code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#6b7280}",
    ".vc-form{display:flex;gap:8px;padding:12px;border-top:1px solid #e6e7ea;background:#fff}",
    ".vc-in{flex:1;border:1px solid #d9dbe0;border-radius:2px;padding:9px 11px;font:inherit;font-size:14px;outline:none}",
    ".vc-in:focus{border-color:#14161a}",
    ".vc-send{background:#14161a;color:#fff;border:0;border-radius:2px;padding:9px 15px;font-weight:600;cursor:pointer}",
    ".vc-send:disabled{background:#9aa0a8;cursor:default}",
    ".vc-foot{padding:7px 12px;font-size:10.5px;color:#8b9099;text-align:center;border-top:1px solid #f0f1f3;",
    "background:#fff;letter-spacing:.03em}",
  ].join("");

  // A woven-loop mark: VelcrowAI's own, not a sparkle and not a robot.
  var MARK =
    '<svg class="vc-mark" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
    '<path d="M2 5.2h7.2a2.6 2.6 0 1 1 0 5.2H2" stroke="currentColor" stroke-width="1.6" stroke-linecap="square"/>' +
    '<path d="M14 10.8H6.8a2.6 2.6 0 1 1 0-5.2H14" stroke="currentColor" stroke-width="1.6" stroke-linecap="square"/>' +
    "</svg>";

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function money(paise) {
    return "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // ---- shell ---------------------------------------------------------------
  var root = el("div", "vc-root");
  var launcher = el("button", "vc-launch", MARK + "<span>Shop with VelcrowAI</span>");
  var panel = el("div", "vc-panel");
  panel.style.display = "none";
  panel.innerHTML =
    '<div class="vc-head"><div class="vc-title">' + MARK +
    "<span>VELCROWAI</span></div>" +
    '<div style="display:flex;align-items:center;gap:10px">' +
    '<span class="vc-host"></span><button class="vc-x" aria-label="close">&times;</button></div></div>' +
    '<div class="vc-log"></div>' +
    '<form class="vc-form"><input class="vc-in" placeholder="Ask for something" autocomplete="off"/>' +
    '<button class="vc-send" type="submit">Send</button></form>' +
    '<div class="vc-foot">Installed by the merchant. It cannot pay without your approval.</div>';

  root.appendChild(panel);
  root.appendChild(launcher);

  var log = panel.querySelector(".vc-log");
  var form = panel.querySelector(".vc-form");
  var input = panel.querySelector(".vc-in");
  var sendBtn = panel.querySelector(".vc-send");
  var hostLabel = panel.querySelector(".vc-host");

  function open() {
    panel.style.display = "flex";
    launcher.style.display = "none";
    launcher.removeAttribute("data-waiting");
    input.focus();
    showWaitingOffers();
  }
  function close() {
    panel.style.display = "none";
    launcher.style.display = "flex";
  }
  launcher.addEventListener("click", open);
  panel.querySelector(".vc-x").addEventListener("click", close);

  function scroll() {
    log.scrollTop = log.scrollHeight;
  }
  function say(cls, text) {
    var n = el("div", "vc-msg " + cls, esc(text));
    log.appendChild(n);
    scroll();
    return n;
  }

  // ---- cart shared with the host page --------------------------------------
  async function ensureCart() {
    var key = cfg.cart_storage_key;
    var existing = null;
    try {
      existing = localStorage.getItem(key);
    } catch (e) {
      existing = null;
    }
    if (existing) {
      var check = await fetch(cfg.api_base + "/cart/" + existing);
      if (check.ok) return existing;
    }
    var made = await fetch(cfg.api_base + "/cart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(function (r) {
      return r.json();
    });
    try {
      localStorage.setItem(key, made.cart_id); // the page picks up the same cart
    } catch (e) {}
    return made.cart_id;
  }

  // A reference this browser keeps so a returning shopper's last basket can be
  // found (spec 7.3 reorder). Not an account and not an identity: it is per
  // shop, never leaves this browser except to the shop it belongs to, and if
  // storage is unavailable reorder simply reports that it has no history.
  // The storage names come from the server so the widget and the storefront
  // agree exactly. They did not once: the fallback below used SHOP ("grocery")
  // while the storefront used shop_id ("freshkart"), so a browser could hold
  // two identities and a shopper lost their history by switching between them.
  // The fallback is now derived from the same shop_id the server sends.
  function refKey() {
    return cfg.shopper_storage_key || "velcrow-shopper-" + (cfg.shop_id || SHOP);
  }
  function contactKeyName() {
    return cfg.contact_storage_key || "velcrow-contact-" + (cfg.shop_id || SHOP);
  }

  function ensureShopperRef() {
    try {
      var existing = localStorage.getItem(refKey());
      if (existing) return existing;
      var made = "shp_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
      localStorage.setItem(refKey(), made);
      return made;
    } catch (e) {
      return "";
    }
  }

  // The portable half: whatever contact the shopper has already given this
  // shop, here or on the product page. Sent so history follows the person.
  function storedContact() {
    try {
      return localStorage.getItem(contactKeyName()) || "";
    } catch (e) {
      return "";
    }
  }

  function rememberContact(text) {
    try {
      if (text) localStorage.setItem(contactKeyName(), text);
    } catch (e) {}
  }

  // The host page owns the visible cart; tell it to refetch rather than
  // drawing our own copy of it.
  function announceCartChange() {
    window.dispatchEvent(new CustomEvent("velcrow:cart-changed", { detail: { cartId: cartId } }));
  }

  // ---- one turn ------------------------------------------------------------

  // The reasoning strip (spec 6.5). This is the evidence that the agent chose
  // its own actions, so it is built out of text nodes rather than assembled
  // HTML: every line is set with textContent, and any field the server did not
  // send degrades to a visible placeholder. A bullet can therefore never come
  // out blank — if something is missing the strip says which part is missing
  // instead of rendering an empty row.
  function stepLine(parent, cls, text, fallback) {
    var node = el(cls === "code" ? "code" : "span", cls === "code" ? "" : cls);
    var value = text === undefined || text === null ? "" : String(text);
    node.textContent = value.trim() ? value : fallback;
    parent.appendChild(node);
    return node;
  }

  function renderTrace(steps) {
    if (!steps || !steps.length) return;
    var d = el("details", "vc-trace");
    var word = steps.length === 1 ? "step" : "steps";
    var summary = el("summary");
    summary.textContent = "how I did this · " + steps.length + " " + word;
    d.appendChild(summary);

    var ul = el("ul", "vc-steps");
    steps.forEach(function (s) {
      s = s || {};
      var li = el("li", "vc-step" + (s.ok === false ? " bad" : ""));
      // Prefer the server's ready-made strings; fall back to the raw fields so
      // an older agent build still renders something readable.
      stepLine(li, "code", s.call_display || legacyCall(s), "(tool call not recorded)");
      stepLine(li, "vc-out", s.result_display || legacyResult(s), "(no result recorded)");
      stepLine(li, "vc-why", s.why_display || s.why, "(no reason given)");
      ul.appendChild(li);
    });
    d.appendChild(ul);
    log.appendChild(d);
    scroll();
  }

  function legacyCall(s) {
    if (!s.tool) return "";
    var args = Object.keys(s.args || {})
      .map(function (k) {
        return k + "=" + JSON.stringify(s.args[k]);
      })
      .join(", ");
    return s.tool + "(" + args + ")";
  }

  function legacyResult(s) {
    if (!s.result_summary) return "";
    return s.result_summary + (s.latency_ms ? " · " + s.latency_ms + "ms" : "");
  }

  // ---- the approval gate (spec 5.1, 6.3) -----------------------------------
  // Nothing below moves money on its own. The agent can only produce a quote;
  // this card is the human tap that signs the cart-bound approval, and the
  // wallet's five checks still run server-side after it.

  function addRow(tbody, left, right, cls) {
    var tr = document.createElement("tr");
    var a = document.createElement("td");
    var b = document.createElement("td");
    a.className = cls || "";
    b.className = "r " + (cls || "");
    a.textContent = left;
    b.textContent = right;
    tr.appendChild(a);
    tr.appendChild(b);
    tbody.appendChild(tr);
    return tr;
  }

  function card(cls, heading) {
    var c = el("div", "vc-card" + (cls ? " " + cls : ""));
    var h = el("h4");
    h.textContent = heading;
    c.appendChild(h);
    return c;
  }

  function totalRow(parent, label, value) {
    var t = el("div", "vc-tot");
    var l = document.createElement("span");
    var r = document.createElement("span");
    l.textContent = label;
    r.textContent = value;
    t.appendChild(l);
    t.appendChild(r);
    parent.appendChild(t);
  }

  function renderApproval(q) {
    var c = card("", "Approve this payment");

    var table = el("table", "vc-rows");
    var tbody = document.createElement("tbody");
    table.appendChild(tbody);
    (q.line_items || []).forEach(function (li) {
      var label = li.name + (li.variant ? " (" + li.variant + ")" : "");
      addRow(tbody, label + "  x" + li.qty, li.line_total_display);
    });
    if ((q.coupon_codes || []).length) {
      addRow(tbody, "Coupons " + q.coupon_codes.join(", "),
             "- " + q.coupon_discount_display, "vc-sub");
    }
    c.appendChild(table);
    totalRow(c, "Total", q.charge_display);

    var acts = el("div", "vc-acts");
    var ok = el("button", "vc-ok");
    var no = el("button", "vc-no");
    ok.type = "button";
    no.type = "button";
    ok.textContent = "Approve and pay " + q.charge_display;
    no.textContent = "Not now";
    acts.appendChild(ok);
    acts.appendChild(no);
    c.appendChild(acts);

    var gate = el("div", "vc-gate");
    gate.textContent =
      "Approving signs this exact basket, at this price, to " + cfg.shop_name +
      ". VelcrowAI cannot pay without this tap, and the price is held for five minutes.";
    c.appendChild(gate);

    log.appendChild(c);
    scroll();

    no.addEventListener("click", function () {
      ok.disabled = true;
      no.disabled = true;
      gate.textContent = "Not approved. Nothing was charged.";
    });
    ok.addEventListener("click", function () {
      ok.disabled = true;
      no.disabled = true;
      ok.textContent = "Paying…";
      approve(q, gate, ok);
    });
  }

  async function approve(q, gate, ok) {
    var body;
    try {
      var resp = await fetch(AGENT + "/pay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          shop_id: q.shop_id,
          shop_url: cfg.api_base,
          txn_ref: q.txn_ref,
          mandate_token: mandateToken,
          approved_amount_paise: q.charge_amount_paise,
          // Exactly what the human just saw. The wallet recomputes the cart
          // hash from the shop's own charge and compares, so if these disagree
          // with what is charged the payment dies rather than going through.
          approved_items: (q.line_items || []).map(function (li) {
            return {
              item_id: li.item_id,
              variant: li.variant || "",
              qty: li.qty,
              unit_price_paise: li.unit_price_paise,
            };
          }),
        }),
      });
      body = await resp.json().catch(function () {
        return {};
      });
      if (!resp.ok) {
        renderBlocked(body);
        gate.textContent = "Nothing was charged.";
        ok.textContent = "Payment refused";
        return;
      }
    } catch (e) {
      renderBlocked({ why: "Could not reach the VelcrowAI service at " + AGENT + "." });
      gate.textContent = "Nothing was charged.";
      ok.textContent = "Payment failed";
      return;
    }
    gate.textContent = "Approved by you at " + new Date().toLocaleTimeString() + ".";
    ok.textContent = "Approved";
    renderReceipt(body, q);
    announceCartChange();
  }

  function renderReceipt(r, q) {
    var c = card("vc-paid", "Paid");
    var table = el("table", "vc-rows");
    var tbody = document.createElement("tbody");
    table.appendChild(tbody);
    addRow(tbody, "Shop", cfg.shop_name);
    addRow(tbody, "Reference", r.txn_ref || q.txn_ref, "vc-code");
    if (r.razorpay_order_id) addRow(tbody, "Razorpay test order", r.razorpay_order_id, "vc-code");
    if (r.payment_ref) addRow(tbody, "Payment", r.payment_ref, "vc-code");
    c.appendChild(table);
    totalRow(c, "Charged", q.charge_display);

    var note = el("div", "vc-gate");
    note.textContent = r.confirmed
      ? "The shop has confirmed this order. Every check is in the audit log."
      : "Payment went through, but the shop has not confirmed yet. The wallet entry is logged.";
    c.appendChild(note);
    log.appendChild(c);
    scroll();
  }

  // ---- the comeback sale (spec 7.2) ---------------------------------------
  // This card is the one thing in the widget the shopper did not ask for. It
  // exists because a reservation was taken, the shop restocked while nobody
  // was here, and the agent was told. It is still only an offer: taking it
  // runs the ordinary cart -> quote -> approval -> wallet path.

  function renderOffer(offer) {
    var label = offer.product_name + (offer.variant ? " (" + offer.variant + ")" : "");
    var c = card("", "Back in stock");

    var line = el("div");
    line.style.fontSize = "13px";
    line.style.lineHeight = "1.5";
    line.textContent =
      "You asked me to watch " + label + ". It is back, and I have held your place.";
    c.appendChild(line);

    var table = el("table", "vc-rows");
    var tbody = document.createElement("tbody");
    table.appendChild(tbody);
    addRow(tbody, label + "  x" + offer.qty, offer.line_total_display);
    c.appendChild(table);

    var acts = el("div", "vc-acts");
    var ok = el("button", "vc-ok");
    var no = el("button", "vc-no");
    ok.type = "button";
    no.type = "button";
    ok.textContent = "Add it and check out";
    no.textContent = "No thanks";
    acts.appendChild(ok);
    acts.appendChild(no);
    c.appendChild(acts);

    var gate = el("div", "vc-gate");
    gate.textContent =
      "Held because you reserved it, not because anything was bought. " +
      "You will still see the total and approve it before any money moves.";
    c.appendChild(gate);

    log.appendChild(c);
    scroll();

    no.addEventListener("click", function () {
      ok.disabled = true;
      no.disabled = true;
      gate.textContent = "Dismissed. I will not raise it again.";
      fetch(AGENT + "/agent/offers/" + offer.offer_id + "/decline", { method: "POST" })
        .catch(function () {});
    });
    ok.addEventListener("click", function () {
      ok.disabled = true;
      no.disabled = true;
      gate.textContent = "Adding it to your basket.";
      // Hand it to the ordinary agent loop rather than a special path, so the
      // reserved item goes through the same tools, the same trace and the
      // same approval gate as anything else.
      send("Add the " + offer.qty + " " + label + " I reserved, then check out.");
    });
  }

  var shownOffers = {};

  async function showWaitingOffers() {
    if ((!shopperRef && !contactKey) || busy) return;
    var data;
    try {
      data = await fetch(AGENT + "/agent/offers?shop=" + encodeURIComponent(SHOP) +
                         "&shopper_ref=" + encodeURIComponent(shopperRef) +
                         "&contact_key=" + encodeURIComponent(contactKey))
        .then(function (r) {
          return r.json();
        });
    } catch (e) {
      return; // the service being down must not break the page
    }
    var fresh = (data.offers || []).filter(function (o) {
      return !shownOffers[o.offer_id];
    });
    fresh.forEach(function (o) {
      shownOffers[o.offer_id] = true;
      renderOffer(o);
    });
    // If the panel is shut, mark the launcher so the shopper can see the agent
    // has something for them without being interrupted by it.
    if (fresh.length && panel.style.display === "none") {
      launcher.setAttribute("data-waiting", "1");
    }
  }

  function renderBlocked(err) {
    var c = card("vc-blocked", "Blocked");
    var why = el("div");
    why.style.fontSize = "13px";
    why.style.lineHeight = "1.5";
    why.textContent = (err && (err.why || err.detail)) || "That payment was refused.";
    c.appendChild(why);
    if (err && err.code) {
      var code = el("div", "vc-code");
      code.style.marginTop = "7px";
      code.textContent = err.code;
      c.appendChild(code);
    }
    var note = el("div", "vc-gate");
    note.textContent = "No money moved. The refusal is written to both chain logs.";
    c.appendChild(note);
    log.appendChild(c);
    scroll();
  }

  async function send(text) {
    busy = true;
    sendBtn.disabled = true;
    say("vc-user", text);

    var live = el("div", "vc-live", '<span class="vc-dot"></span><span>working</span>');
    log.appendChild(live);
    scroll();

    var steps = [];
    var quote = null;
    var started;
    try {
      started = await fetch(AGENT + "/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          shop: SHOP,
          cart_id: cartId,
          message: text,
          history: history.slice(-6),
          mandate_token: mandateToken,
          shopper_ref: shopperRef,
          contact_key: contactKey,
        }),
      });
    } catch (e) {
      live.remove();
      say("vc-bot", "I could not reach the VelcrowAI service on " + AGENT + ".");
      busy = false;
      sendBtn.disabled = false;
      return;
    }
    if (!started.ok) {
      var err = await started.json().catch(function () {
        return {};
      });
      live.remove();
      say("vc-bot", err.why || "That request was refused.");
      busy = false;
      sendBtn.disabled = false;
      return;
    }
    var run = await started.json();
    mandateToken = run.mandate_token;

    await new Promise(function (resolve) {
      var es = new EventSource(AGENT + "/agent/run/" + run.run_id + "/events");
      es.onmessage = function (ev) {
        var e = JSON.parse(ev.data);
        if (e.kind === "tool") {
          steps.push(e);
          live.querySelector("span:last-child").textContent = e.tool.replace(/_/g, " ");
        } else if (e.kind === "degraded") {
          var w = el("div", "vc-msg vc-warn",
            "The language model is unreachable, so this reply came from the offline fallback, not the agent.");
          log.appendChild(w);
        } else if (e.kind === "shopper_identified") {
          contactKey = e.contact_key || contactKey;
          rememberContact(e.contact);
        } else if (e.kind === "approval_required") {
          quote = e;
        } else if (e.kind === "message") {
          live.remove();
          renderTrace(steps);
          say("vc-bot", e.text);
          // The card comes after the agent's sentence, so the shopper reads
          // what it did before being asked to approve anything.
          if (quote) renderApproval(quote);
          history.push({ role: "user", content: text });
          history.push({ role: "assistant", content: e.text });
        } else if (e.kind === "error") {
          live.remove();
          // Show the work that did happen before it failed, not just the failure.
          renderTrace(steps);
          say("vc-bot", e.message);
        } else if (e.kind === "cart_changed" && e.changed) {
          announceCartChange();
        }
      };
      es.addEventListener("done", function () {
        es.close();
        resolve();
      });
      es.onerror = function () {
        es.close();
        resolve();
      };
    });

    if (live.isConnected) live.remove();
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    send(text);
  });

  // ---- boot ----------------------------------------------------------------
  (async function boot() {
    var style = el("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    root.setAttribute("data-vc-build", BUILD);
    document.body.appendChild(root);
    console.info("[velcrow] widget build " + BUILD + " from " + AGENT);

    try {
      cfg = await fetch(AGENT + "/agent/config?shop=" + encodeURIComponent(SHOP)).then(function (r) {
        return r.json();
      });
      if (!cfg.api_base) throw new Error(cfg.why || "widget is not configured for this shop");
      cartId = await ensureCart();
      shopperRef = ensureShopperRef();
      // If this browser already knows a contact - typed here or on a product
      // page when reserving - resolve it to its key now, so the very first
      // question about a past order can be answered.
      var saved = storedContact();
      if (saved) {
        try {
          var who = await fetch(cfg.api_base + "/shopper/identify", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ contact: saved, shopper_ref: shopperRef }),
          }).then(function (r) {
            return r.ok ? r.json() : null;
          });
          if (who) contactKey = who.contact_key || "";
        } catch (e) {
          // an unreachable shop must not stop the widget booting
        }
      }
    } catch (e) {
      // Almost always the service simply is not running. Say so, and say
      // what to do about it, rather than surfacing "Failed to fetch".
      hostLabel.textContent = "offline";
      var unreachable = e instanceof TypeError || /fetch/i.test(e.message || "");
      say(
        "vc-bot",
        unreachable
          ? "The VelcrowAI service at " + AGENT + " is not responding, so the agent " +
            "cannot start. Start it with:  python -m uvicorn agent.app:create_app " +
            "--factory --port 8003"
          : "VelcrowAI could not start up here: " + e.message
      );
      sendBtn.disabled = true;
      return;
    }

    hostLabel.textContent = "at " + cfg.shop_name;
    var example = SHOP === "apparel"
      ? "add a medium graphic tee"
      : "add 2 kg lemons under ₹100";
    var hint = el("div", "vc-hint",
      "I can find things, claim the best coupons, bring back your usual order and " +
      "check you out here. Try <em>" + esc(example) +
      "</em>. Nothing is paid until you approve it.");
    log.appendChild(hint);

    // Anything the agent worked out while nobody was here (spec 7.2). Checked
    // on boot and then on a slow poll, so a restock that happens mid-demo
    // surfaces on its own rather than waiting to be asked about.
    showWaitingOffers();
    setInterval(showWaitingOffers, 15000);
  })();
})();
