import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import comment_monitor
import send_comment_alerts


class FakeSmtp:
    def __init__(self):
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send_message(self, message):
        self.messages.append(message)


class MailPipelineTests(unittest.TestCase):
    def test_write_alerts_preserves_existing_pending_mail(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "comment_alert.json"
            queue.write_text(json.dumps({
                "emails": [{"id": "old", "to": "old@example.com",
                            "subject": "old", "body": "old"}],
                "unmapped": [],
            }), encoding="utf-8")
            item = {
                "platform": "bilibili", "platform_label": "B站",
                "account_name": "账号", "title": "内容", "url": "https://example.com",
                "comments": [{"comment_id": "1", "user": "用户", "content": "评论"}],
            }
            with mock.patch.object(comment_monitor, "ALERT_PATH", queue):
                emails, _ = comment_monitor.write_alerts([item])
            payload = json.loads(queue.read_text(encoding="utf-8"))
            self.assertEqual(1, len(emails))
            self.assertEqual(2, len(payload["emails"]))
            self.assertEqual("old", payload["emails"][0]["id"])

    def test_sender_removes_only_sent_mail_and_keeps_unmapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "comment_alert.json"
            queue.write_text(json.dumps({
                "emails": [{"id": "1", "to": "to@example.com",
                            "subject": "subject", "body": "body"}],
                "unmapped": [{"platform": "sohu"}],
            }), encoding="utf-8")
            fake = FakeSmtp()
            config = {"from_addr": "from@example.com"}
            with mock.patch.object(send_comment_alerts, "connect", return_value=fake):
                sent = send_comment_alerts.send_pending(queue, config)
            self.assertEqual(1, sent)
            self.assertEqual(1, len(fake.messages))
            payload = json.loads(queue.read_text(encoding="utf-8"))
            self.assertEqual([], payload["emails"])
            self.assertEqual([{"platform": "sohu"}], payload["unmapped"])

    def test_smtp_config_requires_credentials_as_a_pair(self):
        env = {
            "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "from@example.com",
            "SMTP_USERNAME": "user", "SMTP_PASSWORD": "",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            _config, problems = send_comment_alerts.smtp_config()
        self.assertIn("SMTP_USERNAME/SMTP_PASSWORD 必须同时配置", problems)

    def test_recipient_environment_overrides_file_and_can_enable_sohu(self):
        env = {
            "COMMENT_RECIPIENT_BILIBILI": "new@example.com",
            "COMMENT_OWNER_BILIBILI": "新负责人",
            "COMMENT_RECIPIENT_SOHU": "sohu@example.com",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            recipients, _fallback = comment_monitor.load_recipient_map()
        self.assertEqual("new@example.com", recipients["bilibili"]["email"])
        self.assertEqual("新负责人", recipients["bilibili"]["owner"])
        self.assertEqual("sohu@example.com", recipients["sohu"]["email"])

    def test_recipient_json_override_and_disable(self):
        env = {
            "COMMENT_RECIPIENTS_JSON": '{"douyin":"json@example.com"}',
            "COMMENT_RECIPIENT_CSDN": "disabled",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            recipients, _fallback = comment_monitor.load_recipient_map()
        self.assertEqual("json@example.com", recipients["douyin"]["email"])
        self.assertNotIn("csdn", recipients)

    def test_large_comment_alert_is_split_without_dropping_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "comment_alert.json"
            comments = [
                {"comment_id": str(index), "user": "用户", "content": f"评论{index}"}
                for index in range(5)
            ]
            item = {
                "platform": "bilibili", "platform_label": "B站",
                "account_name": "账号", "title": "内容", "url": "https://example.com",
                "comments": comments,
            }
            with mock.patch.object(comment_monitor, "ALERT_PATH", queue), \
                    mock.patch.dict(os.environ, {
                        "COMMENT_RECIPIENT_BILIBILI": "to@example.com",
                        "COMMENT_EMAIL_MAX_EVENTS": "2",
                    }, clear=True):
                emails, _ = comment_monitor.write_alerts([item])
            self.assertEqual(3, len(emails))
            delivered = [
                comment["comment_id"]
                for email in emails
                for new_item in email["new_items"]
                for comment in new_item["comments"]
            ]
            self.assertEqual(["0", "1", "2", "3", "4"], delivered)


if __name__ == "__main__":
    unittest.main()
