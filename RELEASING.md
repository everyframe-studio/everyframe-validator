# Release checklist — local preparation only

1. Obtain the owner's license choice and configure private security reporting.
2. Review every source file and run a redacted secret scan.
3. Run the README tests, pip check, python -m build, and twine check.
4. Install the wheel into a clean virtual environment outside the source directory;
   run everyframe-validator --help. Inspect both wheel and source archive contents.
5. Select the GitHub organization and update package repository/source URLs.
6. Review the next version before publishing; never overwrite an existing PyPI version.
7. After separate approval, configure PyPI Trusted Publishing with a protected
   release environment and repository-specific workflow. No publishing workflow
   or PyPI credential is included in this preparation.

The external validator still needs a real signed finalized-epoch feed. Source/package availability does not imply that the feed or production validation is active. The publisher utility is for operators; do not distribute signing keys or accounting databases.

These steps do not authorize paid provider calls, mainnet signing or deployment.
