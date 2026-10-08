# Contributing

Use isolated development environments and the local test commands in README.md.
Do not load production .env files, run paid generations, provision cloud resources
or submit chain transactions as part of tests.

Keep changes scoped and add regression tests. Changes to signatures, immutable
contracts, network scope, submission recovery or accounting require compatibility
and security review. Do not silently relax a fail-closed check.

CI performs tests and package checks only. Publishing and production deployment
are separate operator decisions. No Git repository has been initialized in this
local preparation.
