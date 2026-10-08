# Security

Do not include API keys, wallet material, credentials, deployment state, customer
data or private accounting in issues, pull requests, fixtures or screenshots.
Tests must use generated keys and unmistakably synthetic data.

Security issues should be reported privately to the project owner, not through
a public issue containing exploit details or secrets. A verified private reporting
channel must be configured before public release; no unverified address is listed.

This preparation has no publishing or deployment workflow. CI has read-only
repository permissions and uses no production secrets. Provider/cloud usage,
chain signing and production activation require separate explicit authorization.

Review source and package contents before publication. Ignore rules alone are
not a security boundary. Rotate any credential if it is ever exposed.
