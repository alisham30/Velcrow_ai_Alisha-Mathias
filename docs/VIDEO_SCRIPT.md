# VelcrowAI - 5-minute demo video script

Ten scenes. Each has SCREEN (what is visible), DO (your actions), SAY (your
line - speak it naturally, do not read it stiffly). Total runtime just under
five minutes. Record scenes separately if easier and cut together.

## Before recording - the ritual (10 minutes)

1. `.\run_all.ps1` - all ten services up.
2. Start the tunnel in its own window:
   `& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8003 --no-autoupdate`
   If the URL changed, re-point the Meta webhook (ask Claude Code: "re-point
   the webhook to <new url>").
3. Text "hi" to the WhatsApp number from your phone (keeps the 24h window fresh).
4. Quit Claude Desktop from the tray and reopen it (fresh MCP server).
5. Phone charged, WhatsApp open, Do Not Disturb ON everywhere else.
6. Browser tabs ready, in order: FreshKart (:5173), FreshKart console
   (:5173/console), Buyer app (:5175), Audit (:5175/audit), Orders
   (:5173/orders), the architecture diagram (README on GitHub or the artifact).
7. Screen recorder capturing system audio + mic; phone screen-mirrored or
   filmed over-the-shoulder for the WhatsApp scenes.

---

## Scene 1 - The thesis (0:00-0:25)

SCREEN: the architecture diagram (README).
DO: slow scroll from tier 1 to tier 5.
SAY: "A single agent in a single channel cannot grow commerce - real buying
happens everywhere, and revenue is lost exactly when the customer is not on
your site. So we built VelcrowAI: one agent brain behind every channel -
the shop page, WhatsApp, other people's AIs - with one hard rule: models
make the judgments, but every rule that touches money is code that can
refuse. Six merchants, five surfaces, one wallet. Let me show you."

## Scene 2 - The widget (0:25-0:55)

SCREEN: FreshKart storefront, widget open.
DO: type "add 2 kg lemons and some honey". Point at the trace strip as it
grows, then at the coupon line and the cross-sell sentence.
SAY: "Plain language in. The model picks its own tools - watch the trace.
The shop does every bit of arithmetic: best coupon claimed automatically,
and a cross-sell backed by real paid baskets - 'bought together in six past
baskets'. The count is the evidence. The model cannot invent a price or a
recommendation; it can only relay what the shop computed."

## Scene 3 - The money, and the villain (0:55-1:25)

SCREEN: checkout, then the console's cheat toggle.
DO: pay normally once (approve the exact amount). Then console -> cheat mode
ON -> try to buy again -> show the red PRICE_CHANGED block and the trust
score drop. Turn cheat mode off.
SAY: "Paying means approving one exact amount - and this wallet has five
ordered checks. Now the villain: this merchant inflates the bill AFTER I
approved. The wallet kills it - PRICE_CHANGED, nothing paid, trust halved.
The model can be manipulated. It still cannot spend."

## Scene 4 - WhatsApp: the agent in your pocket (1:25-2:05)

SCREEN: your phone (mirrored), WhatsApp chat with the agent.
DO: text "find me a cotton kurti under 1500". Show the ranked list with the
over-budget option refused. Reply "1". Tap Approve on the quote. Show the
receipt.
SAY: "Same brain, in my pocket. A goal with a budget - and that 1500 became
the wallet's hard cap, minted from my own words. Three merchants compared,
one option refused for breaking my budget, with the arithmetic. I reply
with a number, tap Approve on an exact amount, and it is paid - receipt in
the chat, order in my history. It also messages me first: when something I
was refused comes back in stock, it completes my basket and quotes the
total. One message, never two."

## Scene 5 - The merchant's agent (2:05-2:35)

SCREEN: FreshKart console.
DO: click Run Now. Show a proposal card (model's own rationale + numbers).
Approve it. Point at the bandit note.
SAY: "While nobody watches, the merchant's agent reads real sales and the
lost-demand ledger, simulates every idea, and files a card - or honestly
says nothing is worth doing. Every number here came from a simulation, not
the model's imagination, because three code gates make sure of it. My
decision trains a bandit: strategies I keep rejecting stop being led with."

## Scene 6 - Agent versus agent (2:35-3:05)

SCREEN: terminal + the audit negotiation view.
DO: run `.\.venv\Scripts\python.exe -m lab.negotiate_demo`. Scroll the three
arcs. Open one side-by-side audit link.
SAY: "Two agents with opposed interests. The buyer opens low and hides its
ceiling; the shop's floor is its cost book - cost plus twelve percent, in
integer paise. Deal, deal at the floor, and a walk-away - and the agreed
price is a signed token, spendable exactly once. Both sides' chains record
the same number. Not one rupee here was chosen by a model."

## Scene 7 - A stranger's AI buys (3:05-3:30)

SCREEN: terminal + the manifest in a browser tab.
DO: open /.well-known/agent-commerce.json on :8005, then run
`.\.venv\Scripts\python.exe -m lab.third_party_buyer http://127.0.0.1:8005`.
SAY: "This client has never seen these shops and imports none of our code.
It reads one manifest, speaks the Agentic Commerce Protocol, and pays in
full - at a merchant we onboarded this week with one YAML file. The
merchant is not sellable to MY agent. It is sellable to ANY agent."

## Scene 8 - Claude buys from us (3:30-4:10)

SCREEN: Claude Desktop.
DO: ask Claude to buy a kurta. Show it refuse to quote, hand you the login
link. Open the link, show the login page (do NOT show your number to the
camera - blur or angle). Enter the OTP from WhatsApp. Tell Claude "done".
Show the quote, allow pay_quote, show the receipt. Flip to :5173/orders and
point at the purple "AI assistant (MCP)" chip.
SAY: "And here is another company's AI - Claude - shopping our network over
MCP. It cannot even QUOTE until I log in, and look how: it hands me a link,
I log in on my own page, my number and code never enter the chat. Then it
buys with my approval on an exact amount - and the order lands in MY
history, tagged with the door it came through. Five doors now: web,
WhatsApp, our buyer agent, ACP, MCP. One wallet behind all of them."

## Scene 9 - Everything is provable (4:10-4:40)

SCREEN: audit page, then a terminal.
DO: verify chains (green). Hit Tamper - red at the index. Run
`.\.venv\Scripts\python.exe -m lab.determinism_check` and show the two
traces. Flash BREAKAGE.md with a slow scroll.
SAY: "Every action of every actor is on an append-only hash chain - tamper
with one entry and it goes red at that exact index. And the proof this is
an agent, not a script: the same sentence at two stock states produces two
different tool traces - the check fails itself if they ever match. This
file is every real bug we hit, including four found live by a real shopper,
each with its fix and a regression test. Evidence you can attack is
evidence worth trusting."

## Scene 10 - Close (4:40-5:00)

SCREEN: the architecture diagram again, or the Orders page with all the
source chips visible.
SAY: "VelcrowAI. Six merchants, five surfaces, one brain, one wallet, four
hundred tests. Agents that decide, code that refuses, chains that remember.
Every rule that ever stopped an agent here lives in code, not in a prompt -
and that is why you can let it near money."

---

## If something breaks mid-take

- WhatsApp silent -> the tunnel died: restart it, re-point the webhook, redo
  the ritual step 2.
- A shop refuses for stock -> restock from its console; the demo continues.
- Claude Desktop tools missing -> quit from the tray, reopen.
- Do not narrate failures away - if the wallet blocks something honestly,
  that IS the demo. Say so and keep the take.
