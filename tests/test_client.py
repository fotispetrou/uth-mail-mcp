"""MailClient against an in-memory stand-in for imaplib.IMAP4_SSL that returns
data in the shapes imaplib does, and an smtplib.SMTP whose only fake part is
the network, so the real send_message (Bcc removal, flattening) runs."""

import asyncio
import imaplib
import re
import smtplib
from email.message import EmailMessage

import pytest

from uth_mail_mcp import client as client_mod
from uth_mail_mcp import parsers, server
from uth_mail_mcp.client import LoginError, MailClient, MailError
from uth_mail_mcp.config import Settings

USER, PASSWORD = "student", "secret"


def make_raw(uid, subject, sender="Prof <prof@uth.gr>", body="κείμενο", attachment=None, to="student@uth.gr"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = f"Mon, {uid % 28 + 1:02d} Nov 2026 10:00:00 +0200"
    msg["Message-ID"] = f"<m{uid}@uth.gr>"
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    return msg.as_bytes()


class Mailbox:
    def __init__(self, validity):
        self.validity = validity
        self.next_uid = 1
        self.messages = {}

    def add(self, raw, flags=()):
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = {"raw": raw, "flags": set(flags)}
        return uid


class FakeServer:
    def __init__(self, caps=("IMAP4REV1", "MOVE", "UIDPLUS", "NAMESPACE", "QUOTA"),
                 folders=("INBOX", "Sent", "Trash", "Drafts")):
        self.caps = caps
        self.boxes = {name: Mailbox(1000 + i) for i, name in enumerate(folders)}
        self.connections = []
        self.commands = []
        self.drop_on_noop = False


class FakeIMAP:
    """Just enough of imaplib.IMAP4 over FakeServer."""

    def __init__(self, server, host, port, **kwargs):
        self.server = server
        self.capabilities = ("IMAP4REV1", "AUTH=PLAIN")
        self.literal = None
        self.selected = None
        self.readonly = True
        self.untagged = {}
        server.connections.append(self)

    def login(self, user, password):
        if (user, password) != (USER, PASSWORD):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Authentication failed.")
        return "OK", [b"Logged in"]

    def capability(self):
        return "OK", [" ".join(self.server.caps).encode()]

    def namespace(self):
        return "OK", [b'(("" "/")) NIL NIL']

    def list(self, directory='""', pattern="*"):
        return "OK", [f'(\\HasNoChildren) "/" "{parsers.utf7_encode(n)}"'.encode() for n in self.server.boxes]

    @staticmethod
    def _name(mailbox):
        return parsers.utf7_decode(parsers._unquote(mailbox.encode()))

    def status(self, mailbox, names):
        box = self.server.boxes[self._name(mailbox)]
        unseen = sum("\\Seen" not in m["flags"] for m in box.messages.values())
        return "OK", [f"{mailbox} (MESSAGES {len(box.messages)} UNSEEN {unseen})".encode()]

    def getquotaroot(self, mailbox):
        return "OK", [[b'"INBOX" "User quota"'], [b'"User quota" (STORAGE 2048 1048576)']]

    def select(self, mailbox, readonly=False):
        self.untagged = {}
        name = self._name(mailbox)
        if name not in self.server.boxes:
            return "NO", [b"Mailbox doesn't exist"]
        self.selected, self.readonly = name, readonly
        box = self.server.boxes[name]
        self.untagged["UIDVALIDITY"] = [str(box.validity).encode()]
        return "OK", [str(len(box.messages)).encode()]

    def response(self, code):
        return code, self.untagged.pop(code, [None])

    def create(self, mailbox):
        self.server.boxes[self._name(mailbox)] = Mailbox(2000 + len(self.server.boxes))
        return "OK", [b"Created"]

    def subscribe(self, mailbox):
        return "OK", [b"Subscribed"]

    def append(self, mailbox, flags, date_time, message):
        box = self.server.boxes.get(self._name(mailbox))
        if box is None:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
        uid = box.add(message, re.findall(r"\\\w+", flags or ""))
        if "UIDPLUS" in self.server.caps:
            return "OK", [f"[APPENDUID {box.validity} {uid}] Append completed.".encode()]
        return "OK", [b"Append completed."]

    def noop(self):
        if self.server.drop_on_noop:
            raise imaplib.IMAP4.abort("socket error: EOF")
        return "OK", [b"NOOP completed"]

    def logout(self):
        return "BYE", [b"Logging out"]

    def shutdown(self):
        pass

    # -- UID commands --------------------------------------------------------

    def uid(self, command, *args):
        literal, self.literal = self.literal, None
        self.server.commands.append((command, args, literal))
        box = self.server.boxes[self.selected]
        return getattr(self, "_uid_" + command.lower())(box, list(args), literal)

    @staticmethod
    def _uids(spec):
        return [int(x) for x in str(spec).split(",")]

    def _uid_search(self, box, args, literal):
        tests = []
        while args:
            key = args.pop(0).upper()
            if key == "CHARSET":
                args.pop(0)
            elif key == "ALL":
                pass
            elif key == "UNSEEN":
                tests.append(lambda m: "\\Seen" not in m["flags"])
            elif key == "FLAGGED":
                tests.append(lambda m: "\\Flagged" in m["flags"])
            elif key in ("SINCE", "BEFORE"):
                args.pop(0)
            elif key == "UID":
                wanted = set(self._uids(args.pop(0)))
                tests.append(lambda m, w=wanted: m["uid"] in w)
            elif key == "HEADER":
                field, value = args.pop(0), parsers._unquote(args.pop(0).encode())
                tests.append(lambda m, f=field, v=value: v in str(parsers.parse_message(m["raw"])[f]))
            elif key in ("FROM", "TO", "SUBJECT", "TEXT"):
                value = parsers._unquote(args.pop(0).encode()) if args else literal.decode()
                if key == "TEXT":
                    tests.append(lambda m, v=value: v.lower() in parsers.message_details(m["raw"])["body"].lower()
                                 or v.lower() in parsers.message_details(m["raw"])["subject"].lower())
                else:
                    tests.append(lambda m, k=key, v=value: v.lower() in str(parsers.parse_message(m["raw"])[k]).lower())
            else:
                raise imaplib.IMAP4.error(f"SEARCH key {key} not in the fake")
        hits = [uid for uid, m in box.messages.items() if all(t(dict(m, uid=uid)) for t in tests)]
        return "OK", [" ".join(map(str, hits)).encode()]

    def _uid_fetch(self, box, args, literal):
        uids, items = self._uids(args[0]), args[1]
        out = []
        for seq, uid in enumerate(sorted(box.messages), 1):
            if uid not in uids:
                continue
            m = box.messages[uid]
            head = f"{seq} (UID {uid} FLAGS ({' '.join(sorted(m['flags']))})"
            if "INTERNALDATE" in items:
                head += ' INTERNALDATE "01-Nov-2026 10:00:00 +0200"'
            if "RFC822.SIZE" in items:
                head += f" RFC822.SIZE {len(m['raw'])}"
            if "BODYSTRUCTURE" in items:
                out.append(f"{head} BODYSTRUCTURE {self._structure(parsers.parse_message(m['raw']))})".encode())
            elif "BODY.PEEK[]" in items:
                payload = m["raw"]
                out += [(f"{head} BODY[] {{{len(payload)}}}".encode(), payload), b")"]
            elif "HEADER.FIELDS" in items:
                wanted = re.search(r"HEADER\.FIELDS \(([^)]*)\)", items).group(1).split()
                # the header lines as stored, still RFC 2047 encoded, like a server sends them
                raw_items = parsers.parse_message(m["raw"]).raw_items()
                payload = "".join(f"{k}: {v}\r\n" for k, v in raw_items if k.upper() in wanted).encode() + b"\r\n"
                out += [(f"{head} BODY[HEADER.FIELDS ({' '.join(wanted)})] {{{len(payload)}}}".encode(), payload), b")"]
            else:
                out.append((head + ")").encode())
        return "OK", out

    def _structure(self, part):
        """A cut-down BODYSTRUCTURE: types and dispositions, which is what the
        client looks at."""
        if part.is_multipart():
            inner = "".join(self._structure(p) for p in part.iter_parts())
            return f'({inner} "{part.get_content_subtype()}")'
        disp = part.get_content_disposition()
        disp = f'("{disp}" NIL)' if disp else "NIL"
        return (f'("{part.get_content_maintype()}" "{part.get_content_subtype()}" NIL NIL NIL "7bit" 1 NIL '
                f'{disp} NIL NIL)')

    def _writable(self):
        if self.readonly:
            raise imaplib.IMAP4.error("[READ-ONLY] Mailbox is read-only")

    def _uid_store(self, box, args, literal):
        self._writable()
        flags = set(re.findall(r"\\\w+", args[2]))
        for uid in self._uids(args[0]):
            if uid in box.messages:
                if args[1].startswith("+"):
                    box.messages[uid]["flags"] |= flags
                else:
                    box.messages[uid]["flags"] -= flags
        return "OK", [None]

    def _uid_copy(self, box, args, literal):
        target = self.server.boxes[self._name(args[1])]
        for uid in self._uids(args[0]):
            if uid in box.messages:
                target.add(box.messages[uid]["raw"], box.messages[uid]["flags"])
        return "OK", [None]

    def _uid_move(self, box, args, literal):
        self._writable()
        self._uid_copy(box, args, literal)
        for uid in self._uids(args[0]):
            box.messages.pop(uid, None)
        return "OK", [None]

    def _uid_expunge(self, box, args, literal):
        self._writable()
        for uid in self._uids(args[0]):
            if uid in box.messages and "\\Deleted" in box.messages[uid]["flags"]:
                del box.messages[uid]
        return "OK", [None]


class FakeSMTP(smtplib.SMTP):
    """The real class with the network replaced."""
    sent: list = []
    accept_login = True

    def connect(self, host="localhost", port=0, source_address=None):
        return 220, b"fake ready"

    def starttls(self, *args, **kwargs):
        return 220, b"ready"

    def login(self, user, password, **kwargs):
        if not (self.accept_login and (user, password) == (USER, PASSWORD)):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed")
        return 235, b"ok"

    def ehlo_or_helo_if_needed(self):
        pass

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        FakeSMTP.sent.append((from_addr, list(to_addrs), msg))
        return {}

    def noop(self):
        return 250, b"ok"


def settings(**kw):
    base = dict(username=USER, password=PASSWORD, address="student@uth.gr", imap_host="mail.ds.uth.gr",
                imap_port=993, imap_security="ssl", smtp_host="smtp.uth.gr", smtp_port=587,
                smtp_security="starttls", download_dir=None, display_name="Γιώργος Παπαδόπουλος")
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    srv = FakeServer()
    inbox = srv.boxes["INBOX"]
    inbox.add(make_raw(1, "Ανακοίνωση εξεταστικής", body="Η εξεταστική ξεκινά τη Δευτέρα"), ["\\Seen"])
    inbox.add(make_raw(2, "Lab 3 results", sender="ta@uth.gr", body="see attached"), ["\\Seen"])
    inbox.add(make_raw(3, "Εργαστήριο Δικτύων", attachment=("βαθμοί.pdf", b"%PDF grades")))
    inbox.add(make_raw(4, "Αλλαγή αίθουσας", sender="Γραμματεία <secretary@ds.uth.gr>"), ["\\Flagged"])
    monkeypatch.setattr(client_mod.imaplib, "IMAP4_SSL", lambda host, port, **kw: FakeIMAP(srv, host, port, **kw))
    monkeypatch.setattr(client_mod.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    FakeSMTP.accept_login = True
    srv.client = MailClient(settings(download_dir=tmp_path))
    return srv


def test_whoami_and_folders(fake):
    info = fake.client.whoami()
    assert info["inbox_messages"] == 4 and info["inbox_unread"] == 2
    assert info["quota"] == {"used_mb": 2.0, "limit_mb": 1024.0, "percent": 0.2}
    folders = {f["name"]: f for f in fake.client.folders()}
    assert folders["INBOX"]["role"] == "inbox" and folders["Sent"]["role"] == "sent"
    assert folders["Drafts"]["messages"] == 0
    assert len(fake.connections) == 1  # one login, reused


def test_search_latest_and_filters(fake):
    c = fake.client
    assert [m["uid"] for m in c.search()] == [4, 3, 2, 1]
    assert [m["uid"] for m in c.search(limit=2)] == [4, 3]
    assert [m["uid"] for m in c.search(unread_only=True)] == [4, 3]
    assert [m["uid"] for m in c.search(flagged_only=True)] == [4]
    assert [m["uid"] for m in c.search(subject="lab")] == [2]
    first = c.search(limit=1)[0]
    assert first["folder"] == "INBOX" and first["subject"] == "Αλλαγή αίθουσας"
    assert first["from"] == "Γραμματεία <secretary@ds.uth.gr>" and first["flagged"]
    assert [m["has_attachments"] for m in c.search()] == [False, True, False, False]


def test_greek_header_filters_are_local_and_accent_insensitive(fake):
    found = fake.client.search(subject="εργαστηριο δικτυων")
    assert [m["uid"] for m in found] == [3]
    assert found[0]["has_attachments"] is True
    # nothing Greek was sent to the server for it
    command, args, literal = next(c for c in fake.commands if c[0] == "SEARCH")
    assert literal is None and all(a.isascii() for a in args)
    assert [m["uid"] for m in fake.client.search(sender="γραμματεια")] == [4]


def test_greek_full_text_goes_as_utf8_literal(fake):
    found = fake.client.search(query="εξεταστική")
    assert [m["uid"] for m in found] == [1]
    search = next(c for c in fake.commands if c[0] == "SEARCH" and c[2])
    assert search[1][:2] == ("CHARSET", "UTF-8") and search[1][-1] == "TEXT"
    assert search[2] == "εξεταστική".encode()


def test_message_is_read_without_marking_it(fake):
    info = fake.client.message("inbox", 3)
    assert info["subject"] == "Εργαστήριο Δικτύων" and info["body"] == "κείμενο"
    assert info["attachments"] == [{"index": 1, "filename": "βαθμοί.pdf", "content_type": "application/pdf",
                                    "size": 11}]
    assert info["unread"] is True
    assert "\\Seen" not in fake.boxes["INBOX"].messages[3]["flags"]
    fake.commands.clear()
    fake.client.message("inbox", 3)
    assert [c[2] for c in fake.commands] == [None] and "BODY.PEEK[]" not in fake.commands[0][1][1]
    with pytest.raises(ValueError, match="no message with uid 99"):
        fake.client.message("inbox", 99)


def test_download_attachment(fake, tmp_path):
    first = fake.client.download("inbox", 3, 1)
    assert first["filename"] == "βαθμοί.pdf" and first["bytes"] == 11
    assert (tmp_path / "βαθμοί.pdf").read_bytes() == b"%PDF grades"
    assert fake.client.download("inbox", 3, 1)["path"] == first["path"]  # same file, not a copy
    with pytest.raises(ValueError, match="1..1"):
        fake.client.download("inbox", 3, 2)
    with pytest.raises(ValueError, match="no attachments"):
        fake.client.download("inbox", 1, 1)


def test_flags_and_move(fake):
    c = fake.client
    assert c.set_flags("inbox", [3, 99], read=True)["uids"] == [3]
    assert "\\Seen" in fake.boxes["INBOX"].messages[3]["flags"]
    c.set_flags("INBOX", [1], read=False, flagged=True)
    assert fake.boxes["INBOX"].messages[1]["flags"] == {"\\Flagged"}
    with pytest.raises(ValueError, match="nothing to change"):
        c.set_flags("inbox", [1])
    with pytest.raises(ValueError, match="none of these"):
        c.set_flags("inbox", [99], read=True)
    moved = c.move("inbox", [2], "trash")
    assert moved == {"moved": [2], "from": "INBOX", "to": "Trash"}
    assert 2 not in fake.boxes["INBOX"].messages and len(fake.boxes["Trash"].messages) == 1
    with pytest.raises(ValueError, match="no folder 'Archive'"):
        c.move("inbox", [1], "Archive")


def test_trash_is_created_when_missing(fake):
    del fake.boxes["Trash"]
    assert fake.client.move("inbox", [1], "κάδος")["to"] == "Trash"
    assert len(fake.boxes["Trash"].messages) == 1
    with pytest.raises(ValueError, match="no folder 'Archive'"):
        fake.client.move("inbox", [2], "Archive")


def test_move_without_move_extension(fake):
    fake.caps = ("IMAP4REV1", "UIDPLUS")
    fake.client.move("inbox", [1], "Trash")
    assert [c[0] for c in fake.commands[-3:]] == ["COPY", "STORE", "EXPUNGE"]
    assert 1 not in fake.boxes["INBOX"].messages
    fake.caps = ("IMAP4REV1",)
    fresh = MailClient(settings(download_dir=None))
    with pytest.raises(MailError, match="neither MOVE nor UIDPLUS"):
        fresh.move("inbox", [2], "Trash")


def test_draft_reply_and_send(fake):
    c = fake.client
    draft = c.draft("Ευχαριστώ, θα είμαι εκεί.", reply_to_uid=3, bcc="me@uth.gr")
    assert draft["folder"] == "Drafts" and draft["to"] == ["Prof <prof@uth.gr>"]
    assert draft["subject"] == "Re: Εργαστήριο Δικτύων" and draft["from"] == "Γιώργος Παπαδόπουλος <student@uth.gr>"
    stored = fake.boxes["Drafts"].messages[draft["uid"]]
    assert stored["flags"] == {"\\Draft", "\\Seen"}
    text = parsers.message_details(stored["raw"])
    assert text["in_reply_to"] == "<m3@uth.gr>"
    assert text["body"].startswith("Ευχαριστώ, θα είμαι εκεί.\n\n-------- Αρχικό μήνυμα --------")

    result = c.send_draft(draft["uid"])
    assert result["sent_to"] == ["prof@uth.gr", "me@uth.gr"] and result["refused"] == []
    assert result["saved_to"] == "Sent" and "warnings" not in result
    (from_addr, rcpts, flat), = FakeSMTP.sent
    assert from_addr == "student@uth.gr" and rcpts == ["prof@uth.gr", "me@uth.gr"]
    assert b"Bcc" not in flat.split(b"\r\n\r\n", 1)[0]  # recipients do not see the Bcc
    assert draft["uid"] not in fake.boxes["Drafts"].messages
    assert len(fake.boxes["Sent"].messages) == 1
    with pytest.raises(ValueError):
        c.send_draft(draft["uid"])
    assert len(FakeSMTP.sent) == 1


def test_draft_creates_missing_drafts_folder_and_checks_input(fake, tmp_path):
    del fake.boxes["Drafts"]
    c = fake.client
    with pytest.raises(ValueError, match="no recipients"):
        c.draft("hi")
    with pytest.raises(ValueError, match="no such file"):
        c.draft("hi", to="a@uth.gr", attachments=[str(tmp_path / "missing.pdf")])
    (tmp_path / "notes.txt").write_text("σημειώσεις", encoding="utf-8")
    draft = c.draft("hi", to=["a@uth.gr"], subject="Σημειώσεις", attachments=[str(tmp_path / "notes.txt")])
    assert "Drafts" in fake.boxes and draft["attachments"] == ["notes.txt"]
    assert draft["uid"] in fake.boxes["Drafts"].messages


def test_send_failure_keeps_the_draft(fake):
    draft = fake.client.draft("hi", to="a@uth.gr", subject="x")
    FakeSMTP.accept_login = False
    with pytest.raises(LoginError):
        fake.client.send_draft(draft["uid"])
    assert draft["uid"] in fake.boxes["Drafts"].messages and FakeSMTP.sent == []


def test_filing_problems_after_sending_do_not_raise(fake, monkeypatch):
    draft = fake.client.draft("hi", to="a@uth.gr", subject="x")
    monkeypatch.setattr(FakeIMAP, "append", lambda self, *a: ("NO", [b"[OVERQUOTA] Quota exceeded"]))
    result = fake.client.send_draft(draft["uid"])
    assert len(FakeSMTP.sent) == 1
    assert "Quota exceeded" in result["warnings"][0]


def test_wrong_password_is_not_retried_right_away(monkeypatch):
    srv = FakeServer()
    monkeypatch.setattr(client_mod.imaplib, "IMAP4_SSL", lambda host, port, **kw: FakeIMAP(srv, host, port, **kw))
    c = MailClient(settings(password="wrong"))
    with pytest.raises(LoginError, match="rejected the login"):
        c.folders()
    with pytest.raises(LoginError, match="not retrying"):
        c.folders()
    assert len(srv.connections) == 1


def test_stale_connection_is_replaced(fake, monkeypatch):
    fake.client.folders()
    fake.drop_on_noop = True
    fake.client._last_used -= client_mod.IDLE_CHECK + 1
    fake.client.folders()
    assert len(fake.connections) == 2


def test_dropped_connection_mid_call_is_reported(fake, monkeypatch):
    fake.client.folders()

    def boom(self, *args):
        raise imaplib.IMAP4.abort("socket error: EOF")

    monkeypatch.setattr(FakeIMAP, "uid", boom)
    with pytest.raises(MailError, match="dropped"):
        fake.client.search()
    assert fake.client._imap is None


def test_tools_over_mcp(fake, monkeypatch):
    """The real MCP server in-process, with this client behind it."""
    from mcp.client import Client

    monkeypatch.setattr(server, "_client", fake.client)

    async def run():
        async with Client(server.server) as mcp:
            found = await mcp.call_tool("search_messages", {"subject": "εργαστήριο"})
            message = await mcp.call_tool("get_message", {"uid": 3})
            bad = await mcp.call_tool("get_message", {"uid": 3, "folder": "nope"})
            draft = await mcp.call_tool("create_draft", {"body": "Γεια σας", "to": "prof@uth.gr",
                                                         "subject": "Ερώτηση"})
            uid = int(re.search(r'"uid": (\d+)', draft.content[0].text).group(1))
            sent = await mcp.call_tool("send_draft", {"uid": uid})
            return found, message, bad, draft, sent

    found, message, bad, draft, sent = asyncio.run(run())
    assert not found.is_error and '"uid": 3' in found.content[0].text
    assert not message.is_error and "βαθμοί.pdf" in message.content[0].text
    assert bad.is_error and "no folder 'nope'" in bad.content[0].text
    assert not draft.is_error and not sent.is_error and '"sent_to"' in sent.content[0].text
    assert FakeSMTP.sent[0][1] == ["prof@uth.gr"]
