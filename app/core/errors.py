"""MEGA API error codes, ported from MegaAPIException.java / MegaErrorMessages.java."""
from __future__ import annotations

# Error code -> (short name, human message). Meaning is sometimes context
# dependent (account vs. public link) per the Java MegaErrorMessages table;
# we keep the account-oriented message as the default.
ERROR_MESSAGES: dict[int, str] = {
    -1: "Internal server error",
    -2: "Invalid arguments",
    -3: "Request failed, retry (EAGAIN)",
    -4: "Rate limit exceeded, retry later",
    -5: "Failed permanently",
    -6: "Too many concurrent connections",
    -7: "Out of range",
    -8: "Expired",
    -9: "Object (file/folder) not found",
    -10: "Circular linkage detected",
    -11: "Access denied",
    -12: "Object already exists",
    -13: "Incomplete request",
    -14: "Decryption error (wrong key/password)",
    -15: "Session/user invalid, please log in again",
    -16: "Account blocked",
    -17: "Storage/bandwidth quota exceeded",
    -18: "Resource temporarily unavailable",
    -19: "Too many identical requests",
    -24: "Transfer over quota",
    -25: "Multi-factor authentication required",
}

# Codes that mean "retry later" rather than "abort".
RETRYABLE_CODES = {-3, -4}

QUOTA_EXCEEDED_CODE = -17
TWO_FACTOR_REQUIRED_CODE = -25


class MegaAPIException(Exception):
    """A negative MEGA API result code, raised by the API client.

    Carries the numeric `code` and an optional `context` string; the
    convenience properties classify the code so callers can branch on it
    (retry, prompt for 2FA, reroute on quota) without hardcoding numbers.
    """

    def __init__(self, code: int, context: str = ""):
        self.code = code
        self.context = context
        message = ERROR_MESSAGES.get(code, f"Unknown MEGA error code {code}")
        super().__init__(f"{message} (code {code}){': ' + context if context else ''}")

    @property
    def is_retryable(self) -> bool:
        """True for transient codes (EAGAIN / rate-limit) worth retrying."""
        return self.code in RETRYABLE_CODES

    @property
    def is_quota_exceeded(self) -> bool:
        """True for -17, the storage/bandwidth quota (509) code that triggers
        SmartProxy rerouting."""
        return self.code == QUOTA_EXCEEDED_CODE

    @property
    def is_two_factor_required(self) -> bool:
        """True for -25, meaning login needs a 2FA code."""
        return self.code == TWO_FACTOR_REQUIRED_CODE


class ChunkInvalidError(Exception):
    """Raised when a downloaded/uploaded chunk fails integrity validation."""
