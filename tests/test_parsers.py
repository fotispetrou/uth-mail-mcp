"""Fixtures below mirror what imaplib hands back for Dovecot's responses, and
messages as Greek university mail actually arrives (encoded words in
ISO-8859-7, RFC 2231 file names, HTML-only bodies)."""

from datetime import datetime, timedelta, timezone
from email.header import Header
from email.message import EmailMessage

import pytest

from uth_mail_mcp import parsers


def test_utf7_rfc3501_example_and_roundtrip():
    assert parsers.utf7_encode("~peter/mail/台北/日本語") == "~peter/mail/&U,BTFw-/&ZeVnLIqe-"
    assert parsers.utf7_decode("~peter/mail/&U,BTFw-/&ZeVnLIqe-") == "~peter/mail/台北/日本語"
    for name in ("INBOX", "Απεσταλμένα", "Μαθήματα/Δίκτυα & Ασφάλεια", "A&B"):
        encoded = parsers.utf7_encode(name)
        assert encoded.isascii()
        assert parsers.utf7_decode(encoded) == name
    assert parsers.utf7_encode("A&B") == "A&-B"
    assert parsers.utf7_decode("broken&ZeVn") == "broken&ZeVn"


def test_parse_list_items():
    sent = parsers.utf7_encode("Απεσταλμένα")
    items = [
        b'(\\HasNoChildren) "." INBOX',
        b'(\\HasNoChildren \\Sent) "." "Sent Items"',
        (('(\\HasNoChildren) "." {%d}' % len(sent)).encode(), sent.encode()),
        b'(\\Noselect \\HasChildren) NIL "Shared"',
        b'(\\HasNoChildren) "/" "quote \\"in\\" name"',
    ]
    parsed = [parsers.parse_list_item(i) for i in items]
    assert parsed[0] == {"path": "INBOX", "name": "INBOX", "delimiter": ".", "flags": ["\\HasNoChildren"],
                         "selectable": True}
    assert parsed[1]["path"] == "Sent Items" and "\\Sent" in parsed[1]["flags"]
    assert parsed[2]["path"] == sent and parsed[2]["name"] == "Απεσταλμένα"
    assert parsed[3]["delimiter"] is None and parsed[3]["selectable"] is False
    assert parsed[4]["path"] == 'quote "in" name'
    assert parsers.parse_list_item(b"garbage") is None


def folder(path, flags=(), delim="."):
    return {"path": path, "name": parsers.utf7_decode(path), "delimiter": delim, "flags": list(flags),
            "selectable": True}


def test_roles_prefer_special_use_flags():
    folders = parsers.assign_roles([
        folder("INBOX"), folder("Sent"), folder("Sent Items", ["\\Sent"]), folder("Drafts", ["\\Drafts"]),
        folder("Trash", ["\\Trash"]), folder("Junk", ["\\Junk"]), folder("Lectures"),
    ])
    roles = {f["path"]: f["role"] for f in folders}
    assert roles == {"INBOX": "inbox", "Sent": None, "Sent Items": "sent", "Drafts": "drafts",
                     "Trash": "trash", "Junk": "junk", "Lectures": None}


def test_roles_from_names_without_special_use():
    folders = parsers.assign_roles([
        folder("INBOX"), folder("INBOX.sent-mail"), folder("INBOX.Drafts"),
        folder(parsers.utf7_encode("INBOX.Κάδος")), folder("INBOX.spam"),
    ])
    assert [f["role"] for f in folders] == ["inbox", "sent", "drafts", "trash", "junk"]


def test_parse_status_and_quota():
    assert parsers.parse_status(b'INBOX (MESSAGES 231 UNSEEN 4)') == {"messages": 231, "unseen": 4}
    assert parsers.parse_status((b'{11}', b'Sent (MESSAGES 3 UNSEEN 0)')) == {"messages": 3, "unseen": 0}
    assert parsers.parse_status(b"") == {}
    assert parsers.parse_quota([b'"User quota" (STORAGE 51200 1048576)']) == {
        "used_mb": 50.0, "limit_mb": 1024.0, "percent": 4.9}
    assert parsers.parse_quota([]) is None


