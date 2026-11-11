"""Command line entry point: run the server, store credentials, or do a quick
end-to-end check against the real mail server."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import traceback

from .config import KEYRING_SERVICE, ConfigError, Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uth-mail-mcp", description="MCP server for UTH email")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    sub.add_parser("check", help="log in and try each operation once; sends nothing")
    sub.add_parser("login", help="store username and password in the system keyring")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.command == "login":
        return store_credentials()
    if args.command == "check":
        return check()
    from .server import run
    run()
    return 0


def store_credentials() -> int:
    import keyring

    username = input("UTH username (without @uth.gr): ").strip()
    if username.lower().endswith("@uth.gr"):
        # the servers want the bare username; a wrong login costs a few minutes' lockout
        username = username.rsplit("@", 1)[0]
        print(f"using {username}")
    password = getpass.getpass("password: ")
    if not username or not password:
        print("nothing stored", file=sys.stderr)
        return 1
    name = input("your name for the From line (optional, Enter to skip): ").strip()
    keyring.set_password(KEYRING_SERVICE, "username", username)
    keyring.set_password(KEYRING_SERVICE, username, password)
    if name:
        keyring.set_password(KEYRING_SERVICE, "display_name", name)
    print(f"stored credentials for {username} in the system keyring")
    return 0


def check() -> int:
    """Run each read operation once and print what came back, and log in to
    SMTP without sending anything."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    from .client import MailClient

    client = MailClient(settings)
    failures = 0

    def step(label, fn):
        nonlocal failures
        try:
            result = fn()
        except Exception as exc:
            failures += 1
            print(f"FAIL  {label}: {type(exc).__name__}: {exc}")
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                traceback.print_exc()
            return None
        if isinstance(result, list):
            sample = result[0] if result else None
            summary = f"{len(result)} items"
            if sample:
                summary += ", first: " + str(sample.get("subject") or sample.get("name") or sample)[:70]
        elif isinstance(result, dict):
            summary = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(result.items())[:5])
        else:
            summary = str(result)[:80]
        print(f"ok    {label}: {summary}")
        return result

    print(f"IMAP: {settings.imap_host}:{settings.imap_port}  SMTP: {settings.smtp_host}:{settings.smtp_port}  "
          f"user: {settings.username}  from: {settings.sender}")
    if step("login", lambda: client.login() or "logged in") is None:
        print("stopping here: repeated failed logins can get the account blocked for a while")
        return 1
    step("whoami", client.whoami)
    step("folders", client.folders)
    latest = step("latest in inbox", lambda: client.search("inbox", limit=5)) or []
    if latest:
        uid = latest[0]["uid"]
        message = step(f"read message {uid}", lambda: client.message("inbox", uid))
        if message:
            print(f"      {len(message['body'])} characters of text ({message['body_format']}), "
                  f"{len(message['attachments'])} attachment(s)")
    step("search with Greek text", lambda: client.search("inbox", subject="μάθημα", limit=3))
    step("unread in inbox", lambda: client.search("inbox", unread_only=True, limit=5))
    step("smtp login", client.smtp_check)
    client.close()
    print("all good" if not failures else f"{failures} step(s) failed; run with -v for details")
    return 1 if failures else 0
