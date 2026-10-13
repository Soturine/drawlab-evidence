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


def commits_by_path(root: Path, folder: str) -> dict[str, list[str]]:
    """Every commit that touched each path under ``folder`` (exact paths, no rename detection), in one git call:
    the same answer as ``git log --no-renames -- <path>`` per file, without one process per file."""
    out: dict[str, list[str]] = {}
    current = None
    for line in git(root, "log", "--format=@%H", "--name-only", "--no-renames", "--", folder).splitlines():
        if line.startswith("@"):
            current = line[1:]
        elif line.strip() and current:
            out.setdefault(line.strip(), []).append(current)
    return out


def check_commitments(root: Path, report: Report) -> dict[str, dict]:
    out = {}
    touched = commits_by_path(root, "commitments")
    for path in sorted((root / "commitments").rglob("*.json")) if (root / "commitments").exists() else []:
        relative = path.relative_to(root).as_posix()
        # exact path, no --follow: rename detection would attribute similar new files' commits to this one
        commits = touched.get(relative, [])
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
            shown = subprocess.run(["git", "cat-file", "blob", f"{commit}:{relative}"], cwd=root, capture_output=True)  # no path stat (long paths)
            versions.append(shown.stdout if shown.returncode == 0 else b"")
        if any(not later.startswith(earlier) for earlier, later in zip(versions, versions[1:])):
            report.fail(f"{relative}: a committed version does not extend the previous one (history rewritten or truncated)")
        else:
            report.ok("ledgers append-only")
        ledgers[relative] = entries
    return ledgers


# ---- m7-timing-v2: copied verbatim from drawlab/league/timing.py (a parity test keeps them identical) ----
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

TIMING_RULE = "m7-timing-v2"
CLOCK_ALLOWANCE = timedelta(seconds=2)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_http_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def classify_commitment(check_1: Mapping[str, Any] | None, check_2: Mapping[str, Any] | None, *, confirmed: bool,
                        push_time: Any) -> tuple[str, str]:
    """(timing, reason) under ``m7-timing-v2``; fails closed."""
    check_1, check_2 = check_1 or {}, check_2 or {}
    verdicts = [check_1.get("verdict"), check_2.get("verdict")]
    if "PUBLISHED" in verdicts:
        return "CATCH_UP", "the official result was published at an availability check"
    if confirmed is not True:
        return "UNCERTAIN", "the remote did not serve back the committed bytes"
    if verdicts != ["UNPUBLISHED", "UNPUBLISHED"]:
        return "UNCERTAIN", f"availability checks were {verdicts}, not both UNPUBLISHED"
    pushed = _parse_iso(push_time)
    if pushed is None:
        return "UNCERTAIN", "no verifiable GitHub server push time for the commit"
    first = _parse_http_date((check_1.get("latest") or {}).get("provider_date"))
    second = _parse_http_date((check_2.get("latest") or {}).get("provider_date"))
    if first is None or second is None:
        return "UNCERTAIN", "a provider Date header is missing or unparseable"
    if first > second:
        return "UNCERTAIN", "provider Date of check 1 is after check 2 (contradictory provider clock)"
    if pushed < first - CLOCK_ALLOWANCE:
        return "UNCERTAIN", "the server push time is before the first availability check (order not demonstrable)"
    if pushed > second + CLOCK_ALLOWANCE:
        return "UNCERTAIN", "the server push time is after the second availability check (order not demonstrable)"
    return "PROSPECTIVE_LIVE", "both checks unpublished around a remotely confirmed publication, in causal order"
# ---- end of the copied rule ----


def derive_timing(receipt: dict) -> str:
    return classify_commitment(receipt.get("availability_check_1"), receipt.get("availability_check_2"),
                               confirmed=receipt.get("remote_confirmed") is True, push_time=receipt.get("server_push_time"))[0]


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
                if entry["state"] != expected:
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
            ours = sorted(contests[contest][-1], key=lambda d: d["draw_index"])[0]["numbers"]  # the latest official version
            if (numbers if game in POSITIONED else sorted(numbers)) != list(ours):
                report.fail(f"history {game} {contest}: differs from the official API")
            else:
                report.ok("history vs official API")


AUTHORIZED_GO_ACTORS = ("Soturine",)


