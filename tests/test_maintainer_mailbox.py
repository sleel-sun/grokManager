import base64
import unittest

from app.maintainer.mailbox import (
    extract_verification_code,
    extract_verification_code_from_mail,
    fetch_emails,
    normalise_mail_rows,
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


class SequencedMailSession:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        payload = self._payloads.pop(0) if self._payloads else {"results": []}
        response = FakeMailResponse([])
        response.json = lambda: payload
        return response


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

    def test_wait_for_verification_code_accepts_reused_code_from_new_mail_id(self) -> None:
        session = FakeMailSession(
            [
                {
                    "id": "new-login-mail",
                    "to": [{"address": "target@example.com"}],
                    "raw": "To: target@example.com\nSubject: Your ChatGPT code\n\nChatGPT code: 123456",
                },
            ]
        )

        code = wait_for_verification_code(
            session=session,
            worker_domain="mail.example.com",
            cf_token="mailbox-token",
            target_email="target@example.com",
            timeout=0,
            ignore_codes={"123456"},
            ignore_ids={"old-registration-mail"},
        )

        self.assertEqual(code, "123456")

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

    def test_extract_verification_code_reads_chatgpt_login_code_label(self) -> None:
        content = """
To: target@example.com
Subject: Your ChatGPT code

Your ChatGPT login code is 492817.
"""

        self.assertEqual(extract_verification_code(content), "492817")

    def test_extract_verification_code_reads_code_is_label(self) -> None:
        content = """
To: target@example.com
Subject: OpenAI sign-in

Your code is 384920.
"""

        self.assertEqual(extract_verification_code(content), "384920")

    def test_extract_verification_code_reads_highlighted_html_code(self) -> None:
        content = """
Subject: Your ChatGPT code
<html>
  <body>
    <p style="margin:0;background-color: #F3F3F3; padding: 8px;">
      736251
    </p>
  </body>
</html>
"""

        self.assertEqual(extract_verification_code(content), "736251")

    def test_extract_verification_code_from_mail_reads_common_content_fields(self) -> None:
        code = extract_verification_code_from_mail(
            {
                "to": [{"address": "target@example.com"}],
                "subject": "Your OpenAI verification code",
                "text_content": "Use this code to continue: 918273",
                "html_content": "<p>Use this code to continue: <b>918273</b></p>",
            },
            target_email="target@example.com",
        )

        self.assertEqual(code, "918273")

    def test_extract_verification_code_falls_back_to_numeric_in_chatgpt_context(self) -> None:
        content = """
Subject: OpenAI verification

Use this to continue signing in:

619204
"""

        self.assertEqual(extract_verification_code(content), "619204")

    def test_extract_verification_code_does_not_read_plain_numeric_without_context(self) -> None:
        content = """
Subject: Monthly report

Your ticket number is 619204.
"""

        self.assertIsNone(extract_verification_code(content))

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

    def test_fetch_emails_falls_back_to_token_api_when_admin_filter_is_empty(self) -> None:
        session = SequencedMailSession(
            [
                {"results": []},
                {"messages": [{"id": "mail-2", "subject": "Your ChatGPT code"}]},
            ]
        )

        rows = fetch_emails(
            session=session,
            worker_domain="mail.example.com",
            cf_token="address-jwt",
            target_email="target@example.com",
            admin_password="worker-secret",
        )

        self.assertEqual(rows, [{"id": "mail-2", "subject": "Your ChatGPT code"}])
        self.assertEqual(
            session.urls,
            ["https://mail.example.com/admin/mails", "https://mail.example.com/api/mails"],
        )

    def test_normalise_mail_rows_accepts_common_mail_list_keys(self) -> None:
        self.assertEqual(normalise_mail_rows({"messages": [{"id": "mail-1"}]}), [{"id": "mail-1"}])
        self.assertEqual(normalise_mail_rows({"data": {"rows": [{"id": "mail-2"}]}}), [{"id": "mail-2"}])

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
