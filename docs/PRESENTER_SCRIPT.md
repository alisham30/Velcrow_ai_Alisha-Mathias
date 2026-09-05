# VelcrowAI - presenter script

Every scene has three parts. SCREEN and DO are what is visible and what you
click. SAY is the spoken line. UNDERNEATH is the technology behind that scene,
in case a judge asks. Spoken lines run about six minutes if every word is
said; the cuts marked "trim" bring it to five.

## Before recording

1. Text "hi" to the WhatsApp number from your phone. This opens the 24-hour
   window Meta requires before the agent may message you.
2. Quit Claude Desktop from the tray and reopen it, so it starts a fresh MCP
   server.
3. Tabs in order: README diagram, FreshKart (:5173), FreshKart console
   (:5173/console), Loomcraft console (:5174/console), buyer app (:5175),
   audit room (:5175/audit), orders (:5173/orders), DailyMandi manifest
   (http://127.0.0.1:8005/.well-known/agent-commerce.json). A terminal at the
   repo root. Phone mirrored or filmed.
4. Check the block print cushion covers at MittiCraft are in stock.

## 1. Thesis

SCREEN: README architecture diagram. DO: slow scroll top to bottom.

SAY: "A single agent in a single channel cannot grow commerce. Real buying
happens everywhere, and revenue is lost exactly when the customer is not on
your site. VelcrowAI is one agent brain behind every channel: the shop page,
WhatsApp, other companies' AIs. One hard rule: models make the judgments,
every rule that touches money is code that can refuse. Six merchants, five
surfaces, one wallet."

UNDERNEATH: seven services, each its own process and database. Six shops
(FastAPI, Uvicorn, SQLite, one YAML config and one JSON catalog each) on
ports 8001 to 8007, and the VelcrowAI layer on 8003. The storefronts, console,
buyer app and audit room are React 18 with Vite and Tailwind. The shops cannot
see each other, the agent cannot see a stock number, and money enters a shop
through exactly one endpoint. Adding a merchant is one YAML plus one JSON.

## 2. The shop page

SCREEN: FreshKart, widget open. DO: type "add 2 kg lemons and some honey".
Point at the trace strip, the coupon line, the cross-sell sentence.

SAY: "Plain language in. Watch the trace strip: the model is choosing its own
tools, search, add, add, coupon. The shop does all the arithmetic. The coupon
was claimed without being asked, and this cross-sell says bought together in
past baskets, with the count. The model cannot invent a price. It can only
relay what the shop computed."

UNDERNEATH: the shopping assistant is OpenAI gpt-4o-mini with function
calling over eleven tools: search_catalog, add_to_cart, update_qty,
remove_line, view_cart, apply_best_coupons, reorder_last, identify_shopper,
start_checkout, search_network, switch_shop. Every tool returns money as a
ready-made rupee string, so the model never converts paise. Stock reaches the
model only as in stock or out of stock, never as a number, so it cannot cap an
order to fit the shelf. One add per product per turn is enforced in code. The
cross-sell count comes from the shop's paid baskets. The trace strip is
server-sent events, built on the server so it cannot be prettified.

## 3. Paying

SCREEN: checkout. DO: approve the exact amount, pay, show the confirmation.

SAY: "Paying means approving one exact amount. Behind this button is one
wallet with five ordered checks: mandate valid, amount matches, not spent
before, shop trusted, price unchanged. Nothing else in the system can move
money."

UNDERNEATH: the mandate is a signed JWT (PyJWT, HS256) with a per-payment cap,
a total cap, an expiry and a nonce. The approval is cart-bound and signed by
the shopper's tap. The wallet is 76 lines, the only file allowed to import the
Razorpay SDK, and a test fails the build if any other file does. Razorpay test
mode, exact-amount capture. Money is integer paise everywhere.

## 4. The villain

SCREEN: console, cheat mode ON. DO: storefront, add anything, checkout,
approve. Show the red PRICE_CHANGED block. Console: trust score halved. Cheat
mode OFF.

SAY: "Now this merchant inflates the bill after approval. The wallet refuses
it, PRICE_CHANGED, nothing paid, and the shop's trust score is cut in half.
The model can be manipulated. It still cannot spend."

UNDERNEATH: cheat mode makes the shop quote honestly and inflate the bill at
payment time, which is the real attack. The wallet compares the bill to the
approved mandate and refuses. The trust score halves on evidence only, and the
buyer agent ranks shops by it. The first version of this demo had the villain
lie in the quote, so the human approved the lie and every check agreed; that
is in the breakage log.

## 5. WhatsApp: the network in your pocket

SCREEN: phone. DO: text "any cushions?" Both home shops with prices. Text
"the mitticraft one". Switched. Text "add the block print cushion covers",
then "pay". Tap Approve. Receipt.

SAY: "Same brain, in my pocket, and it knows the whole network. Two shops sell
cushions, so it shows both with prices and moves me to the one I pick. Nobody
scripted that: the model chose to search the network and chose to switch, and
each choice is on the audit chain with its reason. Then I say pay, tap Approve
on an exact amount, paid."

UNDERNEATH: Meta WhatsApp Cloud API through a cloudflared tunnel. Every
webhook is verified with an HMAC-SHA256 signature and deduplicated by message
id. A message router decides the mode: the model judges instruction versus
cross-shop goal, and deterministic code picks the shop by scoring the words
against the live catalogs, because matching words to catalogs is lookup, not
judgment. When more than one shop sells the goods, the fact is placed in the
search result and the model decides whether to call search_network. The tap
on Approve is the human approval; it can authorise only the quoted amount.
Restock and abandoned-cart offers are say-once: one message per person per
item, and a paid offer never silences the next one.

## 6. WhatsApp: a goal with a budget

SCREEN: phone. DO: text "find me a cotton kurti under 1500". Ranked list,
refused over-budget option. Reply "1". Tap Approve. Receipt.

SAY: "Now a goal instead of an instruction. Three apparel shops compared, one
refused for breaking my budget, with the maths shown. That 1500 became the
wallet's hard cap, minted from my own words. It also messages me first: when
something I was refused comes back, it completes my basket and quotes the
total. One message, never two."

UNDERNEATH: the buyer agent is deterministic Python with zero model calls:
rules plus trust-weighted ranking across all six shops. The stated budget
becomes the mandate cap. The restock rescue puts the refused units back into
the basket they came from and quotes the whole basket with coupons.

## 7. The merchant's console

SCREEN: FreshKart console, top tiles, then the lost demand table.

SAY: "This is the merchant's console, and every number on it comes from paid
orders only. Nothing here is estimated. Revenue is what was actually charged.
Coupons claimed: most were claimed by the agent without the shopper asking.
Sales rescued is the number I care about most: five orders from shoppers this
shop had already turned away for stock. That money did not exist before. Now
the honest one. Average order is lower with the agent than without. That is
correct, not a bug. The agent claims coupons the shopper would have missed,
and it closes small exact baskets from a chat. Nobody pads a basket on
WhatsApp. So the scoreboard shows the cost next to the gain. The pitch is not
that the agent makes baskets bigger. It is that it recovers sales that were
zero. Every order is stamped assisted or not the moment it is placed, so this
split is measured, not modelled. Down here is lost demand: what shoppers are
still asking for. Each row says whether the fix is stock or just a message."

Trim: stop after "recovers sales that were zero".

UNDERNEATH: revenue is the sum of charge_amount over paid orders. Average is
revenue over orders. Coupons claimed counts orders whose stored coupon has a
code. Sales rescued sums paid orders flagged rescued, set when a paid order
closed a refusal in the demand ledger. The ledger has four states:
outstanding, told, recovered, lapsed; a row only becomes recovered when a real
paid order answers a real refusal, and a basket can never settle its own
shortfall. Lost demand value is outstanding units times current price. The
line under each value is the restock simulation's verdict.

## 8. The growth agent

SCREEN: Loomcraft console. DO: Run now. Point at the send-back in the trace,
the simulation, the card with its numbers. Approve.

SAY: "While nobody watches, the merchant's agent reads real sales and the
lost-demand ledger. Watch it conclude too early, get sent back by a code
gate, run the simulation, and hear: buy nothing, four units are already on the
shelf, the shoppers were never told. So it files a notify card, with the
simulation attached by code, not by the model. I approve, and the people it
was bought for get the offer. Every number here came from a simulation, and my
decision trains a bandit, so ideas I keep rejecting stop being led with."

UNDERNEATH: gpt-4o-mini with seven tools: sales metrics, demand ledger,
inventory, margins, simulate_discount, simulate_restock, create_proposal.
Three gates in code: concluding without simulating the worst line sends it
back exactly once; a proposal without a supporting simulation is refused; the
same kind and item cannot be filed twice, and the shop refuses a duplicate of
any open card. Card kinds: restock, notify, campaign, coupon. A notify is
allowed only when the simulation reports refused demand already on the shelf,
and approving it fires the same callbacks as a restock without buying stock.
Approving a restock now tells the people it was bought for. Strategy order is
Thompson sampling: one Beta distribution per shop and kind, approval bumps
alpha, rejection bumps beta, stdlib random, no numpy, because twenty decisions
is not a dataset.

## 9. Agent versus agent

SCREEN: terminal. DO: run `.\.venv\Scripts\python.exe -m lab.negotiate_demo`.
Scroll the three arcs. Open the audit link it prints.

SAY: "Two agents with opposed interests. The buyer opens low and hides its
ceiling. The shop's floor is cost plus twelve percent in integer paise. A
deal, a deal at the floor, and a walk-away. The agreed price is a signed
token, spendable once. Both chains record the same number. Not one rupee here
was chosen by a model."

UNDERNEATH: the floor is integer basis points with ceiling division, after a
float once invented a rupee. Offers are HMAC-SHA256 tokens. The buyer repeats
its number once before walking, so the shop's closing rule can fire.

## 10. A stranger's AI buys

SCREEN: manifest tab, then terminal. DO: show the manifest, run
`.\.venv\Scripts\python.exe -m lab.third_party_buyer http://127.0.0.1:8005`.

SAY: "This client has never seen these shops and imports none of the code. It
reads one manifest, speaks the Agentic Commerce Protocol, and pays in full, at
a merchant onboarded with one YAML file. The shop is not sellable to one
agent. It is sellable to any agent."

UNDERNEATH: the ACP adapter follows the live 2026-04-17 spec, read rather than
remembered, because the spec has no quantity field and forbids extra
properties. Idempotency keys: a retried completion returns the identical
receipt, byte for byte. A completed session prices itself from the order, not
the emptied cart. Native and ACP quote the same total for the same basket.

## 11. Claude buys

SCREEN: Claude Desktop. DO: type "buy me a cotton kurta from velcrow". It
refuses to quote and gives a link. Open the link, angle the camera away from
your number, enter the OTP from WhatsApp. Back in Claude type "done". Allow
pay_quote. Receipt. Orders tab, point at the purple chip.

SAY: "This is Claude Desktop. Another company's AI, not ours, connected over
MCP, the Model Context Protocol, which is how an assistant gets tools. Watch
what happens before it can even quote. The tool refuses: not logged in. It
hands me a link. I log in on my own page. My number and the code go to
WhatsApp and to this page. They never enter the chat. Claude never sees them.
Now it can quote: same shops, same prices, same wallet as every other door. It
asks before paying, and I allow it once. Paid, through the same five checks.
Two hard caps on this door that Claude cannot raise: a per-payment limit and
a session total. It could be wrong, it could be malicious. It still cannot
spend more than that, and it cannot pay an amount other than the one it
quoted. And the order lands in my history, tagged with the door it came
through. Five doors. One wallet behind all of them. The merchant did nothing
to get this."

UNDERNEATH: MCP Python SDK 2.x over stdio. Ten tools: list_shops,
search_products, create_cart, add_to_cart, get_quote, pay_quote, order_status,
login_link, check_login, whoami (masked). Quote and pay refuse until link
login completes, and the link login completes only through a successful OTP on
the shop's own page. OTP codes are hashed, expire in five minutes, burn after
three attempts, rate limited. Hard caps: 5,000 rupees total, 3,000 per
payment. Every order carries a source: storefront, widget, whatsapp, acp,
buyer_app, mcp.

## 12. Audit room

SCREEN: audit tab. DO: Trace, Negotiations, Dispute, Revenue Lab, Trust,
Chains last. Re-verify, then Tamper on the buyer card, once.

SAY, Trace: "This is the reasoning strip for one shopper turn: every tool the
model called, the arguments it chose, the reason it gave, and what the shop
answered. Built server side, so nothing can prettify it."

SAY, Negotiations: "Both sides of one deal, each on its own chain, recording
the same price."

SAY, Dispute: "When two agents disagree about what was owed, the buyer's
account sits beside the shop's. Both were written independently, both are
tamper-evident, and a mismatch is named with the indices that prove it."

SAY, Revenue Lab: "Every figure traces to a paid order, and the cost sits
beside the gain. The first version of this lab multiplied guesses and printed
them next to real rupees. It was thrown away, and the log says so."

SAY, Trust: "Every shop has a score that moves on evidence only. A price
change after approval halves it, and the buyer quotes that shop last."

SAY, Chains: "Seven chains, one per actor. Each entry is hashed over the one
before it. Re-verify recomputes every hash from the first line: all green. Now
I do what an attacker with disk access would do, rewrite one past entry. Red
at that exact index, and every link after it. The past cannot be quietly
fixed."

Trim: Trace, Chains, and one line for the rest.

UNDERNEATH: each chain is an append-only JSONL file. Hash equals SHA-256 of
the previous hash plus the canonical JSON of the entry, from a genesis of
sixty-four zeros. Same construction as a blockchain without consensus, because
there is one writer per chain and the goal is detection. Tamper really edits
the file, once, so tell me afterwards and I restore it.

## 13. Proof it is an agent

SCREEN: terminal, then BREAKAGE.md. DO: run
`.\.venv\Scripts\python.exe -m lab.determinism_check`, then scroll the file to
the bottom.

SAY: "Same sentence, two stock states, two different tool traces. The check
fails itself if they ever match. And this file: every real bug hit during the
build, with its fix and its test. Five found live today by a real shopper,
including a fix that was thrown away the same evening because it made the
agent stop being one. Evidence you can attack is evidence worth trusting."

UNDERNEATH: 414 tests, including import guards that fail the build if any file
but the wallet imports Razorpay, any file but one imports OpenAI, or any file
but one talks to Meta. Tests point at a dead port so they can never pass or
fail depending on what happens to be running.

## 14. Close

SCREEN: orders tab with every source chip visible.

SAY: "Six merchants, five surfaces, one brain, one wallet, four hundred and
fourteen tests. Agents that decide, code that refuses, chains that remember.
Every rule that ever stopped an agent here lives in code, not in a prompt.
That is why it can be let near money."

## If asked what is not used

No LangChain or LangGraph: orchestration is plain Python you can point at,
and every handoff between agents is chain-logged. No fine-tuned model: the
only learning is the bandit, deliberately tiny. Nowhere does a model do
arithmetic on money, choose a price, or see a stock number.

## If something breaks mid-take

WhatsApp silent: the tunnel died, restart it and re-point the webhook. A shop
refuses for stock: restock from its console and continue. Claude Desktop
tools missing: quit from the tray, reopen. If the wallet blocks something
honestly, that is the demo. Say so and keep the take.
