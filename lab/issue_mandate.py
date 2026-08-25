r"""Issue a Tier-1 session mandate from the command line (Phase 1 harness —
in later phases the :8003 agent service does this when the shopper states
intent).

Example (PowerShell):
  .\.venv\Scripts\python.exe -m lab.issue_mandate --max-total 500000 --max-per-txn 300000 --shops freshkart,loomcraft
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

from common import mandate


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Issue a session mandate (amounts in integer paise)")
    ap.add_argument("--max-total", type=int, required=True, help="budget cap in paise")
    ap.add_argument("--max-per-txn", type=int, required=True, help="per-transaction cap in paise")
    ap.add_argument("--shops", required=True, help="comma-separated shop ids, e.g. freshkart,loomcraft")
    ap.add_argument("--ttl", type=int, default=3600, help="validity in seconds (default 3600)")
    args = ap.parse_args()

    token = mandate.issue(args.max_total, args.max_per_txn, args.shops.split(","), args.ttl)
    claims = mandate.verify(token)
    print(f"jti:          {claims['jti']}")
    print(f"max_total:    {claims['max_total']} paise")
    print(f"max_per_txn:  {claims['max_per_txn']} paise")
    print(f"shops:        {claims['shops']}")
    print(f"expires (unix): {claims['exp']}")
    print()
    print(token)


if __name__ == "__main__":
    main()
