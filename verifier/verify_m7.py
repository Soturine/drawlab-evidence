#!/usr/bin/env python3
"""Independent verifier of the DrawLab M7 evidence repository (standard library only; no DrawLab code needed).

    git clone https://github.com/Soturine/drawlab-evidence && cd drawlab-evidence
    python verifier/verify_m7.py [--online] [--expect-ancestor <sha> ...]

What it checks INDEPENDENTLY of DrawLab's private code:
  1. every commitment's payload hash, path identity and publication mode;
  2. every commitment file was added by exactly one commit and never modified afterwards (git history);
  3. every ledger file is a valid hash chain and only ever grew (each committed version extends the previous one);
  4. every published ledger event points at the commitment holding the same ticket;
  5. every timing class is re-derived from the availability checks recorded in its receipt;
  6. every score is recomputed from the public official history (official numbers, primary hits over all draws);
  7. with --online, the public history is compared with the official CAIXA API;
  8. with --expect-ancestor, previously recorded commit SHAs are still ancestors of the head (detects rewritten history
     relative to an external snapshot you kept — a repository admin CAN rewrite git history; keep your own snapshots,
     and compare with the GitHub server times recorded in the receipts).

What it CANNOT check without DrawLab's code: that each ticket is the output of the frozen generator on the history
before its target (DrawLab's own verifier does that against the frozen fingerprints).
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

GENESIS = "0" * 64


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checked: dict[str, int] = {}

    def ok(self, name: str) -> None:
        self.checked[name] = self.checked.get(name, 0) + 1

    def fail(self, message: str) -> None:
        self.failures.append(message)


def check_commitments(root: Path, report: Report) -> dict[str, dict]:
    out = {}
    for path in sorted((root / "commitments").rglob("*.json")) if (root / "commitments").exists() else []:
        relative = path.relative_to(root).as_posix()
        # exact path, no --follow: rename detection would attribute similar new files' commits to this one
        commits = git(root, "log", "--format=%H", "--no-renames", "--", relative).split()
        if len(commits) != 1:
            report.fail(f"{relative}: touched by {len(commits)} commits (a published commitment must never change)")
        else:
            report.ok("commitments never modified")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("ticket"), dict):
                raise ValueError("not a commitment object")
        except ValueError as exc:
            report.fail(f"{relative}: malformed commitment ({exc})")
            continue
        body = {k: v for k, v in payload.items() if k != "payload_sha256"}
        identity = payload.get("identity", {})
        expected = f"commitments/{identity.get('league', '').lower()}/{identity.get('game')}/{identity.get('contest')}/{identity.get('bot')}.json"
        if digest(body) != payload.get("payload_sha256"):
            report.fail(f"{relative}: payload hash mismatch")
        elif relative != expected:
            report.fail(f"{relative}: path does not match its identity ({expected})")
        elif payload.get("mode") != "PUBLIC_TICKETS":
            report.fail(f"{relative}: unexpected publication mode {payload.get('mode')}")
        else:
            report.ok("commitment hashes")
        out[relative] = payload
    return out


def check_ledgers(root: Path, report: Report) -> dict[str, list[dict]]:
    ledgers = {}
    for path in sorted((root / "ledger").glob("*.jsonl")) if (root / "ledger").exists() else []:
        relative = path.relative_to(root).as_posix()
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        previous = GENESIS
        for position, entry in enumerate(entries):
            body = {k: v for k, v in entry.items() if k != "entry_hash"}
            if entry.get("seq") != position or entry.get("prev_hash") != previous or digest(body) != entry.get("entry_hash"):
                report.fail(f"{relative}: entry {position} breaks the hash chain")
                break
            previous = entry["entry_hash"]
        else:
            report.ok("ledger chains")
        versions = []
        for commit in reversed(git(root, "log", "--format=%H", "--", relative).split()):
            shown = subprocess.run(["git", "show", f"{commit}:{relative}"], cwd=root, capture_output=True)
            versions.append(shown.stdout if shown.returncode == 0 else b"")
        if any(not later.startswith(earlier) for earlier, later in zip(versions, versions[1:])):
            report.fail(f"{relative}: a committed version does not extend the previous one (history rewritten or truncated)")
        else:
            report.ok("ledgers append-only")
        ledgers[relative] = entries
    return ledgers


def derive_timing(receipt: dict) -> str:
    verdicts = [(receipt.get("availability_check_1") or {}).get("verdict"), (receipt.get("availability_check_2") or {}).get("verdict")]
    if "PUBLISHED" in verdicts:
        return "CATCH_UP"
    if not receipt.get("remote_confirmed") or verdicts != ["UNPUBLISHED", "UNPUBLISHED"]:
        return "UNCERTAIN"
    return "PROSPECTIVE_LIVE"


def recheck_verdict(check: dict, target: int) -> str | None:
    latest = (check.get("latest") or {}).get("contest")
    state = (check.get("target_endpoint") or {}).get("state")
    if (latest is not None and latest >= target) or state == "PUBLISHED":
        return "PUBLISHED"
    if latest is not None and latest < target and state == "NOT_PUBLISHED":
        return "UNPUBLISHED"
    return "UNKNOWN"


def load_history(root: Path) -> dict[str, dict[int, list[list[dict]]]]:
    """Per game and contest, every official version in order: the original, then each provider amendment."""
    history: dict[str, dict[int, list[list[dict]]]] = {}
    originals: dict[str, dict[int, list[dict]]] = {}
    for path in sorted((root / "history").glob("*.jsonl")) if (root / "history").exists() else []:
        if path.name.endswith(".amendments.jsonl"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                originals.setdefault(record["game_id"], {}).setdefault(int(record["contest_id"]), []).append(record)
    for game, contests in originals.items():
        for contest, draws in contests.items():
            history.setdefault(game, {})[contest] = [draws]
    for path in sorted((root / "history").glob("*.amendments.jsonl")) if (root / "history").exists() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                amendment = json.loads(line)
                game = amendment["draws"][0]["game_id"]
                history.setdefault(game, {}).setdefault(int(amendment["contest"]), []).append(amendment["draws"])
    return history


def primary_hits(ticket_numbers: list[int], draws: list[dict], positioned: bool) -> int:
    total = 0
    for draw in sorted(draws, key=lambda d: d["draw_index"]):
        numbers = list(draw["numbers"])
        total += sum(a == b for a, b in zip(ticket_numbers, numbers)) if positioned else len(set(ticket_numbers) & set(numbers))
    return total


POSITIONED = {"supersete"}


def check_events(ledgers: dict[str, list[dict]], commitments: dict[str, dict], history, report: Report) -> None:
    for relative, entries in ledgers.items():
        reveals: dict[str, int] = {}
        for entry in entries:
            key = canonical(entry["identity"])
            if entry["state"] == "REVEALED":
                reveals[key] = reveals.get(key, 0) + 1
            identity, data = entry["identity"], entry["data"]
            path = f"commitments/{identity['league'].lower()}/{identity['game']}/{identity['contest']}/{identity['bot']}.json"
            if entry["state"] in ("COMMITTED", "CATCH_UP", "UNCERTAIN"):
                payload = commitments.get(path)
                if payload is None:
                    report.fail(f"{relative} seq {entry['seq']}: no public commitment at {path}")
                    continue
                if payload["ticket"] != data.get("ticket"):
                    report.fail(f"{relative} seq {entry['seq']}: ledger ticket differs from the public commitment")
                receipt = data.get("receipt") or {}
                for name in ("availability_check_1", "availability_check_2"):
                    check = receipt.get(name)
                    if check and recheck_verdict(check, int(identity["contest"])) != check.get("verdict"):
                        report.fail(f"{relative} seq {entry['seq']}: {name} verdict does not follow from its recorded answers")
                expected = {"PROSPECTIVE_LIVE": "COMMITTED", "CATCH_UP": "CATCH_UP", "UNCERTAIN": "UNCERTAIN"}[derive_timing(receipt)]
                if derive_timing(receipt) == "PROSPECTIVE_LIVE" and receipt.get("timing") != "PROSPECTIVE_LIVE":
                    expected = {"CATCH_UP": "CATCH_UP"}.get(receipt.get("timing"), "UNCERTAIN")  # a stricter server-time rule may downgrade
                if entry["state"] != expected and not (entry["state"] == "UNCERTAIN" and expected == "COMMITTED"):
                    report.fail(f"{relative} seq {entry['seq']}: state {entry['state']} but the receipt supports {expected}")
                else:
                    report.ok("timing classes re-derived")
            if entry["state"] == "SCORED":
                versions = history.get(identity["game"], {}).get(int(identity["contest"])) or []
                version = reveals.get(key, 0) - 1  # scored against the version revealed just before it
                draws = versions[version] if 0 <= version < len(versions) else None
                payload = commitments.get(path)
                if not draws or not payload:
                    report.fail(f"{relative} seq {entry['seq']}: scored without public history or commitment")
                    continue
                hits = primary_hits(payload["ticket"]["official_numbers"], draws, identity["game"] in POSITIONED)
                if hits != data.get("hits"):
                    report.fail(f"{relative} seq {entry['seq']}: score {data.get('hits')} but the public history gives {hits}")
                else:
                    report.ok("scores recomputed")


def check_online(history, report: Report, limit: int) -> None:
    for game, contests in history.items():
        for contest in sorted(contests)[-limit:]:
            request = urllib.request.Request(f"https://servicebus2.caixa.gov.br/portaldeloterias/api/{game}/{contest}",
                                             headers={"Accept": "application/json", "User-Agent": "drawlab-evidence-verifier"})
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                official = json.loads(response.read())
            numbers = [int(n) for n in official["listaDezenas"]]
            ours = sorted(contests[contest], key=lambda d: d["draw_index"])[0]["numbers"]
            if (numbers if game in POSITIONED else sorted(numbers)) != list(ours):
                report.fail(f"history {game} {contest}: differs from the official API")
            else:
                report.ok("history vs official API")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--online-limit", type=int, default=5)
    parser.add_argument("--expect-ancestor", nargs="*", default=[])
    args = parser.parse_args()
    root = Path(args.root).resolve()
    report = Report()
    commitments = check_commitments(root, report)
    ledgers = check_ledgers(root, report)
    history = load_history(root)
    check_events(ledgers, commitments, history, report)
    for sha in args.expect_ancestor:
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=root).returncode == 0
        report.ok("expected ancestors") if ancestor else report.fail(f"{sha} is not an ancestor of HEAD: history was rewritten")
    if args.online:
        check_online(history, report, args.online_limit)
    result = {"verified": not report.failures, "checked": report.checked, "failures": report.failures,
              "not_independent": "ticket = frozen generator(history before target) needs DrawLab's code and its frozen fingerprints"}
    print(json.dumps(result, indent=1))
    return 0 if not report.failures else 1


if __name__ == "__main__":
    sys.exit(main())
