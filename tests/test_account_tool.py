import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from close_call_fold import Fold  # noqa: E402
from close1_account import Archive, reconcile  # noqa: E402

SAMPLE = ROOT / "examples" / "sample-season.jsonl"


def build_archive(directory: Path):
    """Write the sample season as a sweep-record archive; return the fold and the final price."""
    config = json.loads((ROOT / "contest.json").read_text())
    fold, sweeps, final = Fold(config), [], None
    with localcontext() as ctx:
        ctx.prec = 60
        for line in SAMPLE.read_text().splitlines():
            event = json.loads(line)
            if event["t"] == "seed":
                fold.seed(event["px"])
            elif event["t"] == "sweep":
                output = fold.sweep(event["n"], event["ref"], event["close"], event["owners"], event["trades"])
                data = json.dumps({"input": event, "output": output}, sort_keys=True,
                                  separators=(",", ":")).encode()
                digest = hashlib.sha256(data).hexdigest()
                path = f"sweeps/{digest}.json"
                (directory / "sweeps").mkdir(exist_ok=True)
                (directory / path).write_bytes(data)
                sweeps.append({"n": event["n"], "file": digest, "path": path, "status": "full", "bytes": len(data)})
            else:
                final = event["px"]
    (directory / "index.json").write_text(json.dumps({"contest": "sample", "sweeps": sweeps}))
    return Fold(config), fold.final(final), Decimal(final)


class AccountToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "archive").mkdir()
        self.fresh_fold, self.final, self.s = build_archive(root / "archive")
        self.archive = Archive(str(root / "archive"), root / "cache")

    def tearDown(self):
        self.tmp.cleanup()

    def replay(self, owner):
        index = self.archive.index()
        with localcontext() as ctx:
            ctx.prec = 60
            return reconcile(owner, ((n, self.archive.record(index[n])) for n in sorted(index)), self.fresh_fold)

    def test_every_owner_matches_the_folds_standings(self):
        for row in self.final["standings"]:
            result = self.replay(row["key"])
            account = result["account"]
            self.assertIsNotNone(account, row["key"])
            self.assertEqual(account.position, Decimal(row["position"]), row["key"])
            self.assertEqual(account.fees, Decimal(row["fees"]), row["key"])
            score = (account.value_at(self.s) - self.fresh_fold.mint).quantize(Decimal("0.000001"))
            self.assertEqual(score, Decimal(row["score"]), row["key"])

    def test_funds_voids_name_the_failing_side(self):
        seen = 0
        for row in self.final["standings"]:
            for void in self.replay(row["key"])["void"]:
                if void["reason"] == "funds" and void["role"] != "self":
                    self.assertIn(void["failed"], ("own side", "other side"))
                    seen += 1
        self.assertGreater(seen, 0, "the sample season has a funds void")
        # the two sides of one funds void never both pass the check
        by_id = {}
        for row in self.final["standings"]:
            for void in self.replay(row["key"])["void"]:
                if void["reason"] == "funds" and void["role"] != "self":
                    by_id.setdefault((void["sweep"], void["id"]), []).append(void["failed"])
        for sides in by_id.values():
            self.assertIn("own side", sides)

    def test_an_owner_never_minted_has_no_account(self):
        result = self.replay("did:key:z6Mk" + "1" * 44)
        self.assertIsNone(result["account"])
        self.assertEqual(result["settled"], [])

    def test_a_tampered_full_record_is_refused(self):
        index = self.archive.index()
        entry = index[min(index)]
        path = Path(self.archive.base) / entry["path"]
        path.write_bytes(path.read_bytes().replace(b'"sweep"', b'"sweeP"', 1))
        with self.assertRaises(ValueError):
            self.archive.record(entry)


if __name__ == "__main__":
    unittest.main()
