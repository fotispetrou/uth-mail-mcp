"""IMAP and SMTP side: one IMAP connection reused across tool calls, one
method per thing the tools need."""

from __future__ import annotations

import imaplib
import logging
import mimetypes
import re
import smtplib
import ssl
import threading
import time
from collections import OrderedDict
from email.utils import formatdate
from pathlib import Path

from . import parsers
from .config import Settings

log = logging.getLogger(__name__)

# imaplib only sends UID commands it knows about, and older versions do not
# list MOVE
imaplib.Commands.setdefault("MOVE", ("SELECTED",))

# the university blocks an address after repeated failed logins, so after a
# rejected password we stop trying for a while instead of failing on every call
LOGIN_RETRY_DELAY = 5 * 60
# a connection unused for this long gets a NOOP first, so one the server
# dropped in the meantime is replaced instead of failing the call
IDLE_CHECK = 60
# header filters with Greek text are matched locally on this many of the
# newest candidate messages at most
MAX_LOCAL_SCAN = 3000
# the SMTP server refuses anything bigger
MAX_MESSAGE_BYTES = 20_000_000
CACHE_SIZE = 4


class MailError(Exception):
    pass


class LoginError(MailError):
    pass


class MailClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._imap: imaplib.IMAP4 | None = None
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._retry_login_at = 0.0
        self._folders: list[dict] | None = None
        self._prefix = ""
        self._raw_cache: OrderedDict[tuple, tuple[bytes, list[str]]] = OrderedDict()
        self._sent_drafts: set[tuple] = set()

    # -- connection -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._imap is not None:
                try:
                    self._imap.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass
            self._imap = None

    def _drop(self) -> None:
        try:
            if self._imap is not None:
                self._imap.shutdown()
        except OSError:
            pass
        self._imap = None

    def _open(self) -> imaplib.IMAP4:
        s = self.settings
        if time.monotonic() < self._retry_login_at:
            raise LoginError("the mail server rejected the last login; not retrying for a few minutes "
                             "so the account does not get blocked. Check the username and password.")
        ctx = ssl.create_default_context()
        if s.imap_security == "ssl":
            conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port, ssl_context=ctx, timeout=s.timeout)
        else:
            conn = imaplib.IMAP4(s.imap_host, s.imap_port, timeout=s.timeout)
            if s.imap_security == "starttls":
                conn.starttls(ssl_context=ctx)
        try:
            conn.login(s.username, s.password)
        except imaplib.IMAP4.abort:  # connection trouble, not a wrong password
            raise
        except imaplib.IMAP4.error as exc:
            self._retry_login_at = time.monotonic() + LOGIN_RETRY_DELAY
            try:
                conn.shutdown()
            except OSError:
                pass
            raise LoginError(f"{s.imap_host} rejected the login: {exc}") from exc
        # the greeting's capabilities are the pre-login ones
        typ, data = conn.capability()
        if typ == "OK" and data and data[-1]:
            conn.capabilities = tuple(data[-1].decode().upper().split())
        if "NAMESPACE" in conn.capabilities:
            self._prefix = self._personal_prefix(conn)
        log.info("logged in to %s as %s", s.imap_host, s.username)
        return conn

    @staticmethod
    def _personal_prefix(conn: imaplib.IMAP4) -> str:
        try:
            typ, data = conn.namespace()
        except imaplib.IMAP4.error:
            return ""
        m = re.match(rb'\(\("([^"]*)"', data[0] if data and data[0] else b"")
        return m.group(1).decode() if m else ""

    def _conn(self) -> imaplib.IMAP4:
        """The live connection; callers hold the lock."""
        if self._imap is not None and time.monotonic() - self._last_used > IDLE_CHECK:
            try:
                self._imap.noop()
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
                log.info("connection went stale, reconnecting")
                self._drop()
        if self._imap is None:
            self._imap = self._open()
        self._last_used = time.monotonic()
        return self._imap

    def _call(self, fn):
        """Run fn(conn) under the lock. A dropped connection is reported, not
        retried, so nothing that changes the mailbox can happen twice."""
        with self._lock:
            conn = self._conn()
            try:
                return fn(conn)
            except imaplib.IMAP4.abort as exc:
                self._drop()
                raise MailError(f"the connection to the mail server dropped ({exc}); try again") from exc
            except imaplib.IMAP4.error as exc:
                raise MailError(str(exc)) from exc
            except OSError:
                self._drop()
                raise

    def login(self) -> None:
        self._call(lambda conn: None)

    # -- folders ----------------------------------------------------------------

    def _folder_list(self, conn: imaplib.IMAP4, refresh: bool = False) -> list[dict]:
        if self._folders is None or refresh:
            typ, data = conn.list('""', "*")
            if typ != "OK":
                raise MailError(f"LIST failed: {data}")
            folders = [f for f in (parsers.parse_list_item(i) for i in data) if f]
            self._folders = parsers.assign_roles(folders)
        return self._folders

    def _resolve(self, conn: imaplib.IMAP4, folder: str | None) -> dict:
        """A folder by its name, its raw path, or a role (inbox, sent, drafts,
        trash, junk, archive)."""
        wanted = (folder or "INBOX").strip()
        for refresh in (False, True):
            folders = self._folder_list(conn, refresh)
            key = wanted.casefold()
            role = parsers.ROLE_ALIASES.get(key)
            for match in (
                lambda f: f["path"] == wanted or f["name"] == wanted,
                lambda f: f["name"].casefold() == key or f["path"].casefold() == key,
                lambda f: role is not None and f["role"] == role,
                lambda f: f["delimiter"] and f["name"].rsplit(f["delimiter"], 1)[-1].casefold() == key,
            ):
                hits = [f for f in folders if match(f)]
                if hits:
                    return hits[0]
        names = ", ".join(f["name"] for f in folders if f["selectable"])
        raise ValueError(f"no folder {wanted!r}; folders: {names}")

    def _role_folder(self, conn: imaplib.IMAP4, role: str, create_as: str) -> dict:
        """The drafts/sent folder, created if the account has none yet."""
        for f in self._folder_list(conn):
            if f["role"] == role:
                return f
        path = self._prefix + parsers.utf7_encode(create_as)
        typ, data = conn.create(parsers.quote(path))
        if typ != "OK":
            raise MailError(f"could not create the {create_as} folder: {data}")
        conn.subscribe(parsers.quote(path))
        log.info("created folder %s", path)
        for f in self._folder_list(conn, refresh=True):
            if f["path"] == path:
                f["role"] = role
                return f
        raise MailError(f"created {create_as} but the server does not list it")

    def _select(self, conn: imaplib.IMAP4, folder: dict, readonly: bool) -> int:
        typ, data = conn.select(parsers.quote(folder["path"]), readonly=readonly)
        if typ != "OK":
            raise MailError(f"cannot open folder {folder['name']}: {data}")
        _, validity = conn.response("UIDVALIDITY")
        return int(validity[0]) if validity and validity[0] else 0

    def folders(self) -> list[dict]:
        def run(conn):
            out = []
            for f in self._folder_list(conn, refresh=True):
                item = {"name": f["name"], "role": f["role"]}
                if f["selectable"]:
                    typ, data = conn.status(parsers.quote(f["path"]), "(MESSAGES UNSEEN)")
                    counts = parsers.parse_status(data[0]) if typ == "OK" and data else {}
                    item.update(messages=counts.get("messages"), unread=counts.get("unseen"))
                else:
                    item["container_only"] = True
                out.append(item)
            return out
        return self._call(run)

    def whoami(self) -> dict:
        s = self.settings

        def run(conn):
            typ, data = conn.status("INBOX", "(MESSAGES UNSEEN)")
            counts = parsers.parse_status(data[0]) if typ == "OK" and data else {}
            quota = None
            if "QUOTA" in conn.capabilities:
                try:
                    typ, data = conn.getquotaroot("INBOX")
                    quota = parsers.parse_quota(data[-1] if data else None)
                except imaplib.IMAP4.error:
                    pass
            return {"account": s.username, "address": s.address,
                    "imap": f"{s.imap_host}:{s.imap_port}", "smtp": f"{s.smtp_host}:{s.smtp_port}",
                    "inbox_messages": counts.get("messages"), "inbox_unread": counts.get("unseen"),
                    "quota": quota}
        return self._call(run)

    # -- reading ----------------------------------------------------------------

    def _fetch_headers(self, conn: imaplib.IMAP4, uids: list[int]) -> list[dict]:
        typ, data = conn.uid("FETCH", ",".join(map(str, uids)),
                             f"(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({parsers.HEADER_FIELDS})])")
        if typ != "OK":
            raise MailError(f"FETCH failed: {data}")
        return [parsers.summarize(r) for r in parsers.parse_fetch(data)]

    def search(self, folder: str = "inbox", query: str = "", sender: str = "", to: str = "",
               subject: str = "", since: str | None = None, before: str | None = None,
               unread_only: bool = False, flagged_only: bool = False, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        keys, literal, local = parsers.search_criteria(query, sender, to, subject, since, before,
                                                       unread_only, flagged_only)

        def run(conn):
            box = self._resolve(conn, folder)
            self._select(conn, box, readonly=True)
            if literal:
                conn.literal = literal[1].encode("utf-8")
                typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *keys, literal[0])
            else:
                typ, data = conn.uid("SEARCH", *(keys or ["ALL"]))
            if typ != "OK":
                raise MailError(f"SEARCH failed: {data}")
            uids = parsers.parse_search(data)[::-1]  # newest first
            if not local:
                uids = uids[:limit]
            found = []
            step = limit if not local else 200
            for start in range(0, min(len(uids), MAX_LOCAL_SCAN if local else limit), step):
                batch = self._fetch_headers(conn, uids[start:start + step])
                for item in sorted(batch, key=lambda i: i["uid"], reverse=True):
                    if parsers.matches(item, local):
                        item["folder"] = box["name"]
                        found.append(parsers.compact(item))
                if len(found) >= limit:
                    break
            found = found[:limit]
            if found:
                typ, data = conn.uid("FETCH", ",".join(str(i["uid"]) for i in found), "(UID BODYSTRUCTURE)")
                flags = parsers.attachment_flags(data) if typ == "OK" else {}
                for item in found:
                    item["has_attachments"] = flags.get(item["uid"], False)
            return found
        return self._call(run)

    def _raw(self, conn: imaplib.IMAP4, box: dict, uid: int) -> tuple[bytes, list[str]]:
        validity = self._select(conn, box, readonly=True)
        key = (box["path"], validity, uid)
        if key in self._raw_cache:
            self._raw_cache.move_to_end(key)
            raw, _ = self._raw_cache[key]
            # flags change, the content does not
            typ, data = conn.uid("FETCH", str(uid), "(UID FLAGS)")
            recs = parsers.parse_fetch(data) if typ == "OK" else []
            if recs:
                return raw, recs[0]["flags"]
        typ, data = conn.uid("FETCH", str(uid), "(UID FLAGS RFC822.SIZE BODY.PEEK[])")
        recs = [r for r in parsers.parse_fetch(data) if r["payload"] is not None] if typ == "OK" else []
        if not recs:
            raise ValueError(f"no message with uid {uid} in {box['name']}")
        raw, flags = recs[0]["payload"], recs[0]["flags"]
        self._raw_cache[key] = (raw, flags)
        while len(self._raw_cache) > CACHE_SIZE:
            self._raw_cache.popitem(last=False)
        return raw, flags

    def message(self, folder: str, uid: int) -> dict:
        def run(conn):
            box = self._resolve(conn, folder)
            raw, flags = self._raw(conn, box, uid)
            return box, raw, flags

        box, raw, flags = self._call(run)
        info = parsers.message_details(raw)
        info.update(uid=uid, folder=box["name"], size=len(raw), **parsers.flag_state(flags))
        return info

    def download(self, folder: str, uid: int, index: int, destination: str | Path | None = None) -> dict:
        def run(conn):
            box = self._resolve(conn, folder)
            raw, _ = self._raw(conn, box, uid)
            return box, raw

        box, raw = self._call(run)
        parts = parsers.attachments(parsers.parse_message(raw))
        if not parts:
            raise ValueError(f"message {uid} in {box['name']} has no attachments")
        if not 1 <= index <= len(parts):
            raise ValueError(f"attachment index must be 1..{len(parts)}")
        info, content = parts[index - 1]
        dest = Path(destination or self.settings.download_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        target = parsers.free_path(dest, parsers.safe_filename(info["filename"]), content)
        if not target.exists():
            target.write_bytes(content)
        return {"path": str(target), "bytes": len(content), "filename": info["filename"],
                "content_type": info["content_type"]}

    # -- changing -----------------------------------------------------------------

    def _existing(self, conn: imaplib.IMAP4, uids: list[int]) -> list[int]:
        """Which of these UIDs are in the selected folder."""
        uids = sorted({int(u) for u in uids})
        if not uids or uids[0] <= 0:
            raise ValueError("give one or more message uids from search_messages")
        typ, data = conn.uid("SEARCH", "UID", ",".join(map(str, uids)))
        return parsers.parse_search(data) if typ == "OK" else []

    def set_flags(self, folder: str, uids: list[int], read: bool | None = None,
                  flagged: bool | None = None) -> dict:
        if read is None and flagged is None:
            raise ValueError("nothing to change: pass read and/or flagged")

        def run(conn):
            box = self._resolve(conn, folder)
            self._select(conn, box, readonly=False)
            found = self._existing(conn, uids)
            if not found:
                raise ValueError(f"none of these uids are in {box['name']}")
            uid_set = ",".join(map(str, found))
            for flag, value in ((r"\Seen", read), (r"\Flagged", flagged)):
                if value is not None:
                    typ, data = conn.uid("STORE", uid_set, "+FLAGS.SILENT" if value else "-FLAGS.SILENT", f"({flag})")
                    if typ != "OK":
                        raise MailError(f"STORE failed: {data}")
            return {"folder": box["name"], "uids": found, "read": read, "flagged": flagged}
        return self._call(run)

    def move(self, folder: str, uids: list[int], to_folder: str) -> dict:
        def run(conn):
            box = self._resolve(conn, folder)
            try:
                target = self._resolve(conn, to_folder)
            except ValueError:
                # accounts used only through webmail can lack a trash folder
                if parsers.ROLE_ALIASES.get(to_folder.strip().casefold()) != "trash":
                    raise
                target = self._role_folder(conn, "trash", "Trash")
            if target["path"] == box["path"]:
                raise ValueError("source and destination folder are the same")
            self._select(conn, box, readonly=False)
            found = self._existing(conn, uids)
            if not found:
                raise ValueError(f"none of these uids are in {box['name']}")
            uid_set = ",".join(map(str, found))
            if "MOVE" in conn.capabilities:
                typ, data = conn.uid("MOVE", uid_set, parsers.quote(target["path"]))
            elif "UIDPLUS" in conn.capabilities:
                typ, data = conn.uid("COPY", uid_set, parsers.quote(target["path"]))
                if typ == "OK":
                    conn.uid("STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)")
                    typ, data = conn.uid("EXPUNGE", uid_set)
            else:
                # a plain EXPUNGE would also remove anything else marked deleted
                raise MailError("the server supports neither MOVE nor UIDPLUS")
            if typ != "OK":
                raise MailError(f"moving failed: {data}")
            for key in [k for k in self._raw_cache if k[0] == box["path"] and k[2] in found]:
                del self._raw_cache[key]
            return {"moved": found, "from": box["name"], "to": target["name"]}
        return self._call(run)

    # -- writing ------------------------------------------------------------------

    def _append(self, conn: imaplib.IMAP4, box: dict, data: bytes, flags: str,
                message_id: str | None = None) -> int | None:
        typ, resp = conn.append(parsers.quote(box["path"]), flags,
                                imaplib.Time2Internaldate(time.time()), data)
        if typ != "OK":
            raise MailError(f"could not save to {box['name']}: {resp}")
        uid = parsers.append_uid(resp)
        if uid is None and message_id:
            # no UIDPLUS: find what we just stored by its Message-ID
            self._select(conn, box, readonly=True)
            typ, found = conn.uid("SEARCH", "HEADER", "MESSAGE-ID", parsers.quote(message_id))
            uids = parsers.parse_search(found) if typ == "OK" else []
            uid = uids[-1] if uids else None
        return uid

    def draft(self, body: str, to=None, subject: str | None = None, cc=None, bcc=None,
              reply_to_uid: int | None = None, reply_folder: str = "inbox", reply_all: bool = False,
              attachments: list[str] | None = None) -> dict:
        s = self.settings
        files = []
        for name in attachments or []:
            path = Path(name).expanduser()
            if not path.is_file():
                raise ValueError(f"no such file: {path}")
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            files.append((path.name, path.read_bytes(), ctype))
        if sum(len(c) for _, c, _ in files) > MAX_MESSAGE_BYTES * 0.7:
            # base64 grows attachments by a third; the server takes 20 MB in total
            raise ValueError("attachments are too big for the mail server (about 14 MB at most)")

        def run(conn):
            reply = None
            if reply_to_uid is not None:
                box = self._resolve(conn, reply_folder)
                raw, _ = self._raw(conn, box, reply_to_uid)
                reply = parsers.reply_context(raw, s.address, reply_all)
            to_list = parsers.clean_addresses(to) or (reply["to"] if reply else [])
            cc_list = parsers.clean_addresses(cc) if cc is not None else (reply["cc"] if reply else [])
            if not to_list:
                raise ValueError("no recipients: give `to`")
            text = body.rstrip() + ("\n\n" + reply["quote"] if reply else "")
            msg = parsers.build_message(
                s.sender, to_list, subject or (reply["subject"] if reply else ""), text,
                cc=cc_list, bcc=parsers.clean_addresses(bcc),
                in_reply_to=reply["in_reply_to"] if reply else None,
                references=reply["references"] if reply else None, files=files)
            drafts = self._role_folder(conn, "drafts", "Drafts")
            uid = self._append(conn, drafts, msg.as_bytes(), r"(\Draft \Seen)", str(msg["message-id"]))
            return {"uid": uid, "folder": drafts["name"], "from": s.sender, "to": to_list,
                    "cc": cc_list, "bcc": parsers.clean_addresses(bcc), "subject": str(msg["subject"]),
                    "attachments": [n for n, _, _ in files], "message_id": str(msg["message-id"])}
        return self._call(run)

    def send_draft(self, uid: int) -> dict:
        """Send a draft over SMTP, keep a copy in Sent and remove the draft.
        Once SMTP has accepted the message nothing raises any more, so a
        failure afterwards can never make the caller send it a second time."""
        def load(conn):
            drafts = self._resolve(conn, "drafts")
            validity = self._select(conn, drafts, readonly=True)
            if (drafts["path"], validity, uid) in self._sent_drafts:
                raise ValueError(f"draft {uid} was already sent")
            typ, data = conn.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])")
            recs = [r for r in parsers.parse_fetch(data) if r["payload"] is not None] if typ == "OK" else []
            if not recs:
                raise ValueError(f"no draft with uid {uid} in {drafts['name']}")
            return drafts, validity, recs[0]["payload"]

        drafts, validity, raw = self._call(load)
        msg = parsers.parse_message(raw)
        recipients = parsers.recipients_of(msg)
        if not recipients:
            raise ValueError("the draft has no recipients")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("the draft is bigger than the 20 MB the mail server accepts")
        del msg["Date"]
        msg["Date"] = formatdate(localtime=True)
        refused = {addr.lower() for addr in self._smtp_send(msg)}
        self._sent_drafts.add((drafts["path"], validity, uid))

        result = {"sent_to": [r for r in recipients if r not in refused], "refused": sorted(refused),
                  "subject": parsers.header(msg, "subject"), "message_id": parsers.header(msg, "message-id")}
        warnings = []

        def file_it(conn):
            sent = self._role_folder(conn, "sent", "Sent")
            self._append(conn, sent, msg.as_bytes(), r"(\Seen)")
            result["saved_to"] = sent["name"]
            self._select(conn, drafts, readonly=False)
            conn.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
            if "UIDPLUS" in conn.capabilities:
                conn.uid("EXPUNGE", str(uid))
            else:
                warnings.append("the draft is marked deleted but still in Drafts (no UIDPLUS)")

        try:
            self._call(file_it)
        except Exception as exc:  # the mail is out; report, do not raise
            warnings.append(f"sent, but filing it failed: {exc}")
        if warnings:
            result["warnings"] = warnings
        return result

    # -- SMTP ---------------------------------------------------------------------

    def _smtp(self) -> smtplib.SMTP:
        s = self.settings
        ctx = ssl.create_default_context()
        if s.smtp_security == "ssl":
            smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.timeout, context=ctx)
        else:
            smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.timeout)
        try:
            if s.smtp_security == "starttls":
                smtp.starttls(context=ctx)
            smtp.login(s.username, s.password)
        except smtplib.SMTPAuthenticationError as exc:
            smtp.close()
            raise LoginError(f"{s.smtp_host} rejected the login: {exc}") from exc
        except BaseException:
            smtp.close()
            raise
        return smtp

    def smtp_check(self) -> str:
        with self._smtp() as smtp:
            smtp.noop()
        return f"logged in to {self.settings.smtp_host}, nothing sent"

    def _smtp_send(self, msg) -> dict:
        try:
            with self._smtp() as smtp:
                return smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"every recipient was refused: {exc.recipients}") from exc
        except smtplib.SMTPException as exc:
            raise MailError(f"sending failed: {exc}") from exc