def test_parse_fetch_shapes():
    data = [
        (b'1 (UID 101 FLAGS (\\Seen) INTERNALDATE "01-Nov-2026 09:15:02 +0200" RFC822.SIZE 1234 '
         b'BODY[HEADER.FIELDS (DATE FROM)] {20}', b"From: a@uth.gr\r\n\r\n"),
        b")",
        # Dovecot can put items after the literal
        (b"2 (BODY[] {5}", b"hello"),
        b" UID 102 FLAGS (\\Answered))",
        b"3 (UID 103 FLAGS ())",
        b"4 (FLAGS (\\Seen))",  # unsolicited update, no UID
    ]
    recs = parsers.parse_fetch(data)
    assert [r["uid"] for r in recs] == [101, 102, 103]
    assert recs[0]["flags"] == ["\\Seen"] and recs[0]["size"] == 1234
    assert recs[0]["internaldate"] == "01-Nov-2026 09:15:02 +0200"
    assert recs[1]["payload"] == b"hello" and recs[1]["flags"] == ["\\Answered"]
    assert recs[2]["payload"] is None and recs[2]["flags"] == []


def test_search_and_append_responses():
    assert parsers.parse_search([b"5 3 9"]) == [3, 5, 9]
    assert parsers.parse_search([b""]) == []
    assert parsers.parse_search([None]) == []
    assert parsers.append_uid([b"[APPENDUID 1730000000 3955] Append completed."]) == 3955
    assert parsers.append_uid([b"Append completed."]) is None