def check_go(root: Path, ledgers: dict, report: Report, online: bool) -> None:
    """The league may exist only after a valid GO and its ARMED record (drawlab.league.go), and only for armed games."""
    league_files = [name for name in ledgers if name.split("/")[-1] in ("frozen.jsonl", "adaptive.jsonl")]
    go_path, arm_path = root / "go" / "GO-RECEIPT.json", root / "go" / "ARMED.json"
    if not go_path.exists():
        if league_files:
            report.fail("league ledgers exist without a GO receipt")
        else:
            report.ok("no league before a GO")
        return
    receipt = json.loads(go_path.read_text(encoding="utf-8"))
    go_commits = git(root, "log", "--format=%H", "--no-renames", "--", "go/GO-RECEIPT.json").split()
    problems = []
    if len(go_commits) != 1:
        problems.append(f"GO receipt written by {len(go_commits)} commits")
    if receipt.get("schema") != "m7-go-receipt-v1" or receipt.get("decision") != "GO":
        problems.append("GO receipt schema/decision")
    for game, target in (receipt.get("first_targets") or {}).items():
        seen = (receipt.get("observations_before_publication") or {}).get(game) or {}
        if seen.get("latest") is None or target != int(seen["latest"]) + 1 or seen.get("target_state") != "NOT_PUBLISHED":
            problems.append(f"{game}: first target is not the unpublished next contest")
    copy = root / "go" / f"{receipt.get('qualification_id')}.json"
    if copy.exists():
        raw = copy.read_bytes()
        matrix = json.loads(raw)
        if sha256(raw).hexdigest() != receipt.get("qualification_sha256"):
            problems.append("the published qualification matrix is not the one hashed in the GO receipt")
        if matrix.get("decision") != "GO" or matrix.get("candidate_revision") != receipt.get("candidate_revision"):
            problems.append("the published qualification matrix is not a GO for the receipt's candidate")
    else:
        problems.append("no public copy of the qualification matrix")
    armed: dict = {}
    arm_commit = None
    if arm_path.exists():
        arm = json.loads(arm_path.read_text(encoding="utf-8"))
        arm_commits = git(root, "log", "--format=%H", "--no-renames", "--", "go/ARMED.json").split()
        if len(arm_commits) != 1 or arm.get("go_commit") != (go_commits or [None])[-1] or arm.get("go_receipt_sha256") != digest(receipt):
            problems.append("ARMED record does not bind the GO commit and receipt")
        else:
            arm_commit = arm_commits[0]
            armed = {g: v["first_target"] for g, v in (arm.get("games") or {}).items()
                     if v.get("verdict") == "UNPUBLISHED" and v.get("target_state") == "NOT_PUBLISHED" and v.get("first_target") == receipt["first_targets"].get(g)}
    if online and go_commits:
        try:
            request = urllib.request.Request("https://api.github.com/repos/Soturine/drawlab-evidence/activity?per_page=100",
                                             headers={"Accept": "application/vnd.github+json", "User-Agent": "drawlab-evidence-verifier"})
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                activity = {item.get("after"): item for item in json.loads(response.read())}
            for commit in [go_commits[0]] + ([arm_commit] if arm_commit else []):
                actor = ((activity.get(commit) or {}).get("actor") or {}).get("login")
                if actor not in AUTHORIZED_GO_ACTORS:
                    problems.append(f"{commit[:12]} was pushed by {actor!r} (not authorised) or is absent from GitHub's activity log")
        except OSError as exc:
            problems.append(f"GitHub activity API unavailable: {exc}")
    for problem in problems:
        report.fail(f"GO: {problem}")
    if not problems:
        report.ok("GO receipt and ARMED record")
    after_arm = set(git(root, "rev-list", "--ancestry-path", f"{arm_commit}..HEAD").split()) | {arm_commit} if arm_commit else set()
    for name in league_files:
        for entry in ledgers[name]:
            identity = entry["identity"]
            if entry["state"] not in ("COMMITTED", "CATCH_UP", "UNCERTAIN"):
                continue
            first = armed.get(identity["game"])
            if first is None or int(identity["contest"]) < int(first):
                report.fail(f"{name} seq {entry['seq']}: league commitment for a game/contest that is not armed")
                continue
            commit = ((entry.get("data") or {}).get("receipt") or {}).get("remote_commit")
            if not arm_commit or not commit or commit not in after_arm:
                report.fail(f"{name} seq {entry['seq']}: league commitment not published after the ARMED record")
            else:
                report.ok("league commitments after ARMED")


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
    check_go(root, ledgers, report, args.online)
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
