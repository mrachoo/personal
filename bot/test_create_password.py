from django.test import TestCase

from accounts.models import Admin, User
from bot.handlers import _check_new_user_details, _create_core, _new_user_credentials

TG = 999001


class CreateWithPasswordTests(TestCase):
    def setUp(self):
        self.admin = Admin.objects.create(telegram_id=TG, role=Admin.Role.OWNER)

    def test_creates_user_with_usable_password(self):
        status, user = _create_core(TG, "Jsmith", "J.Smith@Example.com", "Jane", "Smith", "tr0ubador-quay")
        self.assertEqual(status, "ok")
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password("tr0ubador-quay"))
        self.assertEqual(user.username, "jsmith")
        self.assertEqual(user.email, "j.smith@example.com")
        self.assertEqual(user.created_by, self.admin)

    def test_no_invite_token_is_minted(self):
        _create_core(TG, "jsmith", "j@example.com", "Jane", "Smith", "tr0ubador-quay")
        self.assertEqual(User.objects.get(username="jsmith").invite_tokens.count(), 0)

    def test_weak_password_rejected_and_no_user_created(self):
        for pw in ("12345678", "password", "short", "jsmith123"):
            with self.subTest(pw=pw):
                status, msg = _create_core(TG, "jsmith", "j@example.com", "Jane", "Smith", pw)
                self.assertEqual(status, "weak_password", msg)
                self.assertFalse(User.objects.filter(username="jsmith").exists())

    def test_password_with_spaces_allowed(self):
        status, user = _create_core(TG, "jsmith", "j@example.com", "Jane", "Smith", "correct horse battery")
        self.assertEqual(status, "ok")
        self.assertTrue(user.check_password("correct horse battery"))

    def test_duplicate_username_and_email(self):
        _create_core(TG, "jsmith", "j@example.com", "Jane", "Smith", "tr0ubador-quay")
        self.assertEqual(_create_core(TG, "jsmith", "x@example.com", "J", "S", "tr0ubador-quay")[0], "username_taken")
        self.assertEqual(_create_core(TG, "other", "j@example.com", "J", "S", "tr0ubador-quay")[0], "email_taken")

    def test_preflight_matches_create(self):
        self.assertEqual(_check_new_user_details("jsmith", "j@example.com")[0], "ok")
        self.assertEqual(_check_new_user_details("A!", "j@example.com")[0], "invalid")
        _create_core(TG, "jsmith", "j@example.com", "Jane", "Smith", "tr0ubador-quay")
        self.assertEqual(_check_new_user_details("jsmith", "n@example.com")[0], "username_taken")
        self.assertEqual(_check_new_user_details("new", "j@example.com")[0], "email_taken")

    def test_credentials_message_carries_case_id_and_password(self):
        _, user = _create_core(TG, "jsmith", "j@example.com", "Jane", "Smith", "tr0ubador-quay")
        msg = _new_user_credentials(user, "tr0ubador-quay")
        self.assertIn(user.account_id, msg)
        self.assertIn(user.account_number, msg)
        self.assertIn("tr0ubador-quay", msg)
