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

  var cfg = null;
  var cartId = null;
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
    input.focus();
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

  // The host page owns the visible cart; tell it to refetch rather than
  // drawing our own copy of it.
  function announceCartChange() {
    window.dispatchEvent(new CustomEvent("velcrow:cart-changed", { detail: { cartId: cartId } }));
  }

  // ---- one turn ------------------------------------------------------------
  function renderTrace(steps) {
    if (!steps.length) return;
    var d = el("details", "vc-trace");
    var word = steps.length === 1 ? "step" : "steps";
    d.innerHTML = "<summary>how I did this &middot; " + steps.length + " " + word + "</summary>";
    var ul = el("ul", "vc-steps");
    steps.forEach(function (s) {
      var args = Object.keys(s.args || {})
        .map(function (k) {
          return k + "=" + JSON.stringify(s.args[k]);
        })
        .join(", ");
      var li = el("li", "vc-step" + (s.ok ? "" : " bad"));
      li.innerHTML =
        "<code>" + esc(s.tool) + "(" + esc(args) + ")</code>" +
        '<span class="vc-out">' + esc(s.result_summary) + " &middot; " + s.latency_ms + "ms</span>" +
        '<span class="vc-why">' + esc(s.why) + "</span>";
      ul.appendChild(li);
    });
    d.appendChild(ul);
    log.appendChild(d);
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
        } else if (e.kind === "message") {
          live.remove();
          renderTrace(steps);
          say("vc-bot", e.text);
          history.push({ role: "user", content: text });
          history.push({ role: "assistant", content: e.text });
        } else if (e.kind === "error") {
          live.remove();
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
    document.body.appendChild(root);

    try {
      cfg = await fetch(AGENT + "/agent/config?shop=" + encodeURIComponent(SHOP)).then(function (r) {
        return r.json();
      });
      if (!cfg.api_base) throw new Error(cfg.why || "widget is not configured for this shop");
      cartId = await ensureCart();
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
      "I can find things in this shop and build your basket. Try <em>" + esc(example) +
      "</em>. Coupons and checkout are not wired up yet.");
    log.appendChild(hint);
  })();
})();
