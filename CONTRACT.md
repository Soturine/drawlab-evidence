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

## Timing classes (re-derived by the verifier)

| Ledger state | Condition |
|---|---|
| COMMITTED (`PROSPECTIVE_LIVE`) | check 1 UNPUBLISHED, bytes confirmed on the remote, check 2 (after the confirmation) UNPUBLISHED, GitHub server push time not after check 2's provider `Date` (+5 min tolerance) |
| CATCH_UP | the result was PUBLISHED at a check (scored, never prospective) |
| UNCERTAIN | anything else (failed or ambiguous checks, unconfirmed remote) |
| CONFLICT | the remote already held different bytes at the path |
| MISSED | the result was published before any confirmed commitment (stays in the denominator) |

## Ledger (`ledger/<league>.jsonl`, schema `m7-ledger-v1`)

`{schema, seq, prev_hash, identity, state, data, written_at_client, entry_hash}` with
`entry_hash = SHA-256(canonical JSON of every other field)` and `prev_hash` the previous entry's hash (genesis 64 zeros).
`written_at_client` is informational only.

## Score (`m7-score-v1`)

Primary hits summed over every draw of the contest (Dupla Sena: two draws, one ticket). Prize tiers are computed only
on the components the ticket contains: a PRIMARY_ONLY ticket never wins a tier that needs an absent component. A SCORED
event is computed against the official version revealed just before it; a provider correction appends an amendment, a
new REVEALED event and a new SCORED event.
