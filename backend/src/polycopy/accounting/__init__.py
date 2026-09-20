"""Wallet accounting: settlement feed + validated realized P&L.

Design rule (from the rebuild plan): accounting is validated BEFORE
scoring consumes it. Nothing in this package writes WalletScore rows —
it produces plain, testable numbers that PR-D (scoring) will consume.

* ``pnl`` — pure cash-flow math. No DB, no network.
* ``settlements`` — detect resolved markets via Gamma, record them.
* ``service`` — DB-backed assembly: per-wallet realized P&L from trades
  + settlements, and best-effort reconciliation against the Data API
  ``/positions`` realizedPnl figure.
"""
