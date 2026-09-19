"""Source-identity probe — verifies the Polymarket API contract on the VPS.

Read-only, small, rate-limited. Prints a pass/fail report for every
⚠ PENDING-VPS item in docs/source-identity-contract.md.

Usage:
    python backend/scripts/probe_source_identity.py
"""

from __future__ import annotations

import json
import time
from collections import Counter
from urllib.request import Request, urlopen

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

UA = {"User-Agent": "polycopy-source-audit/0.1"}


def fetch(url: str) -> object:
    req = Request(url, headers=UA)
    with urlopen(req, timeout=20) as resp:  # noqa: S310 — public read-only APIs
        return json.loads(resp.read())


def check_trades_identity() -> dict:
    trades = fetch(f"{DATA_API}/trades?limit=200")
    composite = Counter(
        (
            t["transactionHash"], t["proxyWallet"], t["asset"],
            str(t["size"]), str(t["price"]), str(t["timestamp"]),
        )
        for t in trades
    )
    collisions = {k: v for k, v in composite.items() if v > 1}
    tx_multi_fill = Counter(
        (t["transactionHash"], t["proxyWallet"], t["asset"]) for t in trades
    )
    multi = {k: v for k, v in tx_multi_fill.items() if v > 1}
    return {
        "sample_size": len(trades),
        "composite_collisions": len(collisions),
        "txs_with_multiple_fills_same_wallet_asset": len(multi),
        "outcome_index_values": sorted({t.get("outcomeIndex") for t in trades}, key=str),
        "ok": len(collisions) == 0,
    }


def check_positions(wallet: str) -> dict:
    positions = fetch(f"{DATA_API}/positions?user={wallet}&limit=5")
    return {"ok": isinstance(positions, list), "fields": sorted(positions[0]) if positions else []}


def check_gamma(condition_id: str) -> dict:
    markets = fetch(f"{GAMMA_API}/markets?condition_ids={condition_id}")
    m = markets[0] if markets else {}
    return {
        "ok": bool(m),
        "has_clobTokenIds": "clobTokenIds" in m,
        "clobTokenIds": m.get("clobTokenIds"),
        "outcomes": m.get("outcomes"),
    }


def check_clob(condition_id: str) -> dict:
    m = fetch(f"{CLOB_API}/markets/{condition_id}")
    tokens = m.get("tokens", []) if isinstance(m, dict) else []
    return {
        "ok": bool(tokens),
        "token_ids": [t.get("token_id") for t in tokens],
    }


def main() -> None:
    report: dict = {}

    print("== Data API /trades identity ==")
    report["trades"] = check_trades_identity()
    print(json.dumps(report["trades"], indent=2))
    time.sleep(1)

    # Use a wallet + market observed in the trade sample for the other checks.
    trades = fetch(f"{DATA_API}/trades?limit=1")
    sample_wallet = trades[0]["proxyWallet"]
    sample_condition = trades[0]["conditionId"]
    time.sleep(1)

    print("== Data API /positions ==")
    report["positions"] = check_positions(sample_wallet)
    print(json.dumps(report["positions"], indent=2))
    time.sleep(1)

    print("== Gamma API /markets ==")
    report["gamma"] = check_gamma(sample_condition)
    print(json.dumps(report["gamma"], indent=2))
    time.sleep(1)

    print("== CLOB API /markets/{condition_id} ==")
    report["clob"] = check_clob(sample_condition)
    print(json.dumps(report["clob"], indent=2))

    all_ok = all(v.get("ok") for v in report.values())
    print("\nRESULT:", "PASS" if all_ok else "FAIL — update the contract doc")


if __name__ == "__main__":
    main()
