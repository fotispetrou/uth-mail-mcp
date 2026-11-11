"""Turn IMAP responses and raw messages into plain dicts, and build the
messages we append or send. Nothing here does I/O, so it is all testable
with fixtures."""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import unicodedata
from datetime import date, datetime, timezone, timedelta
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

# what search_messages fetches per message
HEADER_FIELDS = "DATE FROM TO CC SUBJECT MESSAGE-ID"
# in a BODYSTRUCTURE: an attachment disposition, a part that can only be a
# file, or an image without a Content-ID (one with an ID is embedded in the
# HTML, like Outlook signature logos). Mailing lists wrap every post in
# multipart/mixed with extra text parts, so that alone does not count.
_ATTACHMENT_HINT = re.compile(
    rb'\("attachment"|\("(?:application|audio|video)" "|\("message" "rfc822"|\("text" "calendar"'
    rb'|\("image" "[^"]*" (?:\([^()]*\)|NIL) NIL ', re.I)
MAX_BODY_CHARS = 30_000
MAX_LISTED_ADDRESSES = 10
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

ROLE_FLAGS = {"\\sent": "sent", "\\drafts": "drafts", "\\trash": "trash", "\\junk": "junk",
              "\\archive": "archive"}
# for servers without SPECIAL-USE: common names of the standard folders.
# Kept casefolded, like what they are compared with (casefold turns ς into σ).
ROLE_NAMES = {role: {n.casefold() for n in names} for role, names in {
    "sent": {"sent", "sent items", "sent messages", "sent mail", "sent-mail", "απεσταλμένα"},
    "drafts": {"drafts", "draft", "πρόχειρα"},
    "trash": {"trash", "deleted", "deleted items", "deleted messages", "κάδος", "διαγραμμένα"},
    "junk": {"junk", "spam", "junk e-mail", "junk email", "ανεπιθύμητα"},
    "archive": {"archive", "archives", "αρχείο", "αρχειοθετημένα"},
}.items()}
# words a caller may use instead of a folder name
ROLE_ALIASES = {k.casefold(): v for k, v in {
    "inbox": "inbox", "εισερχόμενα": "inbox", "sent": "sent", "απεσταλμένα": "sent",
    "drafts": "drafts", "πρόχειρα": "drafts", "trash": "trash", "κάδος": "trash",
    "junk": "junk", "spam": "junk", "ανεπιθύμητα": "junk", "archive": "archive",
}.items()}


# -- folders ------------------------------------------------------------------