def local(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def test_dates():
    assert parsers.date_iso("Tue, 10 Nov 2026 16:30:00 +0200") == local(
        datetime(2026, 11, 10, 16, 30, tzinfo=timezone(timedelta(hours=2))))
    assert parsers.date_iso("not a date") is None
    assert parsers.date_iso(None) is None
    assert parsers.internaldate_iso(" 3-Nov-2026 08:05:00 -0500") == local(
        datetime(2026, 11, 3, 8, 5, tzinfo=timezone(timedelta(hours=-5))))
    assert parsers.internaldate_iso("3-Foo-2026 08:05:00 +0000") is None
    assert parsers.imap_date("2026-11-03") == "3-Nov-2026"
    with pytest.raises(ValueError):
        parsers.imap_date("3/11/2026")


def header_block(**fields) -> bytes:
    return ("".join(f"{k.replace('_', '-')}: {v}\r\n" for k, v in fields.items()) + "\r\n").encode()


def test_summarize_decodes_greek_headers():
    subject = Header("Εξέταση Δικτύων: αλλαγή αίθουσας", "utf-8").encode()
    sender = Header("Γιώργος Παπαδόπουλος", "iso-8859-7").encode()
    record = {
        "uid": 7, "flags": ["\\Flagged"], "size": 4321, "internaldate": "10-Nov-2026 10:00:00 +0200",
        "payload": header_block(
            Date="Tue, 10 Nov 2026 09:58:00 +0200", From=f"{sender} <gpapad@uth.gr>",
            To="ds-students@uth.gr", Cc="a@uth.gr, b@uth.gr", Subject=subject,
            Content_Type='multipart/mixed; boundary="xyz"'),
    }
    item = parsers.summarize(record)
    assert item["subject"] == "Εξέταση Δικτύων: αλλαγή αίθουσας"
    assert item["from"] == "Γιώργος Παπαδόπουλος <gpapad@uth.gr>"
    assert item["to"] == ["ds-students@uth.gr"] and item["cc"] == ["a@uth.gr", "b@uth.gr"]
    assert item["unread"] is True and item["flagged"] is True and item["answered"] is False
    assert item["date_iso"] == parsers.date_iso("Tue, 10 Nov 2026 09:58:00 +0200")


def test_attachment_flags_from_bodystructure():
    # shapes as Dovecot sends them: a mailing-list post (multipart/mixed with
    # text parts only), HTML with an inline image, a PDF, and a forwarded
    # message whose subject came as a literal
    data = [
        b'1 (UID 10 BODYSTRUCTURE ((("text" "plain" ("charset" "utf-8") NIL NIL "8bit" 120 4 NIL NIL NIL NIL)'
        b'("text" "html" ("charset" "utf-8") NIL NIL "8bit" 300 9 NIL NIL NIL NIL) "alternative")'
        b'("text" "plain" ("charset" "us-ascii") NIL NIL "7bit" 80 2 NIL ("inline" NIL) NIL NIL) "mixed"))',
        b'2 (UID 11 BODYSTRUCTURE (("text" "html" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("image" "png" ("name" "logo.png") "<logo>" NIL "base64" 900 NIL ("inline" ("filename" "logo.png")) NIL NIL)'
        b' "related"))',
        b'3 (UID 12 BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("application" "pdf" ("name" "x.pdf") NIL NIL "base64" 900 NIL ("attachment" ("filename" "x.pdf")) NIL NIL)'
        b' "mixed"))',
        (b'4 (UID 13 BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
         b'("message" "rfc822" NIL NIL NIL "7bit" 500 ("Mon, 9 Nov 2026" {12}', "Θέμα".encode() + b"\r\n"),
        b' NIL NIL NIL NIL NIL NIL NIL NIL) NIL 20 NIL NIL NIL NIL) "mixed"))',
        # Outlook: signature image with a Content-ID and no disposition
        b'5 (UID 14 BODYSTRUCTURE ((("text" "html" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("image" "png" ("name" "image001.png") "<image001.png@01DC>" NIL "base64" 38528 NIL NIL NIL NIL)'
        b' "related") "mixed"))',
        # a photo pasted inline, no Content-ID: a real file
        b'6 (UID 15 BODYSTRUCTURE (("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("image" "jpeg" ("name" "IMG_1.jpg") NIL NIL "base64" 900 NIL ("inline" ("filename" "IMG_1.jpg")) NIL NIL)'
        b' "mixed"))',
    ]
    assert parsers.attachment_flags(data) == {10: False, 11: False, 12: True, 13: True, 14: False, 15: True}


def test_raw_8bit_headers_are_decoded():
    # some senders put the bytes straight into the header, no encoded words
    for charset in ("utf-8", "windows-1253"):
        payload = (b"From: " + "Γραμματεία ΨΣ".encode(charset) + b" <gram@ds.uth.gr>\r\n"
                   b"Subject: " + "Ανακοίνωση εξετάσεων".encode(charset) + b"\r\n\r\n")
        item = parsers.summarize({"uid": 1, "flags": [], "size": 1, "internaldate": None, "payload": payload})
        assert item["from"] == "Γραμματεία ΨΣ <gram@ds.uth.gr>"
        assert item["subject"] == "Ανακοίνωση εξετάσεων"
    # folded in the middle of a UTF-8 character ("Προσωπικού", split inside the ρ)
    name = "ΔΣ Συλλόγου Διοικητικών Προσωπικού".encode()
    cut = name.index("ρ".encode()) + 1
    payload = b"From: " + name[:cut] + b"\r\n " + name[cut:] + b" <sdp-ds@uth.gr>\r\n\r\n"
    item = parsers.summarize({"uid": 2, "flags": [], "size": 1, "internaldate": None, "payload": payload})
    assert item["from"] == "ΔΣ Συλλόγου Διοικητικών Προσωπικού <sdp-ds@uth.gr>"


def test_encoded_words_that_split_a_character():
    # a real header from the university list server: the Θ of ΠΘ is cut in
    # two between the encoded words
    payload = (b"From: =?utf-8?b?zpTOoyDOo8+FzrvOu8+MzrPOv8+FIM6UzrnOv865zrrOt8+EzrnOus+Ozr0gzqDO?=\r\n"
               b"\t=?utf-8?b?mA==?= <sdp-ds@uth.gr>\r\n"
               b"Subject: =?utf-8?b?zpXOvs6t?=\r\n =?utf-8?b?z4TOsc+DzrcgzqDO?= =?utf-8?b?mA==?=\r\n\r\n")
    item = parsers.summarize({"uid": 3, "flags": [], "size": 1, "internaldate": None, "payload": payload})
    assert item["from"] == "ΔΣ Συλλόγου Διοικητικών ΠΘ <sdp-ds@uth.gr>"
    assert item["subject"] == "Εξέταση ΠΘ"


def test_json_safe_replaces_lone_surrogates():
    from uth_mail_mcp.server import json_safe
    assert json_safe({"a": ["ok\udcce", 1], "b": None}) == {"a": ["ok�", 1], "b": None}


def test_summarize_falls_back_to_internaldate_and_compacts():
    many = ", ".join(f"s{i}@uth.gr" for i in range(25))
    item = parsers.summarize({"uid": 1, "flags": ["\\Seen"], "size": 10, "internaldate": "01-Nov-2026 09:00:00 +0000",
                              "payload": header_block(From="x@uth.gr", To=many, Subject="hi")})
    assert item["date_iso"] == parsers.internaldate_iso("01-Nov-2026 09:00:00 +0000")
    out = parsers.compact(item)
    assert len(out["to"]) == parsers.MAX_LISTED_ADDRESSES + 1 and out["to"][-1] == "... and 15 more"
    assert "cc" not in out
    assert len(item["to"]) == 25  # matching still sees all of them


def test_search_criteria():
    keys, literal, local_filters = parsers.search_criteria(
        query="exam", sender="gpapad", subject="Lab 3", since="2026-11-01", unread_only=True)
    assert keys == ["UNSEEN", "SINCE", "1-Nov-2026", "FROM", '"gpapad"', "SUBJECT", '"Lab 3"', "TEXT", '"exam"']
    assert literal is None and local_filters == {}

    keys, literal, local_filters = parsers.search_criteria(query="εξεταστική", subject="Εργαστήριο")
    assert keys == []
    assert literal == ("TEXT", "εξεταστική")
    assert local_filters == {"subject": "εργαστηριο"}

    keys, literal, local_filters = parsers.search_criteria(sender='a"b', flagged_only=True, before="2026-12-31")
    assert keys == ["FLAGGED", "BEFORE", "31-Dec-2026", "FROM", '"a\\"b"']


def test_matches_is_case_and_accent_insensitive():
    item = {"from": "Γιώργος Παπαδόπουλος <gpapad@uth.gr>", "subject": "Εξέταση ΔΙΚΤΥΩΝ",
            "to": ["ds@uth.gr"], "cc": ["Μαρία Κ. <maria@uth.gr>"]}
    assert parsers.matches(item, {"subject": parsers.fold("εξεταση δικτυων")})
    assert parsers.matches(item, {"from": parsers.fold("ΓΙΩΡΓΟΣ")})
    assert parsers.matches(item, {"to": parsers.fold("μαρια")})
    assert not parsers.matches(item, {"subject": parsers.fold("εργασία")})
    assert parsers.matches(item, {})


def rich_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Γραμματεία ΨΣ <secretary@ds.uth.gr>"
    msg["To"] = "student@uth.gr"
    msg["Subject"] = "Πρόγραμμα εξεταστικής"
    msg["Date"] = "Mon, 09 Nov 2026 12:00:00 +0200"
    msg["Message-ID"] = "<abc@ds.uth.gr>"
    msg.set_content("Καλησπέρα,\nσας στέλνουμε το πρόγραμμα.\n")
    msg.add_alternative("<p>Καλησπέρα,</p><p>σας στέλνουμε το <b>πρόγραμμα</b>.</p>", subtype="html")
    html_part = msg.get_payload()[1]
    html_part.add_related(b"\x89PNG fake", maintype="image", subtype="png", cid="<logo>")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf",
                       filename="Πρόγραμμα Ιανουαρίου.pdf")
    forwarded = EmailMessage()
    forwarded["Subject"] = "Αρχική ανακοίνωση"
    forwarded["From"] = "prof@uth.gr"
    forwarded.set_content("παλιό κείμενο")
    msg.add_attachment(forwarded)
    return msg.as_bytes()


