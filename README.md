# VelcrowAI

A trust layer for agentic commerce. Merchants plug in and their shop becomes sellable to any AI agent; shoppers get one agent, on the web or in WhatsApp, that can buy anywhere in the network but can never be made to overpay.

## Brief

Commerce is about to be agent-to-agent, and neither side is ready to trust it. Shoppers will not let an AI near their money; merchants lose refusals, abandoned carts, and restock moments silently, with no way to prove what happened afterwards. VelcrowAI answers both with one architectural rule: judgment comes from models, but every rule that touches money or truth is deterministic code that can refuse.

Three LLM agents make the decisions. A shopping assistant (nine tools) serves the shopper on the storefront widget and over WhatsApp; a growth agent (seven tools) wakes hourly per shop, reads real sales and lost-demand numbers, simulates every idea, and files proposal cards or honestly concludes "no action"; a message router judges whether each WhatsApp text is an instruction inside one shop or a goal to satisfy across all of them. Around them stands the machinery no model can talk its way past: HMAC-signed session mandates and cart-bound approvals, a 76-line wallet with five ordered checks as the only door to money, signed single-use negotiation tokens, signature-verified webhooks, and append-only SHA-256 hash chains that make every action of every actor tamper-evident and auditable from both sides.

The result runs end to end today: a shopper is refused for stock, the shortfall is valued in a four-state demand ledger, a restock completes their basket and one WhatsApp message quotes the exact total, a tap pays it through the same wallet as every other sale, and the order appears in their history naming the door it came through. A stranger's client can buy through the published Agentic Commerce Protocol; two agents with opposed interests negotiate signed deals; a determinism check fails the build if the agent ever behaves like a script. Payments are Razorpay test mode and messaging is Meta's test tier throughout - the same discipline: real APIs, no strangers reachable, no real rupees moved. 395 tests pass.

## Architecture

<p align="center"><img src="docs/architecture.svg" width="980" alt="VelcrowAI architecture - five tiers, one door to money"></p>

Models think in tier 3, arithmetic lives in tier 4, money passes only through the wallet, and every actor's every action lands on a hash chain in tier 5.

## Agent-to-agent flow

<p align="center"><img src="docs/agent_to_agent.svg" width="980" alt="Agent to agent - who talks to whom"></p>

The two shops never talk to each other. Buying side and selling side meet only at published endpoints, and money only at the wallet.

## User flow

<p align="center"><img src="docs/user_flow.svg" width="980" alt="One shopper's week - the user flow"></p>

The shopper never creates an account, never types a password or card number, and never has to trust anyone: every charge is an exact amount they saw and tapped, checked by code.

## Branches of the project

<p align="center"><img src="docs/branches.svg" width="980" alt="Branches of the project"></p>

## Run it

```
.\run_all.ps1          # six services, one command
.\run_all.ps1 -Stop    # stop everything
```

Storefronts at http://localhost:5173 and :5174, buyer app and audit at :5175, merchant consoles at /console on each storefront. Razorpay is test mode; WhatsApp is Meta's test number tier. No real money moves anywhere in this project.
