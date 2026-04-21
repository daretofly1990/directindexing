# Launch Strategy — Direct Indexing Platform

**Last updated:** 2026-04-19

**Decision:** Path B with a hedge toward Path C.

- **Path B:** Finish Phase 1 (self-directed) code, then build Phase 2 (RIA) during the 3–5 month regulatory wait for RIA registration approval. Launch Phase 1 on approval, Phase 2 shortly after.
- **Hedge to Path C:** In parallel with Phase 2 engineering, conduct structured RIA customer discovery. If 3+ RIAs provide strong commitments (design partnerships or letters of intent) by the decision point, flip launch order — ship Phase 2 first while waiting for Phase 1 regulatory approval, use Phase 2 revenue to fund the regulatory setup cost.

---

## Why this plan

- Phase 2 has no regulatory gate. Selling software to RIAs does not require you to be an RIA yourself. The 3–5 month ADV approval timeline only blocks Phase 1.
- The regulatory wait is otherwise dead calendar time. Using it to build and potentially sell Phase 2 is a pure accelerator, not a distraction.
- Path C's upside is real (RIA revenue funds regulatory setup; 2–5 RIA customers at $299–$5K/mo covers the $20–60K legal stack and leaves runway). Path C's downside is low (if RIA signal is weak, Path B proceeds unchanged).
- The decision point in month 3 forces a clean go/no-go rather than open-ended optionality.

---

## Two parallel tracks

### Track 1 — Phase 1 engineering (weeks 1–12)

Per TODO.md Milestones 1–8. Engineering runs at normal pace, targeting beta-ready around week 12–13.

### Track 2 — RIA customer discovery (weeks 1–12, overlapping)

Discovery runs alongside engineering, does not steal Phase 1 dev time. Rough weekly allocation: 4–8 hours/week of founder time on outreach + interviews.

### Track 3 — Regulatory (weeks 1–ongoing)

Counsel engagement, entity formation, Form ADV filing. Starts week 1. State approvals expected weeks 10–20.

### Track 4 — Phase 2 engineering (weeks 13–24)

Starts after Phase 1 code is complete. Overlaps with regulatory approval window. If Path C triggers at month 3, Phase 2 launches in weeks 20–24; if not, Phase 2 launches shortly after Phase 1.

---

## RIA discovery playbook

### Target profile

- **Firm size:** Solo practitioners or 2–10 advisor firms. Larger firms have procurement bureaucracy that will delay month-3 decisions.
- **Fee model:** Fee-only RIAs (NAPFA, XYPN members). Commission-based brokers won't pay for fiduciary-aligned tools.
- **Client base:** Mass-affluent to lower-HNW ($500K–$5M households). This matches the product's sweet spot.
- **TLH posture:** Already does TLH manually, or wants to but doesn't because it's too time-consuming. Not: already uses Parametric/Aperio/55ip at scale (those RIAs have committed budget elsewhere).

### Source list (targets of 10–15 conversations)

- NAPFA member directory — filter by AUM range and service offering
- XY Planning Network — younger, more tech-forward RIAs
- Fee-Only Network — similar to NAPFA with different overlap
- Kitces.com author list and commenters — engaged, sophisticated, opinionated about tools
- Twitter/X "fin-X" community — Doug Boneparth, Bill Harris, others
- LinkedIn search: "RIA" + "tax-loss harvesting" or "direct indexing"
- Warm intros from the network — ask current contacts for RIAs they respect

### Outreach template (adapt per target)

```
Subject: quick question on your TLH workflow

Hi [Name] — I'm building a tool that uses an LLM to do tax-loss harvesting
analysis for individual investors, specifically at the $500K-$2M range where
direct indexing normally starts becoming viable. The AI ingests the client's
lot-level CSV and produces a proposed harvesting plan the advisor can review.

Wanted to ask whether this would be useful at your practice, and if so what
30-45 min of your time would be worth — happy to pay, share the prototype
early, or just buy you a coffee if you're in [city]. Either way, I'd find
your perspective valuable.

Thanks,
[You]
```

Goal: 25–40% response rate, book 8–12 calls.

### Interview structure (30–45 min)

Open: 5 min context-setting, what you're building, no pitch yet.

