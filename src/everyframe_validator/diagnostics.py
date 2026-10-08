"""Allowlisted operator diagnostics; never expose raw provider/SDK exceptions."""

import json
import ssl
import sqlite3
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from bittensor.result import ChainError, PolicyError


ERRORS = {
    "state_directory_must_be_owned_and_mode_700": (
        "unsafe_state_permissions",
        "Use an operator-owned state directory with mode 0700.",
    ),
    "private_file_permissions": (
        "unsafe_state_permissions",
        "Private profile files must be operator-owned and mode 0600.",
    ),
    "state_database_missing_or_unsafe": (
        "unsafe_or_missing_journal",
        "Restore the complete state backup and check database permissions. Do not create an empty journal.",
    ),
    "state_directory_symlink": (
        "unsafe_state_path",
        "Use the original state directory, not a symlink.",
    ),
    "profile_identity_changed": (
        "profile_identity_changed",
        "Restore the original profile and its matching journals. Do not edit identity or trust pins.",
    ),
    "journal_validator_mismatch": (
        "journal_validator_mismatch",
        "Use the state directory belonging to this validator hotkey.",
    ),
    "state_already_initialized": (
        "already_initialized",
        "Use status or doctor with the existing state directory; do not reinitialize it.",
    ),
    "invalid_public_hotkey": (
        "invalid_public_hotkey",
        "Provide a public SS58 hotkey address, never a private key or seed.",
    ),
    "wallet_not_configured": (
        "wallet_not_configured",
        "Configure the intended local wallet before authorizing signing.",
    ),
    "--wallet is required for explicit submission": (
        "wallet_not_configured",
        "Configure the intended local wallet before submitting.",
    ),
    "wallet does not match intended validator": (
        "wallet_identity_mismatch",
        "Select the wallet containing the configured validator hotkey.",
    ),
    "password file must be owner-only": (
        "unsafe_password_file",
        "Protect the wallet password file with mode 0600.",
    ),
    "validator has no permit": (
        "validator_permit_missing",
        "Check hotkey registration and validator permit on the configured subnet.",
    ),
    "invalid or expired mainnet authorization": (
        "authorization_invalid_or_expired",
        "Review and renew local signing consent with authorize. Do not delete the journal.",
    ),
    "mainnet authorization submission budget exhausted": (
        "authorization_exhausted",
        "The authorized submission count is exhausted. Review it before renewing consent.",
    ),
    "local signing authorization file required": (
        "authorization_missing",
        "Run authorize only if you intend to permit real chain submissions.",
    ),
    "wrong_chain": (
        "wrong_chain",
        "The RPC returned an unexpected genesis hash. Stop and verify the configured endpoint.",
    ),
    "unexpected chain genesis": (
        "wrong_chain",
        "The RPC returned an unexpected genesis hash. Stop and verify the configured endpoint.",
    ),
    "unexpected_chain_genesis": (
        "wrong_chain",
        "The RPC returned an unexpected genesis hash. Stop and verify the configured endpoint.",
    ),
    "unsupported_chain_policy": (
        "unsupported_chain_policy",
        "Check the subnet tempo and required scoring version before updating this validator.",
    ),
    "immutable_file_conflict": (
        "conflicting_accounting",
        "Conflicting signed accounting was received. Preserve the state and investigate the publisher.",
    ),
    "feed_http_error": (
        "feed_http_error",
        "The accounting endpoint returned an unsupported response. Check publisher availability.",
    ),
    "document_too_large": (
        "feed_too_large",
        "The accounting document exceeds the size limit. Ask the publisher to investigate.",
    ),
    "feed_requires_https_directory_url": (
        "invalid_feed_url",
        "Use a public HTTPS directory URL ending with a slash.",
    ),
    "feed_must_be_public": (
        "invalid_feed_destination",
        "The feed must resolve only to public IP addresses.",
    ),
    "submission_policy_rejected": (
        "submission_policy_rejected",
        "The SDK rejected the transaction policy. Check fee limits and chain requirements before retrying.",
    ),
    "submission not confirmed": (
        "submission_unconfirmed",
        "Run reconcile to check finalized evidence. Do not repeat the transaction or clear the journal.",
    ),
    "outdated scoring version; even a dry-run must use a compatible version": (
        "outdated_scoring_version",
        "Update the validator's scoring version to the subnet's required version before running.",
    ),
    "outdated_version_key": (
        "outdated_scoring_version",
        "Use a scoring version at least as recent as the pinned network profile.",
    ),
    "newest closed epoch is not finalized": (
        "epoch_changed_or_missing",
        "The required closed epoch is unavailable. Wait for publication and rerun the read-only checks.",
    ),
    "epoch changed before signing; refuse stale submission": (
        "epoch_changed_before_signing",
        "No transaction was sent. The next iteration will use the newest closed epoch.",
    ),
    "invalid_recovery_evidence": (
        "invalid_recovery_evidence",
        "Finalized transaction evidence was inconsistent. Keep the journal and investigate the archive RPC.",
    ),
    "invalid_recovery_cursor": (
        "invalid_recovery_cursor",
        "Recovery progress does not match finalized chain state. Preserve the journal and check the archive RPC.",
    ),
    "recovery_block_unavailable": (
        "recovery_block_unavailable",
        "The archive RPC could not supply the recovery block. No retry is authorized.",
    ),
}


