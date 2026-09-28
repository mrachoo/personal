import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from telegram import BotCommand, MenuButtonCommands, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.handlers import (
    callback_auth_gate,
    cmd_addadmin,
    cmd_address,
    cmd_approve,
    cmd_audit,
    cmd_balance,
    cmd_card,
    cmd_create,
    cmd_decline,
    cmd_credit,
    cmd_deactivate,
    cmd_debit,
    cmd_help,
    cmd_history,
    cmd_menu,
    cmd_msg,
    cmd_reactivate,
    cmd_removeadmin,
    cmd_rename,
    cmd_requests,
    cmd_resendinvite,
    cmd_setpassword,
    cmd_specialist,
    command_auth_gate,
    on_confirmation_callback,
    on_error,
    on_menu_callback,
    on_request_action,
    on_text_input,
    on_ui_callback,
)

logger = logging.getLogger("bot")

# Populates the "/" button beside the message box, so every command is one tap
# away instead of something you have to remember and type.
BOT_COMMANDS = [
    BotCommand("menu", "Open the admin menu"),
    BotCommand("create", "Create a user: username email first last password"),
    BotCommand("setpassword", "Set a user's password"),
    BotCommand("credit", "Add funds to a user"),
    BotCommand("debit", "Remove funds from a user"),
    BotCommand("balance", "Show a user's balance"),
    BotCommand("history", "Show a user's recent ledger entries"),
    BotCommand("requests", "List pending transfer / deposit requests"),
    BotCommand("card", "Show or start a card application"),
    BotCommand("msg", "Send a portal message to a user"),
    BotCommand("audit", "Recent ledger entries across all users"),
    BotCommand("help", "List every command"),
]


async def on_startup(application):
    """Register the command list and point the menu button at it."""
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        # A transient Telegram error here shouldn't stop the bot from polling.
        logger.warning("could not register bot commands", exc_info=True)


class Command(BaseCommand):
    help = "Run the Telegram bot worker (long-polling)."

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN is not set.")

        application = ApplicationBuilder().token(token).post_init(on_startup).build()

        # Auth gates run in group=-1, before every command / button press.
        application.add_handler(MessageHandler(filters.COMMAND, command_auth_gate), group=-1)
        application.add_handler(CallbackQueryHandler(callback_auth_gate), group=-1)

        application.add_handler(CommandHandler("create", cmd_create))
        application.add_handler(CommandHandler("setpassword", cmd_setpassword))
        application.add_handler(CommandHandler("credit", cmd_credit))
        application.add_handler(CommandHandler("debit", cmd_debit))
        application.add_handler(CommandHandler("balance", cmd_balance))
        application.add_handler(CommandHandler("history", cmd_history))
        application.add_handler(CommandHandler("rename", cmd_rename))
        application.add_handler(CommandHandler("deactivate", cmd_deactivate))
        application.add_handler(CommandHandler("reactivate", cmd_reactivate))
        application.add_handler(CommandHandler("resendinvite", cmd_resendinvite))
        application.add_handler(CommandHandler("addadmin", cmd_addadmin))
        application.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
        application.add_handler(CommandHandler("audit", cmd_audit))
        application.add_handler(CommandHandler("requests", cmd_requests))
        application.add_handler(CommandHandler("card", cmd_card))
        application.add_handler(CommandHandler("specialist", cmd_specialist))
        application.add_handler(CommandHandler("msg", cmd_msg))
        application.add_handler(CommandHandler("address", cmd_address))
        application.add_handler(CommandHandler("approve", cmd_approve))
        application.add_handler(CommandHandler("decline", cmd_decline))
        application.add_handler(CommandHandler("help", cmd_help))
        application.add_handler(CommandHandler("menu", cmd_menu))
        application.add_handler(CommandHandler("start", cmd_menu))

        application.add_handler(CallbackQueryHandler(on_confirmation_callback, pattern=r"^(confirm|cancel):"))
        application.add_handler(CallbackQueryHandler(on_menu_callback, pattern=r"^menu:"))
        application.add_handler(CallbackQueryHandler(on_request_action, pattern=r"^req:(approve|decline):"))
        application.add_handler(CallbackQueryHandler(on_ui_callback, pattern=r"^(usr|card|adm|flow):|^req:view:"))

        # Guided flows: a menu button arms a prompt, the admin's next plain-text
        # message supplies the values. Auth is checked inside the handler.
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_input))

        application.add_error_handler(on_error)

        self.stdout.write(self.style.SUCCESS("Bot starting (long-polling)..."))
        # Updates are processed one at a time (no concurrent_updates), which
        # serializes ledger writes per-process without extra locking.
        application.run_polling(allowed_updates=Update.ALL_TYPES)
