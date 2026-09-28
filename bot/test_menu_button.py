from unittest.mock import AsyncMock, MagicMock

from asgiref.sync import async_to_sync
from django.test import TestCase

from accounts.models import Admin, User
from bot.handlers import MENU_BUTTON_TEXT, PERSISTENT_MENU_KB, on_text_input

TG = 999002
GOOD_PW = "tr0ubador-quay"


def make(text, user_data):
    update = MagicMock()
    update.update_id = abs(hash(text)) % 100000
    update.effective_user.id = TG
    update.effective_message.text = text
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data
    return update, context


def sent(update):
    return [c.args[0] for c in update.effective_message.reply_text.call_args_list]


class MenuButtonTests(TestCase):
    def setUp(self):
        Admin.objects.create(telegram_id=TG, role=Admin.Role.OWNER)
        self.armed = {
            "flow": "create_password",
            "details": ["jsmith", "j@example.com", "Jane", "Smith"],
        }

    def test_is_persistent_and_resizes(self):
        self.assertTrue(PERSISTENT_MENU_KB.is_persistent)
        self.assertTrue(PERSISTENT_MENU_KB.resize_keyboard)
        self.assertEqual(PERSISTENT_MENU_KB.keyboard[0][0].text, MENU_BUTTON_TEXT)

    def test_tap_mid_flow_does_not_become_the_password(self):
        ud = {"pending_input": dict(self.armed)}
        update, context = make(MENU_BUTTON_TEXT, ud)
        async_to_sync(on_text_input)(update, context)
        self.assertFalse(User.objects.filter(username="jsmith").exists())
        self.assertNotIn("pending_input", ud)
        self.assertTrue(any("admin menu" in m for m in sent(update)))

    def test_real_password_still_creates_the_user(self):
        ud = {"pending_input": dict(self.armed)}
        update, context = make(GOOD_PW, ud)
        async_to_sync(on_text_input)(update, context)
        user = User.objects.get(username="jsmith")
        self.assertTrue(user.check_password(GOOD_PW))
        self.assertNotIn("pending_input", ud)

    def test_tap_with_no_flow_opens_menu(self):
        update, context = make(MENU_BUTTON_TEXT, {})
        async_to_sync(on_text_input)(update, context)
        self.assertTrue(any("admin menu" in m for m in sent(update)))

    def test_dock_is_announced_once_then_suppressed(self):
        ud = {}
        first, context = make(MENU_BUTTON_TEXT, ud)
        async_to_sync(on_text_input)(first, context)
        self.assertTrue(any("docked below" in m for m in sent(first)))
        self.assertTrue(ud["menu_docked"])

        second, context2 = make(MENU_BUTTON_TEXT, ud)
        async_to_sync(on_text_input)(second, context2)
        self.assertFalse(any("docked below" in m for m in sent(second)))

    def test_dock_attaches_the_persistent_keyboard(self):
        update, context = make(MENU_BUTTON_TEXT, {})
        async_to_sync(on_text_input)(update, context)
        markups = [c.kwargs.get("reply_markup") for c in update.effective_message.reply_text.call_args_list]
        self.assertIn(PERSISTENT_MENU_KB, markups)

    def test_whitespace_around_the_tap_is_tolerated(self):
        ud = {"pending_input": dict(self.armed)}
        update, context = make(f"  {MENU_BUTTON_TEXT} ", ud)
        async_to_sync(on_text_input)(update, context)
        self.assertFalse(User.objects.filter(username="jsmith").exists())