def test_message_details_body_and_attachments():
    info = parsers.message_details(rich_message())
    assert info["subject"] == "Πρόγραμμα εξεταστικής"
    assert info["from"] == "Γραμματεία ΨΣ <secretary@ds.uth.gr>"
    assert info["body_format"] == "plain"
    assert info["body"] == "Καλησπέρα,\nσας στέλνουμε το πρόγραμμα."
    assert info["message_id"] == "<abc@ds.uth.gr>"
    names = [(a["index"], a["filename"], a["content_type"]) for a in info["attachments"]]
    # the inline logo is part of the HTML, not an attachment
    assert names == [(1, "Πρόγραμμα Ιανουαρίου.pdf", "application/pdf"),
                     (2, "Αρχική ανακοίνωση.eml", "message/rfc822")]
    parts = parsers.attachments(parsers.parse_message(rich_message()))
    assert parts[0][1] == b"%PDF-1.4 fake"
    assert b"Subject:" in parts[1][1]


def test_outlook_signature_images_are_not_attachments():
    msg = EmailMessage()
    msg["Subject"] = "υπογραφή"
    msg.set_content("κείμενο")
    msg.add_alternative("<p>κείμενο</p><img src='cid:image001.png@01DC'>", subtype="html")
    html_part = msg.get_payload()[1]
    html_part.add_related(b"\x89PNG logo", maintype="image", subtype="png", cid="<image001.png@01DC>",
                          filename="image001.png")
    for part in msg.walk():  # Outlook sends these without a Content-Disposition
        if part.get_content_type() == "image/png":
            del part["Content-Disposition"]
            part.set_param("name", "image001.png")
    msg.add_attachment(b"\xff\xd8 photo", maintype="image", subtype="jpeg", filename="φωτογραφία.jpg",
                       disposition="inline")
    names = [a["filename"] for a in parsers.message_details(msg.as_bytes())["attachments"]]
    assert names == ["φωτογραφία.jpg"]


