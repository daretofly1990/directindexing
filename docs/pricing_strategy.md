# Pricing Strategy — Direct Indexing Platform

**Last updated:** 2026-04-19

**Context:** Phase 1 targets self-directed individual investors ($500K–$2M, fee-conscious). Phase 2 targets Registered Investment Advisers managing client accounts. The same codebase serves both, but pricing models should differ fundamentally.

---

## The fundamental pricing split

Existing direct-indexing products price in basis points of AUM (Wealthfront 0.25%, Frec 0.10%, Schwab Personalized Indexing 0.40%, Parametric 0.25–0.55%). They can because they custody the assets. This platform does not custody — money stays at the customer's existing broker — so the AUM model doesn't fit the economic reality, and more importantly, **flat subscription is the differentiator for Phase 1**. Fee-conscious positioning falls apart if pricing matches the incumbents.

For Phase 2 (RIAs), the frame inverts. RIAs charge AUM to their own clients (typically 1%) and expect vendor tools to price per-seat or per-client. A consumer-style flat subscription feels cheap and unserious in the RIA buying process.

---

## Phase 1 — Self-directed individual investors

### The value math

At $1M portfolio with ~1% annual tax alpha from direct indexing:

| Metric | Value |
|---|---|
| Annual tax alpha generated | ~$10,000 |
| What Wealthfront charges to capture it | 0.25% AUM = $2,500/yr |
| % of value Wealthfront captures | ~25% |
| Target capture rate for this platform | 5–15% of value |
| Resulting annual price envelope | $500–$1,500/yr |
| Monthly feel | $40–$125/mo |

### Recommended tiers

| Tier | Price | Ceiling | Who it's for |
|---|---|---|---|
| **Starter** | $29/mo | $500K portfolio, one taxable account | Newer investors, single account |
| **Standard** | $59/mo | $2M, household linking (spouse + IRA for wash-sale scoping) | Core ICP |
| **Premium** | $99/mo | Unlimited portfolio size, multiple external accounts, priority support | $2M+, complex situations |

- Annual billing at 20% discount (effectively two months free)
- 14-day full-feature trial — no feature crippling
- No permanent free tier

### Positioning

> "$1M at Wealthfront costs $2,500/yr in fees. Same tax alpha at your existing broker = $708/yr. Your money doesn't move; you keep it at Schwab. Pocket the difference."

At $2M the savings widen to ~$4,000/yr. The larger the portfolio, the better the value prop — which naturally attracts the upper end of the ICP.

### What NOT to do in Phase 1

- No basis-point pricing (defeats the positioning)
- No per-trade or per-harvest pricing (creates perverse incentives — customers skip harvests to avoid fees)
- No permanent free tier (attracts non-converting users, burns Claude API cost)
- No "premium AI" upsell — the AI advisor is the core product and cannot be paywalled

---

## Phase 2 — RIAs

### Recommended tiers

| Tier | Price | Who it's for |
|---|---|---|
| **Solo** | $299/advisor/mo, up to 25 households | Solo RIAs, XYPN-style practitioners |
| **Firm** | $199/advisor/mo + $20/household/mo | 2–50 advisor firms |
| **Enterprise** | Custom (roughly 3–8 bps of AUM under advisement on the platform) | 50+ advisors, white-label, SSO, dedicated CSM |

### Sanity check

A solo RIA with 40 households averaging $800K = $32M AUM, charging 1% = $320K/yr gross revenue. At the Solo tier that's $3,588/yr on the tool — about 1.1% of revenue. This is well inside what RIAs pay for Orion, Redtail, or Tamarac, and the tool saves 5–10 hours per client per year on TLH (worth $2K–$5K per client at advisor billable rates).

### Tier rationale

- **Solo is deliberately flat** because solo RIAs hate per-client pricing (feels like a "tax on growth")
- **Firm tier uses per-household** to match how larger firms budget
- **Enterprise is bps of AUM** because the buyer at that scale (CFO / COO) thinks in bps anyway, and it scales with the firm rather than punishing them for hiring advisors

---

## The arbitrage problem

Without guardrails, a sophisticated RIA signs up 50 "individual" accounts at $59/mo each ($35,400/yr total) instead of the Firm tier ($15,000–$25,000/yr), defeating the Phase 2 pricing. Three-layer defense:

1. **ToS prohibition:** Self-directed accounts are for personal use only. Using the product "on behalf of others for compensation" is a violation — legally enforceable and a real fiduciary-duty issue for the RIA.
2. **Feature fencing:** The RIA tier includes multi-client dashboard, bulk harvest scans, white-label client reports, compliance audit exports, and an advisor-approval workflow. An RIA running their practice off 50 individual accounts would be doing manual aggregation that defeats the purpose.
3. **Onboarding friction:** Individual signup flow requires the user to be the beneficial account owner and attest so. Signing up as someone else is fraud on the ToS.

This combination doesn't catch every edge case, but it makes the RIA tier clearly the right product for RIAs.

---

## Launch strategy

Operational decisions that matter more than the specific numbers:

- **Launch higher than feels comfortable.** Raising prices on existing customers is hard; lowering is easy. Start at $29/$59/$99 rather than $19/$39/$79.
- **Grandfather early users** if prices rise later. Protects goodwill. Early users are also the marketing.
- **Annual prepay is worth ~20% discount.** Cash flow matters when small. Annual subscribers churn at roughly 1/3 the rate of monthly.
- **Don't discount at launch.** No "beta pricing" or "early-bird" rates. A customer who only buys at 50% off is not the customer.
- **Do offer a founding-member benefit** instead — e.g., "first 100 customers get a lifetime price lock." Feels like a perk, doesn't discount the anchor price.
- **Revisit pricing at month 12.** LTV, CAC, and churn data will be meaningful by then; reprice with data.

---

## Unit economics

Rough per-customer monthly cost at $59/mo tier:

| Item | Monthly cost |
|---|---|
| Anthropic API (Claude) — ~4 harvest scans/mo | $2–6 |
| Finnhub live quotes | <$0.50 (included in plan) |
| yfinance historicals | $0 |
| Hosting / DB / Caddy (amortized) | $1–3 |
| Stripe transaction fees | $2 |
| **Total COGS** | **$5–12** |
| **Gross margin** | **80–90%** |

Margins are healthy. Pricing is set by value and competitive dynamics, not cost floors.

---

## Pricing milestones tied to the TODO

- **Pre-launch (before first paying customer):** Stripe Billing integration (TODO Milestone 8), tier gates in the backend (portfolio size caps, household limits), ToS enforcement copy
- **Month 3 post-launch:** First pricing retrospective. Are Starter users upgrading to Standard? Is Premium converting? Adjust tier ceilings if needed.
- **Month 6:** Phase 2 (RIA) pricing pages live, even if product features are still being built — lets early RIA interest inbound-book demos
- **Month 12:** Full pricing review with real LTV/CAC/churn data. Consider price increases for new customers; grandfather existing.
- **Month 18+:** Enterprise tier and custom contracts as larger RIA firms enter pipeline

---

## Open questions worth deciding before launch

- Does the free trial require a credit card upfront? (Higher intent, lower trial count; industry standard for this price point is yes)
- Is there a family/household discount for a second user in the same household? (Probably no — household linking is already included in Standard+)
- Is there a referral program at launch or later? (Later — TODO Milestone 9)
- Does a one-time setup fee make sense for complex portfolios? (No — friction for no benefit)
- Should there be an a la carte "tax-report for your CPA" purchase for users who don't want full subscription? (Not at launch — too complex, dilutes subscription story)