def diagnostic(exc, stage=None):
    # Exact matches only: an exception containing credentials must never be echoed.
    key = exc.args[0] if exc.args and isinstance(exc.args[0], str) else None
    if key in ERRORS:
        code, message = ERRORS[key]
    elif isinstance(exc, InvalidSignature):
        code, message = (
            "invalid_feed_signature",
            "Accounting signature verification failed. Verify the pinned signer; do not bypass verification.",
        )
    elif isinstance(exc, BlockingIOError):
        code, message = (
            "operation_in_progress",
            "Another validator operation owns the state lock. Status and doctor remain available; wait before starting a mutation.",
        )
    elif isinstance(exc, FileNotFoundError):
        code, message = (
            "required_file_missing",
            "A required local file is missing. Check the selected state directory and wallet; restore existing state rather than clearing it.",
        )
        if stage == "authorization" or (
            exc.filename and Path(exc.filename).name == "authorization.json"
        ):
            code, message = (
                "authorization_missing",
                "No local signing consent exists. Dry-run is available; use authorize only when ready to permit submissions.",
            )
    elif isinstance(exc, PermissionError):
        code, message = (
            "permission_denied",
            "Check the operator's access to the state directory and wallet files.",
        )
    elif isinstance(exc, (TimeoutError, ConnectionError)):
        code, message = (
            "network_timeout_or_disconnect",
            "The network request timed out or disconnected. Check RPC/feed connectivity; reconcile uncertain submissions before retrying.",
        )
    elif isinstance(exc, ssl.SSLError):
        code, message = (
            "tls_verification_failed",
            "Check the endpoint certificate and system clock. Do not disable TLS verification.",
        )
    elif isinstance(exc, PolicyError):
        code, message = ERRORS["submission_policy_rejected"]
    elif isinstance(exc, ChainError):
        code, message = (
            "chain_request_failed",
            "The chain request failed. Check RPC connectivity and run doctor. Reconcile any uncertain submission before retrying.",
        )
    elif isinstance(exc, json.JSONDecodeError):
        code, message = (
            "invalid_json",
            "The profile or accounting document is not valid JSON. Inspect the relevant source without sharing secrets.",
        )
    elif isinstance(exc, sqlite3.Error):
        code, message = (
            "state_database_error",
            "The journal could not be read or updated. Check disk space and the complete state backup; do not delete databases.",
        )
    else:
        code, message = (
            "validation_failed",
            "Validation could not complete. Run doctor to identify the failing stage. Preserve journals and do not retry uncertain transactions.",
        )
    result = {"state": "failed", "code": code, "message": message}
    if stage:
        result["stage"] = stage
    return result