def test_html_only_message():
    msg = EmailMessage()
    msg["From"] = "noreply@eclass.uth.gr"
    msg["Subject"] = "Νέα ανακοίνωση"
    msg.set_content(
        "<html><head><style>p {color: red}</style></head><body>"
        "<p>Δείτε την <a href='https://eclass.uth.gr/courses/DS101/'>ανακοίνωση</a>.</p>"
        "Γραμμή 1<br>Γραμμή 2<script>alert(1)</script></body></html>", subtype="html")
    info = parsers.message_details(msg.as_bytes())
    assert info["body_format"] == "html"
    assert info["body"] == ("Δείτε την ανακοίνωση (https://eclass.uth.gr/courses/DS101/).\n"
                            "Γραμμή 1\nΓραμμή 2")
    assert info["attachments"] == []


def test_unknown_charset_still_reads():
    raw = (b"From: x@uth.gr\r\nSubject: test\r\nContent-Type: text/plain; charset=unknown-8bit\r\n"
           b"Content-Transfer-Encoding: 8bit\r\n\r\n" + "Καλημέρα".encode("windows-1253") + b"\r\n")
    assert parsers.message_details(raw)["body"] == "Καλημέρα"


def test_long_body_is_cut():
    msg = EmailMessage()
    msg["Subject"] = "long"
    msg.set_content("α" * 50)
    info = parsers.message_details(msg.as_bytes(), max_chars=10)
    assert info["body"] == "α" * 10 and info["body_truncated"] is True


def test_safe_filename():
    assert parsers.safe_filename('Εργασία 1: "τελική"?.pdf') == "Εργασία 1_ _τελική__.pdf"
    assert parsers.safe_filename("../../etc/passwd") == ".._.._etc_passwd"
    assert parsers.safe_filename("CON.txt") == "_CON.txt"
    assert parsers.safe_filename("  ...  ") == "attachment"
    long = parsers.safe_filename("x" * 300 + ".pdf")
    assert len(long) == 150 and long.endswith(".pdf")


def test_free_path(tmp_path):
    assert parsers.free_path(tmp_path, "a.pdf", b"one") == tmp_path / "a.pdf"
    (tmp_path / "a.pdf").write_bytes(b"one")
    assert parsers.free_path(tmp_path, "a.pdf", b"one") == tmp_path / "a.pdf"
    assert parsers.free_path(tmp_path, "a.pdf", b"two") == tmp_path / "a (2).pdf"


