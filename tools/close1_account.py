#!/usr/bin/env python3
"""Check one owner's close-1 account against the published sweep records.

The records at https://challenges.technocore.chat/close-1/ hold each sweep's fold input and output.
This replays the trades one owner was a side of, with the package's own accounting
(`close_call_fold.Account` and `Fold.side_fees`), and prints the owner's mint, cash, position, fees
and score at a mark. For each trade that names the owner and was void it says why, and for a
`funds` void whether the owner's own side failed the check or the other side did.

    python3 tools/close1_account.py <did:key> [--from N] [--to N] [--mark PRICE]

Only the owner's own trades change its account, so the replay needs only the sweeps from the mint
on. It reads nothing else and writes only its cache (`CLOSE1_CACHE`, default `close1-cache/`).
Standard library only. Not part of the package: the referee does not use it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from decimal import Decimal, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from close_call_fold import DID, Account, Fold  # noqa: E402

ARCHIVE = "https://challenges.technocore.chat/close-1/"


class Archive:
    """The sweep records, from the published archive or a local copy of it, cached by path."""

    def __init__(self, base: str, cache: Path):
        self.base = base if base.endswith("/") else base + "/"
        self.cache = cache

    def _get(self, path: str) -> bytes:
        if "://" not in self.base:
            return (Path(self.base) / path).read_bytes()
        with urllib.request.urlopen(self.base + path, timeout=60) as reply:
            return reply.read()

    def index(self) -> dict[int, dict]:
        return {entry["n"]: entry for entry in json.loads(self._get("index.json"))["sweeps"]}

    def record(self, entry: dict) -> dict:
        cached = self.cache / entry["path"]
        if cached.exists():
            data = cached.read_bytes()
        else:
            data = self._get(entry["path"])
            if entry["status"] == "full" and hashlib.sha256(data).hexdigest() != entry["file"]:
                raise ValueError(f"sweep {entry['n']}: record does not match the posted hash {entry['file']}")
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        return json.loads(data)


def own_side_fails(fold: Fold, account: Account, trade: dict, close: Decimal) -> bool:
    """Whether the owner's side alone fails the fold's funds check, before this trade."""
    qty, px = Decimal(trade["qty"]), Decimal(trade["px"])
    side = 1 if trade["side"] == "buy" else -1
    mk_fee, tk_fee = fold.side_fees(side, qty, px, close)
    if trade["maker"] == trade["countersigner"]:
        return account.cash < mk_fee + tk_fee
    if trade["maker"] == account.key:
        return account.cash < account.opening(side, qty) * px + mk_fee
    return account.cash < account.opening(-side, qty) * px + tk_fee


def reconcile(owner: str, records, fold: Fold) -> dict:
    """Replay `records`, (n, record) pairs in sweep order, for one owner."""
    account, minted, last_close, redacted, n = None, None, None, 0, None
    settled, void = [], []
    for n, record in records:
        sweep_in, sweep_out = record["input"], record["output"]
        close = Decimal(sweep_in["close"])
        last_close = close
        if account is None and owner in sweep_out.get("minted", []):
            account, minted = Account(owner, fold.mint), n
        for trade, outcome in zip(sweep_in["trades"], sweep_out["trades"]):
            if "redacted" in trade:
                redacted += 1
                continue
            role = ("self" if trade.get("maker") == owner == trade.get("countersigner")
                    else "maker" if trade.get("maker") == owner
                    else "taker" if trade.get("countersigner") == owner else None)
            if role is None:
                continue
            row = {"sweep": n, "id": trade["id"], "role": role, "side": trade["side"],
                   "qty": trade["qty"], "px": trade["px"]}
            if outcome["outcome"] == "settled":
                fee = Decimal(outcome["maker_fee"]) if role == "maker" else Decimal(outcome["taker_fee"])
                if role == "self":
                    fee = Decimal(outcome["maker_fee"]) + Decimal(outcome["taker_fee"])
                    account.cash -= fee
                    account.fees += fee
                else:
                    side = 1 if trade["side"] == "buy" else -1
                    account.apply(side if role == "maker" else -side, Decimal(trade["qty"]),
                                  Decimal(trade["px"]), fee)
                settled.append({**row, "fee": str(fee)})
                continue
            reason = outcome["reason"]
            if role == "maker" and (reason in ("taker", "settled") or reason == "not_owner" and account is not None):
                # another key countersigned this owner's terms: its attempt, not this owner's
                row["by"] = trade["countersigner"]
            if reason == "funds" and account is not None:
                row["failed"] = "own side" if own_side_fails(fold, account, trade, close) else "other side"
            void.append({**row, "reason": reason})
    return {"owner": owner, "minted": minted, "account": account, "last_sweep": n,
            "last_close": last_close, "redacted": redacted, "settled": settled, "void": void}


def report(result: dict, mint: Decimal, mark: Decimal | None) -> str:
    lines = [f"owner {result['owner']}"]
    account = result["account"]
    if account is None:
        lines.append(f"  no mint in the sweeps read (through {result['last_sweep']})")
        return "\n".join(lines)
    price = mark if mark is not None else result["last_close"]
    lines += [f"  minted at sweep {result['minted']}",
              f"  cash      {account.cash.quantize(Decimal('0.01'))}",
              f"  position  {account.position}",
              f"  fees      {account.fees.quantize(Decimal('0.01'))}",
              f"  score at {price}: {(account.value_at(price) - mint).quantize(Decimal('0.01'))}"
              f"  ({'given mark' if mark is not None else 'close of sweep %s' % result['last_sweep']})"]
    if result["settled"]:
        lines.append(f"  settled ({len(result['settled'])}):")
        lines += [f"    sweep {r['sweep']:<5} {r['id']:<20} {r['role']:<5} {r['side']:<4} {r['qty']:>7} @ {r['px']:>8}"
                  f"  fee {r['fee']}" for r in result["settled"]]
    if result["void"]:
        lines.append(f"  void ({len(result['void'])}):")
        for r in result["void"]:
            note = f"  {r['failed']}" if "failed" in r else f"  attempt by {r['by']}" if "by" in r else ""
            lines.append(f"    sweep {r['sweep']:<5} {r['id']:<20} {r['role']:<5} {r['reason']}{note}")
    if result["redacted"]:
        lines.append(f"  warning: {result['redacted']} trades in these sweeps were posted in private rooms and are"
                     " redacted; any of this owner's among them are missing above")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("owner")
    parser.add_argument("--from", dest="first", type=int, default=1, help="first sweep to read (at or before the mint)")
    parser.add_argument("--to", dest="last", type=int, help="last sweep to read (default: the archive's last)")
    parser.add_argument("--mark", help="price to score open contracts at (default: the last sweep's close)")
    parser.add_argument("--archive", default=ARCHIVE, help="archive URL or a local directory with index.json")
    parser.add_argument("--config", default=str(ROOT / "contest.json"))
    args = parser.parse_args()
    if not DID.fullmatch(args.owner):
        parser.error("owner must be a did:key")
    fold = Fold(json.loads(Path(args.config).read_text()))
    archive = Archive(args.archive, Path(os.environ.get("CLOSE1_CACHE", "close1-cache")))
    index = archive.index()
    last = min(args.last or max(index), max(index))
    print(f"archive covers sweeps {min(index)}..{max(index)}; reading {args.first}..{last}", file=sys.stderr)
    with localcontext() as ctx:
        ctx.prec = 60
        records = ((n, archive.record(index[n])) for n in range(args.first, last + 1) if n in index)
        result = reconcile(args.owner, records, fold)
        print(report(result, fold.mint, Decimal(args.mark) if args.mark else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
