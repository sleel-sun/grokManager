import base64
import unittest

from app.maintainer.mailbox import (
    extract_verification_code,
    extract_verification_code_from_mail,
    fetch_emails,
    wait_for_verification_code,
)


class FakeMailResponse:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return {"results": self._rows}


class FakeMailSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_headers = None
        self.last_params = None
        self.last_url = None

    def get(self, url, **kwargs):
        self.last_url = url
        self.last_headers = kwargs.get("headers")
        self.last_params = kwargs.get("params")
        return FakeMailResponse(self._rows)


class MaintainerMailboxTests(unittest.TestCase):
    def test_wait_for_verification_code_uses_code_for_target_email(self) -> None:
        session = FakeMailSession(
            [
                {
                    "id": "other",
                    "to": [{"address": "other@example.com"}],
                    "raw": "To: other@example.com\nverification code: AAA111",
                },
                {
                    "id": "target",
                    "to": [{"address": "target@example.com"}],
                    "raw": "To: target@example.com\nverification code: BBB222",
                },
            ]
        )

        code = wait_for_verification_code(
            session=session,
            worker_domain="mail.example.com",
            cf_token="mailbox-token",
            target_email="target@example.com",
            timeout=0,
        )

        self.assertEqual(code, "BBB222")

    def test_extract_verification_code_ignores_mime_boundary_fragments(self) -> None:
        content = """
Content-Type: multipart/alternative; boundary="bound-mail-html"
To: target@example.com
Subject: Verify your email

--bound-mail-html
This message has not loaded the one-time security code yet.
--bound-mail-html--
"""

        self.assertIsNone(extract_verification_code(content))

    def test_extract_verification_code_reads_labeled_xai_code(self) -> None:
        content = """
To: target@example.com
Subject: Your xAI verification code

Your one-time security code is AB9-CD2.
"""

        self.assertEqual(extract_verification_code(content), "AB9-CD2")

    def test_fetch_emails_uses_admin_address_filter_when_available(self) -> None:
        session = FakeMailSession([{"id": "mail-1"}])

        rows = fetch_emails(
            session=session,
            worker_domain="mail.example.com",
            cf_token="address-jwt",
            target_email="target@example.com",
            admin_password="worker-secret",
        )

        self.assertEqual(rows, [{"id": "mail-1"}])
        self.assertEqual(session.last_url, "https://mail.example.com/admin/mails")
        self.assertEqual(session.last_params["address"], "target@example.com")
        self.assertEqual(session.last_headers["x-admin-auth"], "worker-secret")
        self.assertNotIn("Authorization", session.last_headers)

    def test_extract_verification_code_from_mail_decodes_raw_rfc822_body(self) -> None:
        body = "Your one-time security code is ZX9-QW2."
        raw = "\r\n".join(
            [
                "Subject: Verify your email",
                "To: target@example.com",
                "Content-Type: text/plain; charset=utf-8",
                "Content-Transfer-Encoding: base64",
                "",
                base64.b64encode(body.encode()).decode(),
            ]
        )

        code = extract_verification_code_from_mail(
            {"source": raw, "to": [{"address": "target@example.com"}]},
            target_email="target@example.com",
        )

        self.assertEqual(code, "ZX9-QW2")


if __name__ == "__main__":
    unittest.main()
