"""Store the Webull sandbox app pair in the installation's Windows vault."""

from dataclasses import dataclass, field
from getpass import GetPassWarning, getpass
import json
import os
import sys
import warnings


APP_SLOT = "webull_sandbox_app"
ACCESS_TOKEN_SLOT = "webull_sandbox_access_token"


class CredentialError(RuntimeError):
    """Report a fixed reason without revealing vault errors or credentials."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class WebullCredentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    access_token: str | None = field(default=None, repr=False)


def _service() -> str:
    # WinVault can move an existing account on a shared target when writing.
    # Use the app namespace with a separate target to keep Settings untouched.
    namespace = os.environ.get("BTA_KEYRING_SERVICE") or "BetterTradingAgents"
    return f"{namespace}.WebullSandbox"


def _vault():
    if sys.platform != "win32":
        raise CredentialError("vault_unavailable", "Webull sandbox credentials use Windows Credential Manager.")
    try:
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    except Exception:  # noqa: BLE001 - provider errors may contain private data
        raise CredentialError("vault_unavailable", "Windows Credential Manager is unavailable.") from None


def _read(vault, service: str, slot: str) -> str | None:
    try:
        return vault.get_password(service, slot)
    except Exception:  # noqa: BLE001 - provider errors may contain private data
        raise CredentialError("vault_read_failed", "Windows Credential Manager could not read Webull credentials.") from None


def _usable(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 4096 and all("!" <= character <= "~" for character in value)


def load_credentials() -> WebullCredentials | None:
    """Read only sandbox-qualified entries and keep missing credentials unset."""
    vault, service = _vault(), _service()
    stored = _read(vault, service, APP_SLOT)
    if stored is None:
        return None
    try:
        pair = json.loads(stored)
        key, secret = pair["app_key"], pair["app_secret"]
        if not (_usable(key) and _usable(secret)):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise CredentialError("credentials_invalid", "The saved Webull sandbox app credential pair is incomplete or invalid.") from None
    token = _read(vault, f"{service}.AccessToken", ACCESS_TOKEN_SLOT)
    if token and not _usable(token):
        raise CredentialError("token_invalid", "The saved Webull sandbox access token is invalid.")
    return WebullCredentials(key, secret, token or None)


def store_credentials() -> None:
    """Save the existing app's two values from a non-echoing terminal prompt."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise CredentialError("terminal_required", "Run Webull credential setup in an interactive terminal.")
    vault, service = _vault(), _service()
    if _read(vault, service, APP_SLOT) is not None:
        raise CredentialError("credentials_already_stored", "A Webull sandbox app entry is already stored.")
    try:
        with warnings.catch_warnings():
            # getpass can otherwise fall back to echoing on some terminals.
            warnings.simplefilter("error", GetPassWarning)
            key = getpass("Webull sandbox App Key: ").strip()
            secret = getpass("Webull sandbox App Secret: ").strip()
    except (EOFError, OSError, GetPassWarning):
        raise CredentialError("terminal_required", "This terminal could not read the Webull credentials without echoing.") from None
    if not (_usable(key) and _usable(secret)):
        raise CredentialError("credentials_incomplete", "Enter both the existing sandbox App Key and App Secret.")
    try:
        # A single native vault entry writes the pair together.
        vault.set_password(service, APP_SLOT, json.dumps({"app_key": key, "app_secret": secret}))
    except Exception:  # noqa: BLE001 - provider errors may contain private data
        raise CredentialError("vault_write_failed", "Windows Credential Manager could not save the Webull credentials.") from None
