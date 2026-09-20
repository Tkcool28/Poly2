"""Wallet scoring V1 — the "what makes a wallet smart" spec, as code.

Everything here implements docs/wallet-intelligence.md sections 3–4:

* Eligibility gates (cheap yes/no checks) run BEFORE scoring. Fail any →
  rejected, reason recorded.
* Five interpretable components, weighted: P&L quality 25%, concentration
  20% (hard reject >50%), profit factor 20%, consistency 20%, recency 15%.
* Verdicts: ≥70 → pending_review (human decides) · 50–69 → stays
  discovered · <50 → rejected.

No Kelly, no Sharpe, no clustering in V1 — add-later candidates only when
paper evidence shows a specific weakness (see the doc's Evidence Loop).
"""
