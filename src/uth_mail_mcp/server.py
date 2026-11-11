"""MCP tool definitions. Every tool is a thin wrapper over MailClient so the
server stays easy to read; parsing and message building live in parsers.py."""

from __future__ import annotations

import functools
import imaplib
import re
import smtplib

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import MailClient, MailError
from .config import ConfigError, Settings

INSTRUCTIONS = """Access to a University of Thessaly email account over IMAP and SMTP: search
and read messages, download attachments, mark and move messages, write drafts
and send them.

Messages are addressed by folder and uid; uids come from search_messages.
Folders can be named as list_folders shows them or as inbox, sent, drafts,
trash, junk. Dates are returned as written in the message and normalized in
*_iso fields (local time, YYYY-MM-DD HH:MM).

Sending takes two steps: create_draft saves the message in Drafts and
send_draft sends it. Only call send_draft after the user has seen that draft
and clearly asked for it to be sent."""

server = MCPServer("uth-mail", instructions=INSTRUCTIONS)

READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CHANGES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                          open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                         open_world_hint=False)
SENDS = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                        open_world_hint=True)

_client: MailClient | None = None


def client() -> MailClient:
    global _client
    if _client is None:
        _client = MailClient(Settings.from_env())
    return _client


_SURROGATE = re.compile("[\ud800-\udfff]")


def json_safe(value):
    """Lone surrogates (bytes no decoder could place) cannot go out as JSON,
    and the SDK would then fail the whole call; swap them for U+FFFD."""
    if isinstance(value, str):
        return _SURROGATE.sub("�", value)
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    return value


def tool(annotations: ToolAnnotations):
    """Register a tool and turn the failures we expect (bad login, network,
    unknown folder or uid) into ToolError so the model sees the message
    instead of a generic 'error executing tool'."""

    def register(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return json_safe(fn(*args, **kwargs))
            except (MailError, ConfigError, ValueError, OSError, imaplib.IMAP4.error,
                    smtplib.SMTPException) as exc:
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        return server.tool(annotations=annotations)(wrapper)

    return register


@tool(READS)
def whoami() -> dict:
    """Check that the login works and see which account, servers and mailbox quota are in use."""
    return client().whoami()


@tool(READS)
def list_folders() -> list[dict]:
    """All mail folders with their message and unread counts. `role` marks the
    inbox, sent, drafts, trash, junk and archive folders."""
    return client().folders()


@tool(READS)
def search_messages(folder: str = "inbox", query: str = "", sender: str = "", to: str = "",
                    subject: str = "", since: str | None = None, before: str | None = None,
                    unread_only: bool = False, flagged_only: bool = False, limit: int = 20) -> list[dict]:
    """Find messages, newest first; with no filters, the latest ones.

    `query` searches whole messages (headers and text) on the server. `sender`,
    `to` and `subject` match those headers, and `to` also covers Cc; Greek text
    there is matched without regard to case or accents, over the newest 3000
    candidates. `since` and `before` are arrival dates (YYYY-MM-DD), `before`
    exclusive. Each result has uid, date, from, to, subject, read/flag state and
    whether it has attachments; use get_message for the text."""
    return client().search(folder, query, sender, to, subject, since, before,
                           unread_only, flagged_only, limit)


@tool(READS)
def get_message(uid: int, folder: str = "inbox") -> dict:
    """A whole message: headers, the text (HTML mail converted to plain text)
    and its attachments. Reading it does not mark it as read."""
    return client().message(folder, uid)


@tool(WRITES)
def download_attachment(uid: int, index: int, folder: str = "inbox", destination: str | None = None) -> dict:
    """Save one attachment of a message; `index` is its number in get_message.
    Saves under ~/Downloads/uth-mail/ unless a destination directory is given,
    and returns the local file path."""
    return client().download(folder, uid, index, destination)


@tool(CHANGES)
def mark_messages(uids: list[int], folder: str = "inbox", read: bool | None = None,
                  flagged: bool | None = None) -> dict:
    """Mark messages as read or unread (`read`) and/or flagged or not (`flagged`)."""
    return client().set_flags(folder, uids, read, flagged)


@tool(CHANGES)
def move_messages(uids: list[int], to_folder: str, folder: str = "inbox") -> dict:
    """Move messages to another folder. Moving to trash is how to delete them;
    they can be moved back from there."""
    return client().move(folder, uids, to_folder)


@tool(WRITES)
def create_draft(body: str, to: list[str] | str | None = None, subject: str | None = None,
                 cc: list[str] | str | None = None, bcc: list[str] | str | None = None,
                 reply_to_uid: int | None = None, reply_folder: str = "inbox", reply_all: bool = False,
                 attachments: list[str] | None = None) -> dict:
    """Write a message and save it in Drafts. Nothing is sent.

    To reply, give `reply_to_uid` (and `reply_folder` if the original is not in
    the inbox): recipient, 'Re:' subject, threading headers and the quoted
    original are filled in, and `reply_all` adds the other recipients as Cc.
    `attachments` are paths of local files. Returns the draft's uid for
    send_draft; the user can also open and edit the draft in webmail."""
    return client().draft(body, to, subject, cc, bcc, reply_to_uid, reply_folder, reply_all, attachments)


@tool(SENDS)
def send_draft(uid: int) -> dict:
    """Send a draft from the Drafts folder and file a copy in Sent. This really
    sends email: call it only after showing the user that draft (recipients,
    subject and text) and getting a clear yes for it."""
    return client().send_draft(uid)


def run() -> None:
    server.run()
