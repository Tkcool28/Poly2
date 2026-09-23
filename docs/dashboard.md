# Dashboard (PR-F)

The dashboard is the operator's phone-first window into the bot. It is
read-mostly: the only actions it can trigger are wallet intake (add a
candidate), the human approval-gate transitions (approve / reject /
disable), and an on-demand scoring run. It cannot place trades, change
sizing, or touch the kill switch.

## Design rules (do not regress)

1. **Phone-first.** The primary device is an Android phone. Navigation is
   a fixed bottom tab bar (thumb reach), every interactive target is at
   least 44px tall, content is single-column cards, never tables.
2. **Plain language.** The operator is not assumed to know trading or
   Polymarket jargon. "Practice mode — no real money", not "PAPER MODE".
   "Copied / Partly copied / Not copied — here's why", not raw status
   enums. Every page states in one sentence what it shows.
3. **Honesty over polish.** If the API is unreachable the banner says so
   and says to assume nothing is running. Miss reasons are translated to
   human sentences (`missReasonText` in `src/lib/api.ts`) — extend that
   mapping when new miss reasons appear, never show raw snake_case.
4. **Safety state is ambient.** The banner always shows practice-vs-live
   and kill-switch state. Live mode renders red and unmistakable.

## Data sources (one endpoint per tab, no client-side joins)

| Tab | Endpoint | Notes |
| --- | --- | --- |
| Home | `/positions`, `/signals`, `/wallets`, `/approval-queue`, `/health/deps` | Headline = realized practice P&L from `positions.totals` |
| Activity | `/signals` | Each item embeds market question, wallet, and the paper-order result |
| Portfolio | `/positions` | Open = `quantity > 0 && settled_at == null`; totals computed server-side |
| Wallets | `/wallets` (GET + POST), POST `/wallets/{id}/disable` | Includes latest composite score; POST is manual candidate intake — the only way a wallet enters the system |
| Review | `/approval-queue`, POST `/wallets/{id}/{action}`, POST `/scoring/run` | The human approval gate; rescoring existing candidates, not wallet discovery |

Backend payloads intentionally carry display context (market question,
wallet address/label) so the frontend never fans out into N+1 requests
on a mobile connection.

All list endpoints are bounded (200–500 rows) — the VPS OOM lesson
applies to read paths too.
