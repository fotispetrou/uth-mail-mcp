"""Settings come from environment variables, with the system keyring as a
fallback for the password so it does not have to live in a config file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from email.headerregistry import Address
from pathlib import Path

# Mailboxes live on per-department hosts, mail.<department>.uth.gr, even for
# name@uth.gr addresses; this is the one for Ψηφιακά Συστήματα. The central
# mail.uth.gr accepts the login but has no mailbox for them. Sending goes
# through the central smtp.uth.gr for everyone.
DEFAULT_IMAP_HOST = "mail.ds.uth.gr"
DEFAULT_SMTP_HOST = "smtp.uth.gr"
DEFAULT_DOMAIN = "uth.gr"
KEYRING_SERVICE = "uth-mail-mcp"
SECURITY_MODES = ("ssl", "starttls", "none")
# plaintext is only for a test server on this machine; never send a password
# unencrypted over the network
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class ConfigError(Exception):
    pass


def keyring_get(key: str) -> str | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, key)
    except Exception:
        return None


def _security(name: str, default: str, host: str) -> str:
    mode = os.environ.get(name, default).strip().lower()
    if mode not in SECURITY_MODES:
        raise ConfigError(f"{name} must be one of {', '.join(SECURITY_MODES)}")
    if mode == "none" and host not in LOCAL_HOSTS:
        raise ConfigError(f"{name}=none is only allowed for a server on this machine")
    return mode


@dataclass
class Settings:
    username: str
    password: str
    address: str
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    download_dir: Path
    timeout: float = 30.0
    display_name: str = ""

    @property
    def sender(self) -> str:
        """The From of what we write: 'Name <address>' when a name is set."""
        if not self.display_name:
            return self.address
        return str(Address(self.display_name, addr_spec=self.address))

    @classmethod
    def from_env(cls) -> "Settings":
        username = os.environ.get("UTHMAIL_USERNAME") or keyring_get("username")
        password = os.environ.get("UTHMAIL_PASSWORD")
        if not password and username:
            password = keyring_get(username)
        if not username or not password:
            raise ConfigError(
                "No mail credentials. Set UTHMAIL_USERNAME and UTHMAIL_PASSWORD, "
                "or run `uth-mail-mcp login` to store them in the system keyring."
            )
        address = os.environ.get("UTHMAIL_ADDRESS") or (
            username if "@" in username else f"{username}@{DEFAULT_DOMAIN}"
        )
        imap_host = os.environ.get("UTHMAIL_IMAP_HOST", DEFAULT_IMAP_HOST)
        imap_security = _security("UTHMAIL_IMAP_SECURITY", "ssl", imap_host)
        imap_port = int(os.environ.get("UTHMAIL_IMAP_PORT") or (993 if imap_security == "ssl" else 143))
        smtp_host = os.environ.get("UTHMAIL_SMTP_HOST", DEFAULT_SMTP_HOST)
        smtp_security = _security("UTHMAIL_SMTP_SECURITY", "starttls", smtp_host)
        smtp_port = int(os.environ.get("UTHMAIL_SMTP_PORT")
                        or {"ssl": 465, "starttls": 587, "none": 25}[smtp_security])
        download_dir = Path(
            os.environ.get("UTHMAIL_DOWNLOAD_DIR", "~/Downloads/uth-mail")
        ).expanduser()
        timeout = float(os.environ.get("UTHMAIL_TIMEOUT", "30"))
        display_name = os.environ.get("UTHMAIL_NAME") or keyring_get("display_name") or ""
        return cls(username, password, address, imap_host, imap_port, imap_security,
                   smtp_host, smtp_port, smtp_security, download_dir, timeout, display_name)