def test_clean_addresses():
    assert parsers.clean_addresses("a@uth.gr; Μαρία <m@uth.gr>, b@uth.gr") == [
        "a@uth.gr", "Μαρία <m@uth.gr>", "b@uth.gr"]
    assert parsers.clean_addresses(["a@uth.gr", "Β. Κ. <bk@uth.gr>"]) == ["a@uth.gr", "Β. Κ. <bk@uth.gr>"]
    assert parsers.clean_addresses(None) == []
    with pytest.raises(ValueError):
        parsers.clean_addresses("not-an-address")


def test_reply_subject():
    assert parsers.reply_subject("Εργασία 2") == "Re: Εργασία 2"
    assert parsers.reply_subject("Re: Εργασία 2") == "Re: Εργασία 2"
    assert parsers.reply_subject("ΑΠ: Εργασία 2") == "ΑΠ: Εργασία 2"
    assert parsers.reply_subject("") == "Re:"


def original_message(**extra) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Prof <prof@uth.gr>"
    msg["To"] = "student@uth.gr, friend@uth.gr"
    msg["Cc"] = "ta@uth.gr"
    msg["Subject"] = "Εργασία 2"
    msg["Date"] = "Mon, 09 Nov 2026 12:00:00 +0200"
    msg["Message-ID"] = "<m2@uth.gr>"
    msg["References"] = "<m1@uth.gr>"
    for k, v in extra.items():
        msg[k.replace("_", "-")] = v
    msg.set_content("Η προθεσμία είναι την Παρασκευή.\n\nΚαλή συνέχεια")
    return msg.as_bytes()


def test_reply_context():
    ctx = parsers.reply_context(original_message(), "student@uth.gr")
    assert ctx["to"] == ["Prof <prof@uth.gr>"] and ctx["cc"] == []
    assert ctx["subject"] == "Re: Εργασία 2"
    assert ctx["in_reply_to"] == "<m2@uth.gr>"
    assert ctx["references"] == "<m1@uth.gr> <m2@uth.gr>"
    assert "> Η προθεσμία είναι την Παρασκευή.\n>\n> Καλή συνέχεια" in ctx["quote"]
    assert ctx["quote"].startswith("-------- Αρχικό μήνυμα --------\nΑπό: Prof <prof@uth.gr>")

    everyone = parsers.reply_context(original_message(Reply_To="list@uth.gr"), "Student@uth.gr", reply_all=True)
    assert everyone["to"] == ["list@uth.gr"]
    assert everyone["cc"] == ["friend@uth.gr", "ta@uth.gr"]


def test_reply_to_own_sent_message_goes_to_its_recipients():
    msg = EmailMessage()
    msg["From"] = "student@uth.gr"
    msg["To"] = "prof@uth.gr"
    msg["Subject"] = "Ερώτηση"
    msg.set_content("γεια")
    assert parsers.reply_context(msg.as_bytes(), "student@uth.gr")["to"] == ["prof@uth.gr"]


def test_build_message():
    msg = parsers.build_message(
        "Γιώργος Παπαδόπουλος <student@uth.gr>", ["prof@uth.gr"], "Re: Εργασία 2", "Ευχαριστώ πολύ!",
        cc=["ta@uth.gr"], bcc=["me@uth.gr"], in_reply_to="<m2@uth.gr>", references="<m1@uth.gr> <m2@uth.gr>",
        files=[("λύση.pdf", b"%PDF", "application/pdf")])
    raw = msg.as_bytes()
    assert raw.isascii()  # nothing 8-bit leaves this machine
    back = parsers.parse_message(raw)
    assert str(back["subject"]) == "Re: Εργασία 2"
    assert back["in-reply-to"] == "<m2@uth.gr>" and back["references"] == "<m1@uth.gr> <m2@uth.gr>"
    assert back["message-id"].endswith("@uth.gr>")
    assert parsers.body_text(back) == ("Ευχαριστώ πολύ!", "plain")
    assert [a["filename"] for a in parsers.message_details(raw)["attachments"]] == ["λύση.pdf"]
    assert parsers.recipients_of(back) == ["prof@uth.gr", "ta@uth.gr", "me@uth.gr"]
