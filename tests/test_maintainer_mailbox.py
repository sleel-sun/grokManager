import unittest

from app.maintainer.mailbox import wait_for_verification_code


class FakeMailResponse:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return {"results": self._rows}


class FakeMailSession:
    def __init__(self, rows):
        self._rows = rows

    def get(self, *args, **kwargs):
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


if __name__ == "__main__":
    unittest.main()
