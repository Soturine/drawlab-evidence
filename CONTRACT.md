# Public contract (M7-v1, mode PUBLIC_TICKETS)

## Commitment (`commitments/<league>/<game>/<contest>/<bot>.json`, schema `m7-commitment-v1`)

```text
schema, mode = "PUBLIC_TICKETS"
prediction_id = "m7:<protocol sha12>:<league>:<game>:<contest>:<bot>"
identity      = {protocol (full SHA-256), league, game, contest, bot}
membership    = "M7_HORIZON" | "PRE_GO_REHEARSAL"
ticket        = {primary (DrawLab cells), official_numbers (sets: sorted numbers; Super Sete: digit per column),
                 auxiliary (trevos / month only when produced), component_scope}
producer      = frozen generator kind + params + fingerprint, or ADAPTIVE policy + state version + state SHA-256
frontier      = {last_contest, outcome_digest} of the history used (contests strictly before the target)
protocol      = {protocol_id, canonical_sha256, preregistration_sha256}
code_revision = DrawLab's commit SHA (private code; the SHA is public)
availability_check_1 = the provider's answers before the publication (see below)
payload_sha256 = SHA-256 of the canonical JSON of all fields above
```

The file is canonical JSON plus a newline and is written by exactly one commit. A correction is never an edit: a
`RETRACTED_WITH_RECEIPT` ledger event references it and scoring excludes it (the denominator keeps it).

## Availability check

Two endpoints of the official API: `/api/<game>` (latest contest, provider-announced next contest and date) and
`/api/<game>/<target>`. For each: HTTP status, the provider's `Date` header, SHA-256 of the raw body. Target state:
PUBLISHED (200 with the target's numbers), NOT_PUBLISHED (the API's inner 404), UNKNOWN (anything else). Verdict:
PUBLISHED if latest ≥ target or the target is PUBLISHED; UNPUBLISHED only if latest < target **and** NOT_PUBLISHED;
otherwise UNKNOWN.

## Timing classes (rule `m7-timing-v2`, re-derived by the verifier)

| Ledger state | Condition |
|---|---|
| COMMITTED (`PROSPECTIVE_LIVE`) | every piece of proof: check 1 and check 2 both explicitly UNPUBLISHED, the committed bytes served back by the remote, the GitHub server push time present and parseable, both provider `Date` headers present with check 1 ≤ check 2, and `Date(check 1) − 2 s ≤ push time ≤ Date(check 2) + 2 s` |
| CATCH_UP | the result was PUBLISHED at a check (scored, never prospective) |
| UNCERTAIN | anything missing, unparseable, contradictory or out of order (fails closed) |
| CONFLICT | the remote already held different bytes at the path |
| MISSED | the result was published before any confirmed commitment (stays in the denominator) |

The 2 s allowance covers whole-second truncation of both clocks; the first real receipts showed push − Date(check 1) of
3–4 s and Date(check 2) − push of 2–3 s. The verifier carries a verbatim copy of the rule (`classify_commitment`).
The 36 rehearsal receipts published on 2026-10-13 before this rule used `m7-timing-v1` (5 min tolerance, a missing push
time accepted); they are all CATCH_UP, never M7 evidence, and are not re-labelled.

## League start (`go/GO-RECEIPT.json`, `go/ARMED.json`, `go/<qualification id>.json`)

Two phases, each written by exactly one commit pushed by an authorised account (GitHub's activity log is the witness):
the GO receipt (`m7-go-receipt-v1`: protocol and pre-registration hashes, qualification id and matrix hash, candidate
revision, per game the provider observation made just before publication and `first_target = latest + 1`) with a
public copy of the qualification matrix, then ARMED (`m7-go-armed-v1`: bound to the GO commit and receipt hash, a fresh
observation per game). A game whose first target was published between the two is NOT_ARMED and has no horizon. League
ledgers without a GO, or commitments for games/contests not armed, or published before ARMED, fail verification.

## Operations records (`ops/`, not evidence)

`ops/<date>.jsonl` watcher log; `ops/health.jsonl` one line per tick with its operational health (process, official
source, publication, scientific eligibility, overall HEALTHY / DEGRADED / BLOCKED / FAILED); `ops/publisher-smoke/<run>.json`
the publisher's qualification writes (run id, revision, token expiry date — never the token). These are append-only
operational records: they show whether the system is running, never what a prediction was.

## Ledger (`ledger/<league>.jsonl`, schema `m7-ledger-v1`)

`{schema, seq, prev_hash, identity, state, data, written_at_client, entry_hash}` with
`entry_hash = SHA-256(canonical JSON of every other field)` and `prev_hash` the previous entry's hash (genesis 64 zeros).
`written_at_client` is informational only.

## Score (`m7-score-v1`)

Primary hits summed over every draw of the contest (Dupla Sena: two draws, one ticket). Prize tiers are computed only
on the components the ticket contains: a PRIMARY_ONLY ticket never wins a tier that needs an absent component. A SCORED
event is computed against the official version revealed just before it; a provider correction appends an amendment, a
new REVEALED event and a new SCORED event.