def utf7_encode(name: str) -> str:
    """Folder name to IMAP modified UTF-7 (RFC 3501 5.1.3)."""
    out, pending = [], []

    def flush():
        if pending:
            b64 = base64.b64encode("".join(pending).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def utf7_decode(name: str) -> str:
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:  # not valid modified UTF-7; show it as it is
            return name
        chunk = name[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            try:
                out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                return name
        i = end + 1
    return "".join(out)


def quote(value: str) -> str:
    """IMAP quoted string; callers only pass ASCII here."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s*(?P<name>.*)$', re.S)


def _unquote(raw: bytes) -> str:
    s = raw.decode("utf-8", "replace").strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def parse_list_item(item) -> dict | None:
    """One LIST response from imaplib: bytes, or a (head, name) tuple when the
    server sent the name as a literal."""
    literal = None
    if isinstance(item, tuple):
        head, literal = re.sub(rb"\{\d+\}\s*$", b"", item[0].rstrip()), item[1]
    elif isinstance(item, bytes):
        head = item
    else:
        return None
    m = _LIST_RE.match(head.strip())
    if not m or (literal is None and not m.group("name").strip()):
        return None
    path = literal.decode("utf-8", "replace") if literal is not None else _unquote(m.group("name"))
    delim = None if m.group("delim") == b"NIL" else _unquote(m.group("delim"))
    flags = m.group("flags").decode().split()
    return {"path": path, "name": utf7_decode(path), "delimiter": delim, "flags": flags,
            "selectable": not any(f.lower() in ("\\noselect", "\\nonexistent") for f in flags)}


def assign_roles(folders: list[dict]) -> list[dict]:
    """Mark inbox/sent/drafts/trash/junk/archive, from SPECIAL-USE flags when
    the server has them and from the usual folder names when it does not."""
    taken = set()
    for f in folders:
        f["role"] = "inbox" if f["path"].upper() == "INBOX" else None
        for flag in f["flags"]:
            role = ROLE_FLAGS.get(flag.lower())
            if role and role not in taken:
                f["role"] = role
                taken.add(role)
    for role, names in ROLE_NAMES.items():
        if role in taken:
            continue
        for f in folders:
            leaf = f["name"].rsplit(f["delimiter"], 1)[-1] if f["delimiter"] else f["name"]
            if f["role"] is None and f["selectable"] and leaf.casefold() in names:
                f["role"] = role
                taken.add(role)
                break
    return folders


def parse_status(line) -> dict:
    """b'INBOX (MESSAGES 4 UNSEEN 2)' -> {'messages': 4, 'unseen': 2}"""
    if isinstance(line, tuple):  # folder name sent as a literal
        line = b" ".join(x for x in line if isinstance(x, bytes))
    m = re.search(rb"\(([^()]*)\)\s*$", line or b"")
    if not m:
        return {}
    return {k.decode().lower(): int(v) for k, v in re.findall(rb"([A-Z]+) (\d+)", m.group(1))}


def parse_quota(lines) -> dict | None:
    for line in lines or []:
        if isinstance(line, bytes):
            m = re.search(rb"STORAGE (\d+) (\d+)", line)
            if m:
                used, limit = int(m.group(1)), int(m.group(2))
                return {"used_mb": round(used / 1024, 1), "limit_mb": round(limit / 1024, 1),
                        "percent": round(100 * used / limit, 1) if limit else None}
    return None


# -- fetch responses ------------------------------------------------------------

def parse_fetch(data) -> list[dict]:
    """imaplib returns FETCH results as (head, literal) tuples followed by the
    rest of the response line as bytes; responses without a literal are
    plain bytes. Keep only records that carry a UID."""
    records, current = [], None
    for item in data or []:
        if isinstance(item, tuple):
            current = {"meta": item[0], "payload": item[1]}
            records.append(current)
        elif isinstance(item, bytes):
            if re.match(rb"\d+ \(", item):
                current = {"meta": item, "payload": None}
                records.append(current)
            elif current is not None:
                current["meta"] += b" " + item
    out = []
    for r in records:
        meta = r["meta"]
        uid = re.search(rb"\bUID (\d+)", meta)
        if not uid:
            continue
        flags = re.search(rb"\bFLAGS \(([^)]*)\)", meta)
        size = re.search(rb"\bRFC822\.SIZE (\d+)", meta)
        idate = re.search(rb'\bINTERNALDATE "([^"]+)"', meta)
        out.append({
            "uid": int(uid.group(1)),
            "flags": flags.group(1).decode().split() if flags else [],
            "size": int(size.group(1)) if size else None,
            "internaldate": idate.group(1).decode() if idate else None,
            "payload": r["payload"],
        })
    return out


def attachment_flags(data) -> dict[int, bool]:
    """uid -> whether its BODYSTRUCTURE shows a real attachment. Names in a
    structure can arrive as literals, which imaplib splits into tuples in the
    middle of a response, so the pieces are joined back up per message."""
    records: list[bytes] = []
    for item in data or []:
        if isinstance(item, tuple):
            head, text = item[0], b" ".join(x for x in item if isinstance(x, bytes))
        elif isinstance(item, bytes):
            head = text = item
        else:
            continue
        if re.match(rb"\d+ \(", head) or not records:
            records.append(text)
        else:
            records[-1] += b" " + text
    out = {}
    for text in records:
        uid = re.search(rb"\bUID (\d+)", text)
        if uid:
            out[int(uid.group(1))] = bool(_ATTACHMENT_HINT.search(text))
    return out


def parse_search(data) -> list[int]:
    uids = []
    for chunk in data or []:
        if isinstance(chunk, bytes):
            uids.extend(int(x) for x in chunk.split() if x.isdigit())
    return sorted(set(uids))


def append_uid(data) -> int | None:
    """UID of an appended message from the [APPENDUID validity uid] code."""
    for chunk in data or []:
        if isinstance(chunk, bytes):
            m = re.search(rb"APPENDUID \d+ (\d+)", chunk)
            if m:
                return int(m.group(1))
    return None


# -- dates ------------------------------------------------------------------------

def _local(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def date_iso(value: str | None) -> str | None:
    """Date header to local time, YYYY-MM-DD HH:MM."""
    if not value:
        return None
    try:
        return _local(parsedate_to_datetime(value))
    except (TypeError, ValueError, IndexError):
        return None


def internaldate_iso(value: str | None) -> str | None:
    """INTERNALDATE ('17-Jul-1996 02:44:25 -0700') to local time."""
    m = re.match(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)", value or "")
    if not m or m.group(2).title() not in MONTHS:
        return None
    day, mon, year, hh, mm, ss, sign, oh, om = m.groups()
    offset = timedelta(hours=int(oh), minutes=int(om)) * (1 if sign == "+" else -1)
    dt = datetime(int(year), MONTHS.index(mon.title()) + 1, int(day), int(hh), int(mm), int(ss),
                  tzinfo=timezone(offset))
    return _local(dt)


def imap_date(value: str) -> str:
    """'2026-11-10' -> '10-Nov-2026' (month names are not localised in IMAP)."""
    d = date.fromisoformat(value.strip())
    return f"{d.day}-{MONTHS[d.month - 1]}-{d.year}"


# -- headers ------------------------------------------------------------------

def decode_words(value: str) -> str:
    """Decode RFC 2047 words that some mailers leave in places they do not
    belong, like attachment names."""
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeError, LookupError, ValueError):
        return value


def _has_raw_bytes(s: str) -> bool:
    return any("\udc80" <= ch <= "\udcff" for ch in s)


def raw_bytes_decoded(s: str) -> str:
    """Headers sent with raw 8-bit bytes instead of encoded words reach us as
    surrogate escapes, which cannot even be sent as JSON (and the email
    policy turns the non-UTF-8 ones into U+FFFD). Decode the bytes as UTF-8,
    or as Greek Windows text when they plainly are not UTF-8."""
    if not _has_raw_bytes(s):
        return s
    raw = s.encode("utf-8", "surrogateescape")
    # some mailers fold a header, or end one encoded word and start the next,
    # in the middle of a UTF-8 character; whitespace between a lead byte and
    # a continuation byte can only be that
    raw = re.sub(rb"([\xc2-\xf4])(?:\r?\n)?[ \t]+([\x80-\xbf])", rb"\1\2", raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    as_utf8 = raw.decode("utf-8", "replace")
    # a stray broken character in UTF-8 text leaves a few U+FFFD; Greek
    # 8-bit text read as UTF-8 turns into almost nothing but U+FFFD
    if as_utf8.count("�") * 4 <= sum(b >= 0x80 for b in raw):
        return as_utf8
    return raw.decode("windows-1253", "replace")


def _raw_header(msg: Message, name: str) -> str | None:
    return next((str(v) for k, v in msg.raw_items() if k.lower() == name.lower()), None)


def header(msg: Message, name: str) -> str | None:
    raw = _raw_header(msg, name)
    if raw is not None and _has_raw_bytes(raw):
        value = decode_words(raw_bytes_decoded(raw))
    else:
        try:
            value = msg[name]
        except Exception:  # a header the parser chokes on; fall back to the raw text
            value = decode_words(raw) if raw is not None else None
    if value is None:
        return None
    return raw_bytes_decoded(re.sub(r"\s+", " ", str(value)).strip())


def addresses(msg: Message, name: str) -> list[str]:
    raw = _raw_header(msg, name)
    addrs = None
    if raw is not None and not _has_raw_bytes(raw):
        try:
            addrs = getattr(msg[name], "addresses", None)
        except Exception:
            addrs = None
    if addrs is None:
        text = header(msg, name)
        return [f"{n} <{a}>" if n else a for n, a in getaddresses([text]) if a] if text else []
    out = []
    for a in addrs:
        if not a.addr_spec or a.addr_spec == "<>":
            continue
        entry = f"{a.display_name} <{a.addr_spec}>" if a.display_name else a.addr_spec
        out.append(raw_bytes_decoded(entry))  # encoded words that split a character
    return out


def emails_of(entries: list[str]) -> list[str]:
    return [a.lower() for _, a in getaddresses(entries) if a]


def short_list(items: list[str], limit: int = MAX_LISTED_ADDRESSES) -> list[str]:
    if len(items) <= limit:
        return items
    return items[:limit] + [f"... and {len(items) - limit} more"]


def flag_state(flags: list[str]) -> dict:
    lowered = {f.lower() for f in flags}
    return {"unread": "\\seen" not in lowered, "flagged": "\\flagged" in lowered,
            "answered": "\\answered" in lowered}


def summarize(record: dict) -> dict:
    """One line of search_messages from a header-only FETCH record. The
    address lists are complete here (local matching needs them); compact()
    shortens them for output."""
    msg = BytesHeaderParser(policy=policy.default).parsebytes(record.get("payload") or b"")
    raw_date = header(msg, "date")
    item = {
        "uid": record["uid"],
        "date": raw_date,
        "date_iso": date_iso(raw_date) or internaldate_iso(record.get("internaldate")),
        "from": ", ".join(addresses(msg, "from")) or None,
        "to": addresses(msg, "to"),
        "cc": addresses(msg, "cc"),
        "subject": header(msg, "subject") or "",
    }
    item.update(flag_state(record.get("flags", [])))
    item["size"] = record.get("size")
    return item


def compact(item: dict) -> dict:
    out = dict(item, to=short_list(item["to"]), cc=short_list(item["cc"]))
    if not out["cc"]:
        del out["cc"]
    return out


# -- search -----------------------------------------------------------------------

def fold(s: str) -> str:
    """Case- and accent-insensitive form, so 'εξεταση' finds 'Εξέταση'."""
    s = unicodedata.normalize("NFD", s.casefold())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def search_criteria(query: str = "", sender: str = "", to: str = "", subject: str = "",
                    since: str | None = None, before: str | None = None,
                    unread_only: bool = False, flagged_only: bool = False):
    """IMAP SEARCH keys for the server, plus what has to be done locally.

    imaplib sends at most one literal per command and only ASCII otherwise, so
    a non-ASCII `query` (full text, only the server can search bodies) goes as
    a UTF-8 literal, and non-ASCII header terms are matched locally on the
    fetched headers, which also makes them accent-insensitive.

    Returns (keys, literal, local) where literal is (KEY, text) or None and
    local maps summary fields to folded search text."""
    keys: list[str] = []
    if unread_only:
        keys.append("UNSEEN")
    if flagged_only:
        keys.append("FLAGGED")
    if since:
        keys += ["SINCE", imap_date(since)]
    if before:
        keys += ["BEFORE", imap_date(before)]
    local = {}
    for key, field, value in (("FROM", "from", sender), ("TO", "to", to), ("SUBJECT", "subject", subject)):
        value = (value or "").strip()
        if not value:
            continue
        if value.isascii():
            keys += [key, quote(value)]
        else:
            local[field] = fold(value)
    literal = None
    query = (query or "").strip()
    if query:
        if query.isascii():
            keys += ["TEXT", quote(query)]
        else:
            literal = ("TEXT", query)
    return keys, literal, local


def matches(summary: dict, local: dict) -> bool:
    """Local filters from search_criteria; 'to' also looks at Cc."""
    for field, needle in local.items():
        if field == "to":
            value = " ".join(summary.get("to", []) + summary.get("cc", []))
        else:
            value = summary.get(field) or ""
        if needle not in fold(value):
            return False
    return True


# -- message bodies and attachments -----------------------------------------------

def parse_message(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def part_text(part: Message) -> str:
    """Decoded text of a part, with plain \\n line ends (base64 text keeps
    the CRLFs of the canonical form)."""
    try:
        text = part.get_content()
    except (LookupError, UnicodeError, AssertionError):  # unknown or wrong charset
        payload = part.get_payload(decode=True) or b""
        text = None
        for charset in (part.get_content_charset(), "utf-8", "windows-1253"):
            if not charset:
                continue
            try:
                text = payload.decode(charset)
                break
            except (LookupError, UnicodeDecodeError):
                continue
        if text is None:
            text = payload.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def html_to_text(markup: str) -> str:
    """Readable text from an HTML mail, keeping line breaks and link targets."""
    doc = BeautifulSoup(markup or "", "html.parser")
    for junk in doc(["script", "style", "head", "title"]):
        junk.decompose()
    for a in doc.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = a["href"].strip()
        if href.lower().startswith(("http://", "https://")) and href not in label:
            a.replace_with(f"{label} ({href})" if label else href)
    for br in doc.find_all("br"):
        br.replace_with("\n")
    for block in doc.find_all(["p", "div", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
                               "blockquote", "pre"]):
        block.insert_before("\n")
        block.insert_after("\n")
    for cell in doc.find_all(["td", "th"]):
        cell.insert_after(" ")
    s = doc.get_text()
    s = re.sub(r"[ \t\xa0\u200b]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def body_text(msg: EmailMessage) -> tuple[str, str]:
    """(text, 'plain' | 'html' | 'none'). Plain text wins unless it is empty."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = part_text(plain).strip()
        if text:
            return text, "plain"
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        return html_to_text(part_text(html)), "html"
    return "", "none"


def attachments(msg: EmailMessage) -> list[tuple[dict, bytes]]:
    """Attachments in order, each as (info, content). Images embedded in the
    HTML (those with a Content-ID, unless explicitly attachments) are left
    out; a forwarded message counts as one attachment instead of being
    opened up."""
    body_parts = {id(p) for p in (msg.get_body(("plain",)), msg.get_body(("html",))) if p is not None}
    found: list[Message] = []

    def walk(part):
        if part is not msg and part.get_content_type() == "message/rfc822":
            found.append(part)
            return
        if part.is_multipart():
            for sub in part.iter_parts():
                walk(sub)
            return
        if id(part) in body_parts:
            return
        disposition = part.get_content_disposition()
        if part.get_content_maintype() == "image" and part.get("content-id") and disposition != "attachment":
            return
        if (part.get_filename() or disposition == "attachment"
                or part.get_content_maintype() not in ("text", "multipart")
                or part.get_content_type() == "text/calendar"):
            found.append(part)

    walk(msg)
    out = []
    for index, part in enumerate(found, 1):
        ctype = part.get_content_type()
        name = part.get_filename()
        if ctype == "message/rfc822":
            inner = part.get_payload(0)
            try:
                content = inner.as_bytes()
            except Exception:  # a forwarded message too broken to re-serialise
                content = b""
            name = name or (header(inner, "subject") or "message") + ".eml"
        else:
            content = part.get_payload(decode=True) or b""
            name = name or f"attachment-{index}{mimetypes.guess_extension(ctype) or '.bin'}"
        out.append(({"index": index, "filename": raw_bytes_decoded(decode_words(name)), "content_type": ctype,
                     "size": len(content)}, content))
    return out


def message_details(raw: bytes, max_chars: int = MAX_BODY_CHARS) -> dict:
    msg = parse_message(raw)
    body, fmt = body_text(msg)
    raw_date = header(msg, "date")
    info = {
        "subject": header(msg, "subject") or "",
        "from": ", ".join(addresses(msg, "from")) or None,
        "to": short_list(addresses(msg, "to"), 50),
        "cc": short_list(addresses(msg, "cc"), 50),
        "reply_to": ", ".join(addresses(msg, "reply-to")) or None,
        "date": raw_date,
        "date_iso": date_iso(raw_date),
        "message_id": header(msg, "message-id"),
        "in_reply_to": header(msg, "in-reply-to"),
        "body": body[:max_chars],
        "body_format": fmt,
        "body_truncated": len(body) > max_chars,
        "attachments": [info for info, _ in attachments(msg)],
    }
    bcc = addresses(msg, "bcc")
    if bcc:
        info["bcc"] = bcc
    return info


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(name: str) -> str:
    """A name Windows (and everyone else) accepts."""
    name = _UNSAFE.sub("_", name or "").strip().rstrip(". ")
    if not name:
        return "attachment"
    if name.split(".", 1)[0].lower() in _RESERVED:
        name = "_" + name
    if len(name) > 150:
        stem, ext = os.path.splitext(name)
        name = stem[: 150 - len(ext)] + ext
    return name


def free_path(directory: Path, name: str, content: bytes) -> Path:
    """directory/name, or 'name (2)' etc. if a different file is already there.
    An identical file is reused rather than duplicated."""
    target = directory / name
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        if target.stat().st_size == len(content) and target.read_bytes() == content:
            return target
        target = directory / f"{stem} ({n}){suffix}"
        n += 1
    return target


# -- writing ------------------------------------------------------------------------

def clean_addresses(value) -> list[str]:
    """Accept a list or a comma/semicolon separated string; check each has an @."""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for name, addr in getaddresses([str(i).replace(";", ",") for i in items]):
        if not addr:
            continue
        if "@" not in addr or " " in addr:
            raise ValueError(f"not an email address: {addr!r}")
        out.append(f"{name} <{addr}>" if name else addr)
    return out


def reply_subject(subject: str) -> str:
    subject = subject or ""
    return subject if re.match(r"^\s*(re|απ|σχετ)\s*:", subject, re.I) else f"Re: {subject}".strip()


def reply_context(raw: bytes, own_address: str, reply_all: bool = False) -> dict:
    """Recipients, subject, threading headers and quoted text for a reply."""
    msg = parse_message(raw)
    own = own_address.lower()

    def others(entries):
        return [e for e in entries if own not in emails_of([e])]

    reply_to = addresses(msg, "reply-to") or addresses(msg, "from")
    # answering one of our own sent messages goes back to its recipients
    to = others(reply_to) or others(addresses(msg, "to")) or reply_to
    cc = []
    if reply_all:
        seen = set(emails_of(to)) | {own}
        for entry in addresses(msg, "to") + addresses(msg, "cc"):
            email = emails_of([entry])
            if email and email[0] not in seen:
                seen.add(email[0])
                cc.append(entry)
    message_id = header(msg, "message-id")
    references = " ".join(x for x in (header(msg, "references"), message_id) if x)
    body, _ = body_text(msg)
    quoted = "\n".join("> " + line if line else ">" for line in body.splitlines())
    intro = "\n".join([
        "-------- Αρχικό μήνυμα --------",
        f"Από: {', '.join(addresses(msg, 'from'))}",
        f"Ημερομηνία: {date_iso(header(msg, 'date')) or header(msg, 'date') or ''}",
        f"Θέμα: {header(msg, 'subject') or ''}",
    ])
    return {"to": to, "cc": cc, "subject": reply_subject(header(msg, "subject") or ""),
            "in_reply_to": message_id, "references": references or None,
            "quote": intro + "\n\n" + quoted}


def build_message(sender: str, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  bcc: list[str] | None = None, in_reply_to: str | None = None,
                  references: str | None = None,
                  files: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:
    # 7bit: Greek text goes out base64-encoded, which every server accepts
    # whether or not it announces 8BITMIME
    msg = EmailMessage(policy=policy.SMTP.clone(cte_type="7bit"))
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].strip(">") or None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    for name, content, ctype in files or []:
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(content, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
    return msg


def recipients_of(msg: Message) -> list[str]:
    return emails_of(addresses(msg, "to") + addresses(msg, "cc") + addresses(msg, "bcc"))
