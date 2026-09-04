# What broke, and how we got out

The full log is `BREAKAGE.md` at the repo root - every real bug, in the order
it hit, with the cause, the fix and the regression test. These are the ones
worth telling. One pattern runs through all of them: **when the agent went
wrong, the fix was never a better prompt. It was moving the rule into code.**

## 1. The agent walked through every guardrail written in prose

The merchant growth agent had three rules in its prompt: simulate before you
conclude, propose only what a simulation supports, never file the same card
twice. Live, it broke all three in one run - read a ledger showing lost
revenue, wrote a paragraph about the "clear opportunity", filed a restock
card BEFORE running the simulation, then filed a second card for the same
item. Prompts are requests; the model treated them as suggestions.

**Out:** three gates in code. Concluding without simulating the worst line
sends it back exactly once, and the send-back is chain-logged. A proposal
without a supporting simulation is refused. The same kind and item cannot be
proposed twice. On camera now you can watch the model get sent back, redo
the work, and honestly conclude "no action". That is the demo.

## 2. The proposal that existed only as prose

The pre-submission full check: the agent simulated a restock, the numbers
supported it, and the model wrote "I propose to restock Cow Ghee" in its
closing summary - without ever calling the tool that files the card. The
merchant's console showed nothing while the text claimed a proposal.

**Out:** a supported simulation with no card triggers one push: file it, or
state why you are discarding it. An explicit discard stands. A proposal
that exists only as prose is indistinguishable from no proposal, so the
code no longer lets one exist.

## 3. A float invented a rupee

The negotiation floor was cost plus twelve percent. `ceil(90000 * 1.12)` is
100,801 in IEEE 754 arithmetic - one paisa above the merchant's actual
policy, in the one module whose entire claim is exact pricing.

**Out:** integer basis points with ceiling division. The project rule was
already "money is integer paise, never floats"; this is why the rule exists.
Caught while writing the test's worked example, before a single test ran.

## 4. The ledger never closed, so the whole system reported money it was not losing

A refusal that had been restocked, notified and BOUGHT went on being counted
as demand still outstanding. The merchant console showed a hole that was
already filled; the growth agent read that hole and proposed spending cash
on it; the Revenue Lab could not credit the recovery it had just measured.

**Out:** the ledger settles. Four honest states - outstanding, told,
recovered, lapsed - settled only when a real paid order answers a real
refusal, and only for as many units as the basket actually supplies.

## 5. A cancelled checkout stranded the shelf (found live by a real shopper)

The shopper placed an order for 18 lemons - stock held behind the price
lock - then cancelled payment. The hold was released lazily, only when that
order was fetched again, which a cancelled checkout never does. The product
page said "out of stock" indefinitely while 18 units sat in a dead order.
The shopper's own retry was refused for stock the shop was not short of.

**Out:** any path about to read or judge stock releases every lapsed hold
first. Regression test: place an instantly lapsing order, walk away, and a
plain product read must show the shelf restored.

## 6. A basket settled its own shortfall (found live by a real shopper)

Wanting 21 lemons with 18 on the shelf correctly recorded a 3-unit refusal.
Paying for the 18 then marked that same refusal "recovered" - settlement
matched shopper and item with no idea which basket the refusal came from.
The restock that followed reported "nobody had asked for this one" while the
person who asked was holding the phone.

**Out:** demand rows carry the cart they came from, and a basket can never
settle its own shortfall. A later basket still can.

## 7. The offer peddled the unit and ignored the basket (found live, and it stung)

Wanting 6 dupattas with 5 on the shelf, the restock WhatsApp message said
"1 is back - pay Rs 899", ignored the other 5, and left the cart untouched.
Technically correct, completely wrong as a shop.

**Out:** the returned units go back INTO the basket they were refused from -
the shopper's original ask, no money moved - and the message quotes the
completed basket with its coupons. Approve pays for all of it through the
same wallet. A second restock cannot stuff the basket again.

## 8. "Are there any cushions" stayed at the grocer - twice

The WhatsApp router judged which shop a message belonged to from shop names
and category words. A cushion question asked mid-grocery conversation stayed
at FreshKart, whose honest search found zero. First fix: feed the router each
shop's catalog vocabulary. Live again: with "sells ... dupatta" printed in its
menu, the model STILL kept "Dupattas" at the grocer.

**Out:** the realisation that matching words to catalogs was never judgment -
it is lexical work. The router now splits along the build's own doctrine: the
model judges intent (instruction vs cross-shop goal), deterministic code picks
the shop by scoring the message against the shops' full catalog text.
"jaggery?" now finds the one shop that sells jaggery. Rule: when a model keeps
fumbling a subtask, check whether the subtask was ever judgment at all.

## 9. The punishment rolled itself back

The OTP verifier counted wrong attempts and burned the code after three - and
raised the error INSIDE `with sqlite3.connect(...)`, whose context manager
rolls the transaction back on exception. The attempt count silently reset on
every wrong guess. Unlimited tries.

**Out:** decide inside the transaction, commit, raise after. Caught by the
test that tries three wrong codes and then the right one.

## 10. A test quietly called the real model

A routing test inherited the developer's OpenAI key from the environment and
made a live API call; the model routed the test's message somewhere the stub
did not expect. Deleting the key in the test fixture did NOT fix it: the app's
`load_dotenv()` re-fills a deleted variable from `.env` but never overrides an
existing one - the key resurrected itself mid-test.

**Out:** the fixture sets the key to empty instead of deleting it. Every LLM
call in tests now raises and the deterministic fallbacks take over; a test
that wants model behaviour stubs it explicitly.

---

The reason the audit trail on this project can be believed is the same
reason this file exists: we kept the evidence when it was embarrassing.
Every entry above has a regression test in `tests/`, and the system that
shipped is the one that survived them.
