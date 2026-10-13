# drawlab-evidence

Public, append-only evidence of the **DrawLab M7 Prospective Evidence League**: predictions for official CAIXA lottery
contests (Brazil) committed here **before** the official result exists, the official results, the scores, and every
receipt needed to check them. The DrawLab code itself is private; everything needed to verify integrity, timing classes
and scores is here and checkable with [`verifier/verify_m7.py`](verifier/verify_m7.py) (Python standard library only).

> **Claim boundary.** This repository records a scientific test, not a betting system. Nothing here claims that lottery
> results are predictable. The pre-registered expectation is *no excess over the fair draw*; the frozen protocol states
> that 100 contests can detect only large effects, and that a null result does not prove the draws are fair.

## Governing files

* [`protocol/M7-PROTOCOL-v1.json`](protocol/M7-PROTOCOL-v1.json) — leagues, cohorts, horizon, metrics, decision rule
  (canonical SHA-256 `f7d79c5fd130766b82b08f2798adacf8dc36933dabda72acb2a32b066d6627a4`)
* [`protocol/M7-HYPOTHESES-v1.json`](protocol/M7-HYPOTHESES-v1.json) — pre-registered hypotheses (exploratory M6
  anomalies, **not findings**; SHA-256 `a2b169669ccaf251e5fed7036a4a2186baeef066a79980e0928e53d608bc5e0e`)
* `go/GO-RECEIPT.json` then `go/ARMED.json` — appear only if the league is started (two phases, see CONTRACT); nothing
  before them is M7 evidence, and a game is in the league only if ARMED lists it

The canonical SHA-256 is computed over the parsed JSON with sorted keys and no whitespace
(`json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`).

## Layout (see [CONTRACT.md](CONTRACT.md))

| Path | Content | Rule |
|---|---|---|
| `commitments/<league>/<game>/<contest>/<bot>.json` | one canonical prediction (full ticket, frontier, producer, first availability check) | written once, never modified |
| `ledger/<league>.jsonl` | hash-chained events: ELIGIBLE → DRAFT → COMMITTED / CATCH_UP / UNCERTAIN / CONFLICT / MISSED → REVEALED → SCORED, RETRACTED_WITH_RECEIPT | append-only |
| `history/<game>.jsonl`, `history/<game>.amendments.jsonl` | official results (from the CAIXA API) and provider corrections | append-only |
| `adaptive/<bot>.jsonl` | ADAPTIVE league policy states, each bound to its predecessor and to a revealed contest | append-only |
| `ops/<date>.jsonl` | watcher operations log (no secrets) | append-only |
| `ops/health.jsonl` | one line per watcher run: process, official source, publication, eligibility, overall health | append-only |
| `ops/publisher-smoke/<run>.json` | the publisher's own qualification writes (token expiry date, never the token) | written once |
| `commitments/pre_go_rehearsal/…`, `ledger/pre_go_rehearsal.jsonl` | pipeline rehearsals **before** the GO; never M7 evidence | append-only |

## Is it running? (operational health)

The last line of [`ops/health.jsonl`](ops/health.jsonl) is the newest watcher run: `overall` is HEALTHY only when the
run completed, the official source answered, every write was read back from this repository and nothing was MISSED or
UNCERTAIN; otherwise DEGRADED, BLOCKED (no valid league start) or FAILED. A gap between lines longer than an hour means
runs were missed: GitHub's scheduler gives no guarantee, and a missed run shows up as MISSED rows, never as a
back-dated prediction. DrawLab's monitor alerts on stale runs, provider outages, publication failures, token expiry,
unprotected branch and verifier failures; you can check the same file yourself.

## Verify

```text
git clone https://github.com/Soturine/drawlab-evidence && cd drawlab-evidence
python verifier/verify_m7.py                       # integrity, immutability, append-only, timing classes, scores
python verifier/verify_m7.py --online              # also compares recent history with the official CAIXA API
python verifier/verify_m7.py --expect-ancestor <a commit SHA you saved earlier>
```

## Limitations (read these)

* **A repository administrator can rewrite git history.** Branch protection forbids force pushes and deletions, but an
  admin can change protection. Keep your own clone or note commit SHAs and check them later with `--expect-ancestor`.
  Receipts also record GitHub's **server-side** push time (repository activity API) and the official provider's `Date`
  headers; git author/committer dates are written by the client and are **not** evidence of time.
* A row is `PROSPECTIVE_LIVE` (ledger state COMMITTED) only if the provider showed the target unpublished at a check
  before the publication and again at a check made after the remote confirmed the bytes, **and** GitHub's server push
  time lies between the provider's two `Date` headers (±2 s); any doubt is UNCERTAIN, any publication CATCH_UP.
* Three clocks appear in the receipts and only two are evidence: the provider's `Date` header (CAIXA's server) and
  GitHub's push time (GitHub's server). `written_at_client`, git author/committer dates and `ops` timestamps are the
  watcher machine's clock — informational only. Both evidential clocks are third parties DrawLab does not control, but
  they are not an independent timestamping authority; an external anchor (e.g. RFC 3161 or OpenTimestamps) is a
  documented, versioned option for a later protocol, not part of M7-v1.
* The verifier cannot check, without DrawLab's private code, that a ticket is the output of the frozen generator on the
  history before its target; DrawLab checks that against behavioural fingerprints frozen in the protocol.