Questions to cover (don't robot-recite; flow naturally):

1. How do you currently handle TLH for your clients? Quarterly? Annually? Ad-hoc?
2. How long does a full TLH pass take you per client?
3. What tools do you use today? Parametric? Excel? Orion? Something custom?
4. Where's the pain — the tax computation, the wash-sale tracking, the replacement selection, the client communication, the trade execution, or something else?
5. Do you track wash-sale at the household level (spouse accounts, IRAs)? How?
6. What's your approval model — discretionary with client trust, or per-trade client approval?
7. Have you considered direct indexing for clients, and why haven't you rolled it out?
8. If I showed you a tool that produced a reviewed-ready TLH plan in 5 minutes for a 200-lot portfolio, what would you pay per month for it?
9. What would have to be true for you to switch off your current workflow?

Close: show prototype if ready, ask about design-partner interest, and request 1–2 warm intros to other RIAs they respect.

### Signal tiers

| Signal | What they say | What it means |
|---|---|---|
| **Cold** | "Interesting, good luck" | Not a buyer. Move on. |
| **Soft** | "I'd take a look when it's ready" | Keep in touch; low conversion. |
| **Medium** | "Send me a demo when you have it" | Legitimately interested; convert 15–25% of these. |
| **Strong** | "I'd be a design partner — give me access and I'll give you feedback" | Real commitment. Track toward a paid conversion. |
| **Very strong** | "I'd pay $X/mo once it's live; here's a signed letter of intent" | Path C trigger. |

### Decision criteria — month 3 checkpoint

Trigger Path C (ship Phase 2 first) if **all three**:

- [ ] 3+ RIAs at Strong or Very Strong signal
- [ ] At least 1 Very Strong signal (signed LOI or verbal commitment to pay at a specific price)
- [ ] Phase 2 UI feasible to ship in 8–10 weeks (confirmed by engineering at month 3)

Otherwise Path B proceeds — Phase 1 launches on regulatory approval, Phase 2 launches shortly after.

Document the decision and rationale in this file at the month-3 checkpoint regardless of outcome.

---

## SOC 2 timing

If Path B: start SOC 2 Type I process month 6 post-launch, target completion month 12–15. Most RIA firms above single-advisor shops require SOC 2 in vendor questionnaires.

If Path C: start SOC 2 Type I immediately upon triggering Path C (month 3). Target completion month 9–12. Likely to be requested by firms 2–3 of the early cohort.

SOC 2 costs to budget: auditor fees $15–40K, internal effort 100–200 hours, readiness consultant optional ($10–25K) but recommended for first-time.

---

## Revised master timeline

| Week | Phase 1 Engineering | RIA Discovery | Regulatory | Phase 2 Engineering |
|---|---|---|---|---|
| 1 | M1 starts (individual persona) | Build target list of 15 RIAs, draft outreach | Engage counsel, form entity | — |
| 2–3 | M1 + M2 in parallel | First 10 outreaches sent | ADV drafting begins | — |
| 4 | M2 continues | 3–5 interviews done | ADV Part 2A brochure | — |
| 5 | M3 (self-approval, trade export) | 5–8 interviews done, prototype walkthroughs begin | ADV filed with IARD | — |
| 6–7 | M4 (compliance wiring + encryption) | Prototype demos to warm contacts | State approvals in flight | — |
| 8 | M5 (dividends), M6 (AI safety) | LOI conversations with strongest contacts | — | — |
| 9 | M6 continues | Month-3 decision checkpoint | — | — |
| 10 | M7 (production hardening) | If Path C: Phase 2 design partner kickoff | State approvals expected | If Path C: design & schema begin |
| 11 | M8 (billing) | If Path C: weekly design partner calls | — | If Path C: UI build begins |
| 12 | Beta with 5–10 friendly users | — | — | — |
| 13–14 | Beta fixes, M9 (growth loops) | — | — | Phase 2 build continues |
| 15 | Phase 1 public launch | — | — | Phase 2 build continues |
| 20 | — | — | — | If Path C: Phase 2 launch to design partners |
| 24 | — | — | — | Phase 2 public launch |

If Path C: Phase 2 launches weeks 20–24 (before Phase 1 in the unlikely case Phase 1 ADV is still pending). If Path B: Phase 2 launches week 24–26, shortly after Phase 1.

---

## Commitments that follow from this plan

- Engineering continues on TODO.md Milestones 1–8 at full pace; RIA discovery does not pull Phase 1 dev time
- Founder commits 4–8 hours/week to RIA outreach and interviews through week 12
- Month-3 decision is documented in this file with the signal tier of each RIA interviewed
- Phase 2 UI speculation is minimized — only build what RIA conversations specifically validate, not what seems "probably useful"
- Regulatory track runs on its own calendar, not gated by either product launch

---

## What this plan does NOT commit to

- Does not commit to launching Phase 2 publicly regardless of RIA interest. If all 15 conversations come back Cold/Soft, Phase 2 is still built (for the eventual launch) but not rushed.
- Does not commit to any specific pricing with RIA design partners. Use the tier framework from pricing_strategy.md as the starting point; negotiate down to reasonable design-partner rates (e.g., 50% off for the first 6 months in exchange for detailed feedback + case-study permission).
- Does not commit to SOC 2 Type II in year one. Type I is the bar for early RIA customers; Type II can wait.
- Does not commit to paid marketing or content for Phase 2 in year one. Early distribution is 1-on-1 founder sales.
