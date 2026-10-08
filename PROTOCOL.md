# Signed finalized epoch feed v1

Each `{epoch}.json` is one JSON object with exactly `payload` and `signature`.
The signature is standard base64 Ed25519 over UTF-8 encoding of
`json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)`.
This format uses Python JSON escaping (`ensure_ascii=True`), not arbitrary JSON
stringification. The pinned public key is base64 DER SubjectPublicKeyInfo.
Duplicate keys, floats, non-finite values, extra wire fields and oversized bodies
are rejected. Maximum document size is 2,000,000 bytes and maximum scored miners 4096.

`payload` has exactly:

- `schema`: `everyframe.validator.epoch.v1`.
- `artifact`: existing version-2 `epoch-value-cap-burn-v2` metadata, economics,
  and aggregate scoring window. No per-job `proofs` or `excluded` lists.
- `bindings`: exactly the scored miner IDs mapped to distinct SS58 hotkeys.
- `sourceHash`: SHA-256 of the original private sealed artifact's canonical JSON.
  This is a commitment, not proof that unpublished private evidence is correct.

The artifact binds network, netuid, genesis, epoch, period, block range and boundary
hashes. Testnet is strictly test/SN566; mainnet strictly finney/SN117. Mainnet also
includes owner-cut evidence, exact derived miner share, recycle/burn disposition,
and `accountingWindow: fixed-tempo-plus-one-v1`. These fixed accounting windows are
not represented as subnet-offset Yuma distribution epochs.

Window fields: `version`, `policy`, `start`, `end`, `finalizedAt`, `scores`.
Scores contain `minerId`, integer `earningsMicrousd`, integer `surchargeMicrousd`,
and integer `jobs`. Price provenance permits only `source`, `observedAt`, `taoUsd`,
and `candleHash`. The publisher uses explicit allowlists at each level.

Accounting is authenticated, not independently reconstructed. Validators recompute
the value-based cap/burn allocation locally with exact rational arithmetic, then
use the pinned Bittensor SDK's normalization. Current chain constraints may force a
defer; the validator never invents work or changes chain settings to fit the vector.

Epoch numbers come from finalized chain head divided by `tempo+1`, minus one.
404 means wait. The complete payload hash (including bindings) is stored immutably;
even another valid signature cannot revise an already accepted epoch locally.
No discovery listing, latest pointer, S3 login, or owner SSH is needed.
