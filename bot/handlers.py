import logging

from asgiref.sync import sync_to_async
from decimal import Decimal

from django.db.models import Sum
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from accounts.amounts import parse_money
from accounts.emails import EmailSendError, send_invite_email
from accounts.models import (
    Admin,
    CardApplication,
    InviteToken,
    LedgerEntry,
    PortalMessage,
    ServiceRequest,
    User,
    username_validator,
)
from bot.auth import get_admin, is_owner
from bot.confirmations import (
    CONFIRMATION_TTL_SECONDS,
    LARGE_AMOUNT_THRESHOLD,
    create_pending,
    peek_pending,
    pop_pending,
)
from bot.idempotency import DUPLICATE, process_once
from bot.notify import notify_password_changed
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.utils import timezone

logger = logging.getLogger("bot")

MAX_LIST_N = 100
DEFAULT_HISTORY_N = 10
DEFAULT_AUDIT_N = 20

COMMON_HELP = """\
/menu - interactive button menu for everything below
/create <username> <email> <first_name> <last_name> - create a user and send an invite email
/setpassword <username> <new_password> - set a user's password (only the admin who created them)
/credit <username> <amount> <reason...> - add funds
/debit <username> <amount> <reason...> - remove funds
/balance <username> - show current balance
/history <username> [n] - show last n ledger entries (default 10)
/rename <username> <new_username> - rename a user
/deactivate <username> - block a user from logging in
/reactivate <username> - re-allow a user to log in
/resendinvite <username> - send a new invite email
/audit [n] - show last n ledger entries across all users (default 20)
/requests [n] - list latest portal transfer/deposit requests (default 10)
/approve <ref> - approve a pending request (marker only - move credits with /credit or /debit)
/decline <ref> - decline a pending request
/card <username> - show a user's card application progress
/card <username> start - open a card application
/card <username> <stage> <state> - update a stage (see /card for stages)
/specialist - view your specialist profile (shown on your users' dashboards)
/specialist name|email|phone|avatar <value> - update your specialist profile
/msg <username> <text...> - send a message to a user's portal Messages page
/address <username> [address | clear] - view or set a user's card delivery address
/help - show this message"""

OWNER_HELP = """\
/addadmin <telegram_id> [name] - authorize a new admin
/removeadmin <telegram_id> - revoke an admin"""


# --- shared helpers ----------------------------------------------------------


def find_user(username):
    return User.objects.filter(username=username.strip().lower()).first()


def get_balance(user):
    return user.ledger_entries.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def describe_admin(telegram_id):
    admin = Admin.objects.filter(telegram_id=telegram_id).first()
    if admin is None:
        return f"{telegram_id} (removed admin)"
    if admin.display_name:
        return f"{admin.display_name} ({telegram_id})"
    return str(telegram_id)


async def reply(update, text, **kwargs):
    await update.effective_message.reply_text(text, **kwargs)


# --- auth gates (group=-1, run before every command / callback) ------------


async def command_auth_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    admin = await sync_to_async(get_admin, thread_sensitive=True)(telegram_id)
    if admin is None:
        logger.warning(
            "unauthorized command from telegram_id=%s text=%r",
            telegram_id,
            update.effective_message.text if update.effective_message else "",
        )
        await reply(
            update,
            f"Not authorized.\n\nYour Telegram ID is {telegram_id} — if you're "
            "meant to have access, send that number to the portal owner.",
        )
        raise ApplicationHandlerStop
    context.user_data["admin"] = admin
    logger.info(
        "admin=%s dispatch: %s",
        telegram_id,
        update.effective_message.text if update.effective_message else "",
    )


async def callback_auth_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    admin = await sync_to_async(get_admin, thread_sensitive=True)(telegram_id)
    if admin is None:
        await update.callback_query.answer("Not authorized.", show_alert=True)
        logger.warning("unauthorized callback from telegram_id=%s", telegram_id)
        raise ApplicationHandlerStop
    context.user_data["admin"] = admin


# --- /create -----------------------------------------------------------------


def _create_core(admin_telegram_id, username, email, first_name, last_name):
    username = username.strip().lower()
    email = email.strip().lower()

    try:
        username_validator(username)
    except ValidationError as exc:
        return ("invalid", str(exc.messages[0]))

    if User.objects.filter(username=username).exists():
        return ("username_taken", username)
    if User.objects.filter(email=email).exists():
        return ("email_taken", email)

    creating_admin = Admin.objects.filter(telegram_id=admin_telegram_id).first()
    user = User(
        username=username,
        email=email,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        created_by=creating_admin,
    )
    user.set_unusable_password()
    try:
        user.full_clean()
    except ValidationError as exc:
        return ("invalid", "; ".join(sum(exc.message_dict.values(), [])))
    user.save()
    token = InviteToken.objects.create(user=user)
    return ("ok", (user, token))


async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if len(args) != 4:
        await reply(update, "Usage: /create <username> <email> <first_name> <last_name>")
        return

    username, email, first_name, last_name = args
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _create_core, admin.telegram_id, username, email, first_name, last_name
    )
    if result is DUPLICATE:
        return

    status, payload = result
    if status in ("username_taken", "email_taken", "invalid"):
        logger.info("admin=%s create failed: %s (%s)", admin.telegram_id, status, payload)
    if status == "username_taken":
        await reply(update, f"Username '{payload}' is already taken.")
        return
    if status == "email_taken":
        await reply(update, f"Email '{payload}' is already registered.")
        return
    if status == "invalid":
        await reply(update, f"Could not create user: {payload}")
        return

    user, token = payload
    logger.info("admin=%s created user=%s", admin.telegram_id, user.username)
    await reply(
        update,
        f"Created user '{user.username}' ({user.email}).\n"
        f"Case ID: {user.account_id}\n"
        f"Account number: {user.account_number}",
    )
    try:
        await sync_to_async(send_invite_email, thread_sensitive=True)(user, token.token)
        await reply(update, "Invite email sent.")
    except EmailSendError as exc:
        await reply(
            update,
            f"Warning: invite email could not be sent ({exc}). "
            f"Use /resendinvite {user.username} to retry once fixed.",
        )


# --- /setpassword --------------------------------------------------------------


def _setpassword_core(admin_telegram_id, username, new_password):
    user = find_user(username)
    # A non-creator gets the same "not_found" response as a truly missing
    # username, so /setpassword can't be used to probe which usernames exist
    # under other admins (mirrors the staff-admin queryset scoping).
    if user is None or user.created_by is None or user.created_by.telegram_id != admin_telegram_id:
        return ("not_found", username)
    try:
        password_validation.validate_password(new_password, user)
    except ValidationError as exc:
        return ("invalid", "; ".join(exc.messages))
    user.set_password(new_password)
    user.save(update_fields=["password"])
    return ("ok", user)


async def cmd_setpassword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if len(args) != 2:
        await reply(update, "Usage: /setpassword <username> <new_password>")
        return
    username, new_password = args
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _setpassword_core, admin.telegram_id, username, new_password
    )
    if result is DUPLICATE:
        return
    status, payload = result
    if status in ("not_found", "invalid"):
        logger.info("admin=%s setpassword failed: %s (%s)", admin.telegram_id, status, payload)
    if status == "not_found":
        await reply(update, f"No such user '{payload}'.")
        return
    if status == "invalid":
        await reply(update, f"Could not set password: {payload}")
        return
    user = payload
    logger.info("admin=%s set password for user=%s", admin.telegram_id, user.username)
    await reply(update, f"Password set for '{user.username}'.")
    await sync_to_async(notify_password_changed, thread_sensitive=True)(
        user, "set by the admin who created this account, via /setpassword"
    )


# --- /requests, /approve, /decline ---------------------------------------------


def _format_request_line(sr):
    if sr.kind == ServiceRequest.Kind.TRANSFER:
        target = f"-> acct {sr.recipient}"
        if sr.recipient_bank:
            target += f" at {sr.recipient_bank}"
        elif sr.routing_number:
            target += f" (routing {sr.routing_number})"
        match = User.objects.filter(account_number=sr.recipient).first()
        if match is not None:
            target += f" ({match.username})"
    else:
        target = f"from {sr.platform}"
        if sr.external_ref:
            target += f" (ref {sr.external_ref})"
    line = (
        f"{sr.reference} [{sr.status}] {sr.get_kind_display().lower()}: "
        f"${sr.amount:,} by {sr.user.username} {target}"
    )
    if sr.note:
        line += f' - "{sr.note}"'
    return line


def _list_requests(n):
    return [
        _format_request_line(sr)
        for sr in ServiceRequest.objects.select_related("user")[:n]
    ]


async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = DEFAULT_HISTORY_N
    if context.args:
        try:
            n = min(int(context.args[0]), MAX_LIST_N)
        except ValueError:
            await reply(update, "Usage: /requests [n]")
            return
    lines = await sync_to_async(_list_requests, thread_sensitive=True)(n)
    if not lines:
        await reply(update, "No portal requests yet.")
        return
    await reply(update, "\n".join(lines))


def _decide_request_core(admin_telegram_id, reference, status):
    sr = ServiceRequest.objects.select_related("user").filter(reference=reference.upper()).first()
    if sr is None:
        return ("not_found", reference)
    if sr.status != ServiceRequest.Status.PENDING:
        return ("already_decided", sr)

    # Approving a transfer debits the sender in the same transaction as the
    # status change, so a request can never read "approved" without its
    # matching ledger entry existing.
    entry = None
    if status == ServiceRequest.Status.APPROVED and sr.kind == ServiceRequest.Kind.TRANSFER:
        user = User.objects.select_for_update().get(id=sr.user_id)
        balance = get_balance(user)
        if sr.amount > balance:
            return ("insufficient", (sr, balance))
        reason = f"Transfer {sr.reference} to account {sr.recipient}"
        if sr.recipient_bank:
            reason += f" ({sr.recipient_bank})"
        if sr.note:
            reason += f' - "{sr.note}"'
        entry = LedgerEntry.objects.create(
            user=user, amount=-sr.amount, reason=reason, authorized_by=admin_telegram_id
        )

    sr.status = status
    sr.processed_at = timezone.now()
    sr.processed_by = admin_telegram_id
    sr.save(update_fields=["status", "processed_at", "processed_by"])
    new_balance = get_balance(sr.user) if entry is not None else None
    return ("ok", (sr, _format_request_line(sr), new_balance))


async def _cmd_decide_request(update, context, status, usage):
    admin = context.user_data["admin"]
    if len(context.args) != 1:
        await reply(update, usage)
        return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _decide_request_core, admin.telegram_id, context.args[0], status
    )
    if result is DUPLICATE:
        return
    outcome, payload = result
    if outcome == "not_found":
        logger.info("admin=%s decide-request failed: no request '%s'", admin.telegram_id, payload)
        await reply(update, f"No request '{payload}'.")
        return
    if outcome == "already_decided":
        await reply(update, f"{payload.reference} was already {payload.status}.")
        return
    if outcome == "insufficient":
        sr, balance = payload
        logger.info(
            "admin=%s approve blocked: %s needs $%s, balance $%s",
            admin.telegram_id, sr.reference, sr.amount, balance,
        )
        await reply(
            update,
            f"Not approved — '{sr.user.username}' has ${balance:,} but {sr.reference} "
            f"is for ${sr.amount:,}.\nCredit the account first, or /decline {sr.reference}.",
        )
        return
    sr, line, new_balance = payload
    logger.info("admin=%s marked request %s %s", admin.telegram_id, sr.reference, sr.status)
    text = line
    if new_balance is not None:
        text += (
            f"\nDebited ${sr.amount:,} from '{sr.user.username}'. "
            f"New balance: ${new_balance:,}."
        )
    elif status == ServiceRequest.Status.APPROVED:
        text += (
            "\nNote: this is a status change only - add the funds with /credit."
        )
    await reply(update, text)


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_decide_request(
        update, context, ServiceRequest.Status.APPROVED, "Usage: /approve <ref>"
    )


async def cmd_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_decide_request(
        update, context, ServiceRequest.Status.DECLINED, "Usage: /decline <ref>"
    )


# --- /specialist ---------------------------------------------------------------

SPECIALIST_USAGE = (
    "Usage:\n"
    "/specialist - show your profile\n"
    "/specialist name <first> <last>\n"
    "/specialist email <address>\n"
    "/specialist phone <number>\n"
    "/specialist avatar <woman|man>\n"
    "This profile appears as \"Admin Specialist\" on the dashboard of every user you create."
)


def _format_specialist(admin):
    name = f"{admin.first_name} {admin.last_name}".strip() or "(not set)"
    return (
        f"Your specialist profile:\n"
        f"  name: {name}\n"
        f"  email: {admin.contact_email or '(not set)'}\n"
        f"  phone: {admin.contact_phone or '(not set)'}\n"
        f"  avatar: {admin.avatar}"
    )


def _specialist_set_core(telegram_id, field, args):
    admin = Admin.objects.get(telegram_id=telegram_id)
    if field == "name":
        if len(args) < 2:
            return ("invalid", "Provide a first and last name: /specialist name <first> <last>")
        admin.first_name, admin.last_name = args[0], " ".join(args[1:])
        admin.save(update_fields=["first_name", "last_name"])
    elif field == "email":
        if len(args) != 1 or "@" not in args[0]:
            return ("invalid", "Provide a valid email: /specialist email <address>")
        admin.contact_email = args[0]
        admin.save(update_fields=["contact_email"])
    elif field == "phone":
        phone = " ".join(args)
        if not phone or len(phone) > 32 or not all(c.isdigit() or c in "+-() ." for c in phone):
            return ("invalid", "Provide a phone number: /specialist phone <number>")
        admin.contact_phone = phone
        admin.save(update_fields=["contact_phone"])
    elif field == "avatar":
        if len(args) != 1 or args[0].lower() not in Admin.Avatar.values:
            return ("invalid", "Choose an avatar: /specialist avatar <woman|man>")
        admin.avatar = args[0].lower()
        admin.save(update_fields=["avatar"])
    else:
        return ("invalid", SPECIALIST_USAGE)
    return ("ok", _format_specialist(admin))


async def cmd_specialist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if not args:
        text = await sync_to_async(
            lambda: _format_specialist(Admin.objects.get(telegram_id=admin.telegram_id)),
            thread_sensitive=True,
        )()
        await reply(update, f"{text}\n\n{SPECIALIST_USAGE}")
        return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _specialist_set_core, admin.telegram_id, args[0].lower(), args[1:]
    )
    if result is DUPLICATE:
        return
    status, payload = result
    if status == "invalid":
        await reply(update, payload)
        return
    logger.info("admin=%s updated specialist profile field=%s", admin.telegram_id, args[0].lower())
    await reply(update, payload)


# --- /address ------------------------------------------------------------------

ADDRESS_USAGE = (
    "Usage:\n"
    "/address <username> - show their delivery address\n"
    "/address <username> clear - remove it\n"
    "/address <username> <street>, [apt/suite,] <city>, <state> <zip>\n"
    "Example: /address janedoe 123 Main St, Apt 4B, Springfield, IL 62704"
)

ADDRESS_FIELDS = ["address_line1", "address_line2", "city", "state", "postal_code"]


def parse_address(text):
    """Parse 'street[, unit], city, state zip' into a field dict, or None."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 3:
        return None
    state_zip = parts[-1].split()
    if len(state_zip) < 2:
        return None
    return {
        "address_line1": parts[0][:128],
        "address_line2": ", ".join(parts[1:-2])[:128],
        "city": parts[-2][:64],
        "state": " ".join(state_zip[:-1])[:32],
        "postal_code": state_zip[-1][:16],
    }


def _format_address(user):
    lines = user.address_lines()
    return "\n".join(lines) if lines else "(no delivery address on file)"


def _address_set_core(username, fields):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    for f in ADDRESS_FIELDS:
        setattr(user, f, fields.get(f, "") if fields else "")
    user.save(update_fields=ADDRESS_FIELDS)
    return ("ok", (user.username, _format_address(user)))


async def cmd_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if not args:
        await reply(update, ADDRESS_USAGE)
        return
    username = args[0]

    if len(args) == 1:
        user = await sync_to_async(find_user, thread_sensitive=True)(username)
        if user is None:
            await reply(update, f"No such user '{username}'.")
            return
        text = await sync_to_async(_format_address, thread_sensitive=True)(user)
        await reply(update, f"Delivery address for '{user.username}':\n{text}")
        return

    if len(args) == 2 and args[1].lower() == "clear":
        fields = None
    else:
        fields = parse_address(" ".join(args[1:]))
        if fields is None:
            await reply(update, f"Couldn't parse that address.\n{ADDRESS_USAGE}")
            return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _address_set_core, username, fields
    )
    if result is DUPLICATE:
        return
    status, payload = result
    if status == "not_found":
        await reply(update, f"No such user '{payload}'.")
        return
    uname, text = payload
    logger.info("admin=%s set address for user=%s", admin.telegram_id, uname)
    await reply(update, f"Delivery address for '{uname}':\n{text}")


# --- /msg ----------------------------------------------------------------------


def _msg_core(admin_telegram_id, username, body):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    PortalMessage.objects.create(
        user=user,
        sender=PortalMessage.Sender.ADMIN,
        body=body[:2000],
        sent_by_admin=admin_telegram_id,
    )
    return ("ok", user.username)


async def cmd_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if len(args) < 2:
        await reply(update, "Usage: /msg <username> <text...>")
        return
    username, body = args[0], " ".join(args[1:])
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _msg_core, admin.telegram_id, username, body
    )
    if result is DUPLICATE:
        return
    status, payload = result
    if status == "not_found":
        await reply(update, f"No such user '{payload}'.")
        return
    logger.info("admin=%s messaged user=%s", admin.telegram_id, payload)
    await reply(update, f"Sent — '{payload}' will see it on their Messages page.")


# --- /card ---------------------------------------------------------------------

CARD_STAGES = {
    "docs": ("docs_status", CardApplication.DocsStatus),
    "review": ("review_status", CardApplication.ReviewStatus),
    "result": ("result_status", CardApplication.ResultStatus),
    "production": ("production_status", CardApplication.ProductionStatus),
    "shipping": ("shipping_status", CardApplication.ShippingStatus),
}

CARD_USAGE = (
    "Usage:\n"
    "/card <username> - show progress\n"
    "/card <username> start - open an application\n"
    "/card <username> <stage> <state> [tracking]\n"
    "Stages: docs(received|missing), review(in_review|reviewed),\n"
    "result(pending|verified|resubmit|info_needed),\n"
    "production(waiting|in_progress|ready), shipping(pending|shipped [tracking])"
)


def _format_card(app):
    lines = [f"Card application for '{app.user.username}':"]
    for key, (field, _) in CARD_STAGES.items():
        lines.append(f"  {key}: {getattr(app, f'get_{field}_display')()}")
    if app.tracking_number:
        lines.append(f"  tracking: {app.tracking_number}")
    return "\n".join(lines)


def _card_show_core(username):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    app = CardApplication.objects.filter(user=user).first()
    if app is None:
        return ("no_app", user.username)
    return ("ok", _format_card(app))


def _card_start_core(username):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    app, created = CardApplication.objects.get_or_create(user=user)
    if not created:
        return ("exists", _format_card(app))
    return ("ok", _format_card(app))


def _card_set_core(username, stage, state, tracking):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    app = CardApplication.objects.filter(user=user).first()
    if app is None:
        return ("no_app", user.username)
    if stage not in CARD_STAGES:
        return ("bad_stage", stage)
    field, choices = CARD_STAGES[stage]
    if state not in choices.values:
        return ("bad_state", f"{stage}: {', '.join(choices.values)}")
    setattr(app, field, state)
    update_fields = [field, "updated_at"]
    if tracking is not None:
        app.tracking_number = tracking
        update_fields.append("tracking_number")
    app.save(update_fields=update_fields)
    return ("ok", _format_card(app))


async def cmd_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if not args:
        await reply(update, CARD_USAGE)
        return
    username = args[0]

    if len(args) == 1:
        result = await sync_to_async(_card_show_core, thread_sensitive=True)(username)
    elif len(args) == 2 and args[1].lower() == "start":
        result = await sync_to_async(process_once, thread_sensitive=True)(
            update.update_id, _card_start_core, username
        )
    elif len(args) in (3, 4):
        tracking = args[3] if len(args) == 4 else None
        result = await sync_to_async(process_once, thread_sensitive=True)(
            update.update_id, _card_set_core, username, args[1].lower(), args[2].lower(), tracking
        )
    else:
        await reply(update, CARD_USAGE)
        return

    if result is DUPLICATE:
        return
    status, payload = result
    if status == "not_found":
        await reply(update, f"No such user '{payload}'.")
        return
    if status == "no_app":
        await reply(update, f"'{payload}' has no card application. Open one with /card {payload} start.")
        return
    if status == "bad_stage":
        await reply(update, f"Unknown stage '{payload}'.\n{CARD_USAGE}")
        return
    if status == "bad_state":
        await reply(update, f"Invalid state. Valid states for {payload}")
        return
    if status == "exists":
        await reply(update, f"Application already exists.\n{payload}")
        return
    logger.info("admin=%s card command for args=%s", admin.telegram_id, args)
    await reply(update, payload)


# --- /credit and /debit -------------------------------------------------------


def _write_ledger(admin_telegram_id, user_id, signed_amount, reason):
    user = User.objects.select_for_update().get(id=user_id)
    LedgerEntry.objects.create(
        user=user, amount=signed_amount, reason=reason, authorized_by=admin_telegram_id
    )
    return ("ok", user.username, signed_amount, get_balance(user))


async def request_confirmation(update, context, admin, summary, fn, args):
    token = create_pending(admin.telegram_id, fn, args, summary)
    logger.info("admin=%s confirmation requested: %s", admin.telegram_id, summary)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"confirm:{token}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{token}"),
            ]
        ]
    )
    await reply(
        update,
        f"{summary}\n\nConfirm within {CONFIRMATION_TTL_SECONDS}s.",
        reply_markup=keyboard,
    )
    if context.job_queue:
        context.job_queue.run_once(
            expire_confirmation_job,
            when=CONFIRMATION_TTL_SECONDS,
            data={"token": token, "summary": summary},
            chat_id=update.effective_chat.id,
        )


async def expire_confirmation_job(context: ContextTypes.DEFAULT_TYPE):
    token = context.job.data["token"]
    summary = context.job.data["summary"]
    if peek_pending(token) is None:
        return
    pop_pending(token)
    logger.info("confirmation expired: %s", summary)
    await context.bot.send_message(
        chat_id=context.job.chat_id, text=f"Confirmation expired: {summary}"
    )


async def _parse_amount_command(update, context, usage):
    args = context.args
    if len(args) < 3:
        await reply(update, usage)
        return None
    username = args[0]
    amount = parse_money(args[1])
    if amount is None:
        await reply(update, "Amount must be a positive number, e.g. 500 or 500.43.")
        return None
    reason = " ".join(args[2:])

    user = await sync_to_async(find_user, thread_sensitive=True)(username)
    if user is None:
        admin = context.user_data.get("admin")
        logger.info(
            "admin=%s command failed: no such user '%s'",
            admin.telegram_id if admin else "?", username,
        )
        await reply(update, f"No such user '{username}'.")
        return None
    return user, amount, reason


async def _execute_credit(update, context, admin, user, amount, reason):
    if amount >= LARGE_AMOUNT_THRESHOLD:
        summary = f"Credit ${amount:,} to '{user.username}' for: {reason}"
        await request_confirmation(
            update, context, admin, summary, _write_ledger, (admin.telegram_id, user.id, amount, reason)
        )
        return

    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _write_ledger, admin.telegram_id, user.id, amount, reason
    )
    if result is DUPLICATE:
        return
    logger.info("admin=%s credited user=%s amount=%s", admin.telegram_id, user.username, amount)
    _, username, signed_amount, balance = result
    await reply(update, f"{username}: {signed_amount:+,.2f}. New balance: ${balance:,}.")


async def _execute_debit(update, context, admin, user, amount, reason):
    current_balance = await sync_to_async(get_balance, thread_sensitive=True)(user)
    prospective = current_balance - amount

    warnings = []
    if amount >= LARGE_AMOUNT_THRESHOLD:
        warnings.append(f"large debit (>= {LARGE_AMOUNT_THRESHOLD})")
    if prospective < 0:
        warnings.append(f"balance would go negative (new balance would be {prospective})")

    if warnings:
        summary = f"Debit ${amount:,} from '{user.username}' for: {reason}\nWarning: {'; '.join(warnings)}."
        await request_confirmation(
            update, context, admin, summary, _write_ledger, (admin.telegram_id, user.id, -amount, reason)
        )
        return

    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _write_ledger, admin.telegram_id, user.id, -amount, reason
    )
    if result is DUPLICATE:
        return
    logger.info("admin=%s debited user=%s amount=%s", admin.telegram_id, user.username, amount)
    _, username, signed_amount, balance = result
    await reply(update, f"{username}: {signed_amount:+,.2f}. New balance: ${balance:,}.")


async def cmd_credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    parsed = await _parse_amount_command(update, context, "Usage: /credit <username> <amount> <reason...>")
    if parsed is None:
        return
    user, amount, reason = parsed
    await _execute_credit(update, context, admin, user, amount, reason)


async def cmd_debit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    parsed = await _parse_amount_command(update, context, "Usage: /debit <username> <amount> <reason...>")
    if parsed is None:
        return
    user, amount, reason = parsed
    await _execute_debit(update, context, admin, user, amount, reason)


async def on_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, _, token = query.data.partition(":")
    telegram_id = query.from_user.id

    entry = peek_pending(token)
    if entry is None:
        logger.info("telegram_id=%s %s on expired/unknown confirmation token", telegram_id, action)
        await query.answer("This confirmation has expired or was already used.", show_alert=True)
        return
    if telegram_id != entry["admin_id"]:
        logger.warning(
            "telegram_id=%s tried to %s another admin's confirmation: %s",
            telegram_id, action, entry["summary"],
        )
        await query.answer("Only the admin who started this can confirm it.", show_alert=True)
        return

    if action == "cancel":
        pop_pending(token)
        await query.answer("Cancelled.")
        await query.edit_message_text(f"Cancelled: {entry['summary']}")
        logger.info("admin=%s cancelled confirmation: %s", telegram_id, entry["summary"])
        return

    if action == "confirm":
        pop_pending(token)
        fn, fn_args = entry["fn"], entry["args"]
        result = await sync_to_async(process_once, thread_sensitive=True)(
            update.update_id, fn, *fn_args
        )
        await query.answer("Confirmed.")
        if result is DUPLICATE:
            return
        _, username, signed_amount, balance = result
        logger.info(
            "admin=%s confirmed: %s -> %s: %+d, new balance %s",
            telegram_id, entry["summary"], username, signed_amount, balance,
        )
        await query.edit_message_text(
            f"{entry['summary']}\n\nConfirmed. {username}: {signed_amount:+,.2f}. New balance: ${balance:,}."
        )


# --- /balance, /history, /audit (read-only, no dedupe needed) ---------------


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await reply(update, "Usage: /balance <username>")
        return
    user = await sync_to_async(find_user, thread_sensitive=True)(args[0])
    if user is None:
        await reply(update, f"No such user '{args[0]}'.")
        return
    balance = await sync_to_async(get_balance, thread_sensitive=True)(user)
    await reply(update, f"{user.username}: ${balance:,}")


def _history_rows(username, n):
    user = find_user(username)
    if user is None:
        return None
    entries = list(user.ledger_entries.all()[:n])
    return user, entries


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) not in (1, 2):
        await reply(update, "Usage: /history <username> [n]")
        return
    username = args[0]
    n = DEFAULT_HISTORY_N
    note = ""
    if len(args) == 2:
        try:
            n = int(args[1])
        except ValueError:
            await reply(update, "n must be an integer.")
            return
        if n > MAX_LIST_N:
            n = MAX_LIST_N
            note = f" (capped at {MAX_LIST_N})"
        if n <= 0:
            await reply(update, "n must be a positive integer.")
            return

    result = await sync_to_async(_history_rows, thread_sensitive=True)(username, n)
    if result is None:
        await reply(update, f"No such user '{username}'.")
        return
    user, entries = result
    if not entries:
        await reply(update, f"No ledger entries for '{user.username}'.")
        return

    lines = [f"Last {len(entries)} entries for '{user.username}'{note}:"]
    for e in entries:
        admin_desc = await sync_to_async(describe_admin, thread_sensitive=True)(e.authorized_by)
        lines.append(
            f"{e.created_at:%Y-%m-%d %H:%M} {e.amount:+,.2f} - {e.reason} (by {admin_desc})"
        )
    await reply(update, "\n".join(lines))


def _audit_rows(n):
    return list(LedgerEntry.objects.select_related("user").all()[:n])


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    n = DEFAULT_AUDIT_N
    note = ""
    if len(args) == 1:
        try:
            n = int(args[0])
        except ValueError:
            await reply(update, "n must be an integer.")
            return
        if n > MAX_LIST_N:
            n = MAX_LIST_N
            note = f" (capped at {MAX_LIST_N})"
        if n <= 0:
            await reply(update, "n must be a positive integer.")
            return
    elif len(args) > 1:
        await reply(update, "Usage: /audit [n]")
        return

    entries = await sync_to_async(_audit_rows, thread_sensitive=True)(n)
    if not entries:
        await reply(update, "No ledger entries yet.")
        return

    lines = [f"Last {len(entries)} ledger entries{note}:"]
    for e in entries:
        admin_desc = await sync_to_async(describe_admin, thread_sensitive=True)(e.authorized_by)
        lines.append(
            f"{e.created_at:%Y-%m-%d %H:%M} {e.user.username}: {e.amount:+,.2f} - {e.reason} (by {admin_desc})"
        )
    await reply(update, "\n".join(lines))


# --- /rename -----------------------------------------------------------------


def _rename_core(username, new_username):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    new_username = new_username.strip().lower()
    try:
        username_validator(new_username)
    except ValidationError as exc:
        return ("invalid", str(exc.messages[0]))
    if User.objects.filter(username=new_username).exclude(id=user.id).exists():
        return ("username_taken", new_username)
    old = user.username
    user.username = new_username
    user.full_clean()
    user.save(update_fields=["username"])
    return ("ok", old, new_username)


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if len(args) != 2:
        await reply(update, "Usage: /rename <username> <new_username>")
        return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _rename_core, args[0], args[1]
    )
    if result is DUPLICATE:
        return
    status = result[0]
    if status in ("not_found", "username_taken", "invalid"):
        logger.info("admin=%s rename failed: %s (%s)", admin.telegram_id, status, result[1])
    if status == "not_found":
        await reply(update, f"No such user '{result[1]}'.")
        return
    if status == "username_taken":
        await reply(update, f"Username '{result[1]}' is already taken.")
        return
    if status == "invalid":
        await reply(update, f"Could not rename: {result[1]}")
        return
    _, old, new = result
    logger.info("admin=%s renamed user %s -> %s", admin.telegram_id, old, new)
    await reply(update, f"Renamed '{old}' to '{new}'.")


# --- /deactivate, /reactivate --------------------------------------------------


def _set_active_core(username, active):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    user.is_active = active
    user.save(update_fields=["is_active"])
    return ("ok", user.username)


async def _cmd_set_active(update, context, active, usage):
    admin = context.user_data["admin"]
    args = context.args
    if len(args) != 1:
        await reply(update, usage)
        return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _set_active_core, args[0], active
    )
    if result is DUPLICATE:
        return
    if result[0] == "not_found":
        logger.info("admin=%s set-active failed: no such user '%s'", admin.telegram_id, result[1])
        await reply(update, f"No such user '{result[1]}'.")
        return
    logger.info(
        "admin=%s set is_active=%s for user=%s", admin.telegram_id, active, result[1]
    )
    state = "reactivated" if active else "deactivated"
    await reply(update, f"User '{result[1]}' {state}.")


async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_set_active(update, context, False, "Usage: /deactivate <username>")


async def cmd_reactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cmd_set_active(update, context, True, "Usage: /reactivate <username>")


# --- /resendinvite -------------------------------------------------------------


def _resendinvite_core(username):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    user.invite_tokens.filter(used_at__isnull=True).update(used_at=timezone.now())
    token = InviteToken.objects.create(user=user)
    return ("ok", (user, token))


async def cmd_resendinvite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    args = context.args
    if len(args) != 1:
        await reply(update, "Usage: /resendinvite <username>")
        return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _resendinvite_core, args[0]
    )
    if result is DUPLICATE:
        return
    status, payload = result
    if status == "not_found":
        logger.info("admin=%s resendinvite failed: no such user '%s'", admin.telegram_id, payload)
        await reply(update, f"No such user '{payload}'.")
        return
    user, token = payload
    logger.info("admin=%s resent invite for user=%s", admin.telegram_id, user.username)
    try:
        await sync_to_async(send_invite_email, thread_sensitive=True)(user, token.token)
        await reply(update, f"New invite sent to '{user.username}'. Case ID: {user.account_id}")
    except EmailSendError as exc:
        await reply(
            update,
            f"Could not send invite email: {exc}. Case ID: {user.account_id}",
        )


# --- /addadmin, /removeadmin (owner only) ------------------------------------


def _addadmin_core(telegram_id, display_name):
    admin, created = Admin.objects.get_or_create(
        telegram_id=telegram_id, defaults={"role": Admin.Role.ADMIN, "display_name": display_name}
    )
    if not created:
        return ("already_exists", admin.telegram_id)
    return ("ok", admin.telegram_id)


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    if not is_owner(admin):
        logger.warning("admin=%s tried owner-only /addadmin", admin.telegram_id)
        await reply(update, "Owner only.")
        return
    args = context.args
    if len(args) < 1:
        await reply(update, "Usage: /addadmin <telegram_id> [name]")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await reply(update, "telegram_id must be an integer.")
        return
    display_name = " ".join(args[1:])

    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _addadmin_core, target_id, display_name
    )
    if result is DUPLICATE:
        return
    status, tid = result
    if status == "already_exists":
        logger.info("admin=%s addadmin failed: %s already an admin", admin.telegram_id, tid)
        await reply(update, f"{tid} is already an admin.")
        return
    logger.info("admin=%s added new admin=%s", admin.telegram_id, tid)
    await reply(update, f"Added {tid} as an admin.")


def _removeadmin_core(telegram_id):
    target = Admin.objects.filter(telegram_id=telegram_id).first()
    if target is None:
        return ("not_found", telegram_id)
    if target.role == Admin.Role.OWNER:
        return ("is_owner", telegram_id)
    target.delete()
    return ("ok", telegram_id)


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    if not is_owner(admin):
        logger.warning("admin=%s tried owner-only /removeadmin", admin.telegram_id)
        await reply(update, "Owner only.")
        return
    args = context.args
    if len(args) != 1:
        await reply(update, "Usage: /removeadmin <telegram_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await reply(update, "telegram_id must be an integer.")
        return

    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _removeadmin_core, target_id
    )
    if result is DUPLICATE:
        return
    status, tid = result
    if status in ("not_found", "is_owner"):
        logger.info("admin=%s removeadmin failed: %s (target=%s)", admin.telegram_id, status, tid)
    if status == "not_found":
        await reply(update, f"{tid} is not an admin.")
        return
    if status == "is_owner":
        await reply(update, "Cannot remove the owner.")
        return
    logger.info("admin=%s removed admin=%s", admin.telegram_id, tid)
    await reply(update, f"Removed {tid} as an admin.")


# --- /help ---------------------------------------------------------------------


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    text = COMMON_HELP
    if is_owner(admin):
        text += "\n\n" + OWNER_HELP
    await reply(update, text)


# --- /menu: button-driven navigation -------------------------------------------
# One message that edits itself in place as the admin taps through sections,
# so the chat stays clean instead of filling up with command output.


def _kb(rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


BACK_ROW = [("‹ Back to menu", "menu:main")]

MAIN_MENU_TEXT = (
    "Member Portal — admin menu\n\n"
    "Pick an area. Each section explains its commands, and some let you act "
    "with a single tap."
)

MENU_CREDITS_TEXT = """\
💰 Money — move and inspect balances

Tap a button below and the bot will ask you for the details, with an example to copy.

Safety: any amount ≥ 10,000, or a debit that would go negative, asks you to Confirm/Cancel first. Every entry is permanent and records who authorized it.

Balances and history are on each user's page — browse them below. The classic commands (/credit, /debit, /balance, /history) still work too."""


def _main_menu(admin):
    rows = [
        [("👤 Users", "menu:users"), ("💰 Money", "menu:credits")],
        [("📨 Requests", "menu:requests"), ("💳 Cards", "menu:cards")],
        [("🧑‍💼 My profile", "menu:specialist"), ("📒 Audit", "menu:audit")],
    ]
    if is_owner(admin):
        rows.append([("🛡️ Admins", "menu:admins")])
    rows.append([("❓ All commands", "menu:help")])
    return MAIN_MENU_TEXT, _kb(rows)


def _menu_requests_view():
    pending = list(
        ServiceRequest.objects.select_related("user")
        .filter(status=ServiceRequest.Status.PENDING)
        .order_by("created_at")[:10]
    )
    if not pending:
        text = (
            "📨 Requests — portal transfer/deposit queue\n\n"
            "No pending requests right now. 🎉\n\n"
            "Users submit transfer and deposit requests from the portal; they land "
            "here for review. Approving a transfer debits the sender automatically; "
            "deposits are a status change only."
        )
        return text, _kb([[("🔄 Refresh", "menu:requests")], BACK_ROW])
    text = (
        f"📨 Requests — {len(pending)} pending review (oldest first)\n\n"
        "Tap a request to see exactly what's being asked for, then approve or "
        "decline it from there."
    )
    rows = [
        [(
            f"{sr.reference} · {sr.get_kind_display().lower()} · ${sr.amount:,} · {sr.user.username}",
            f"req:view:{sr.reference}",
        )]
        for sr in pending
    ]
    rows.append([("🔄 Refresh", "menu:requests")])
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _request_detail_view(reference):
    sr = ServiceRequest.objects.select_related("user").filter(reference=reference.upper()).first()
    if sr is None:
        return f"Request {reference} no longer exists.", _kb([[("‹ Requests", "menu:requests")]])
    u = sr.user
    lines = [f"📨 {sr.reference} — {sr.get_kind_display()}", ""]
    lines.append(f"From: {u.username} ({u.first_name} {u.last_name})".rstrip())
    lines.append(f"Amount: ${sr.amount:,}")
    if sr.kind == ServiceRequest.Kind.TRANSFER:
        dest = f"To account: {sr.recipient}"
        match = User.objects.filter(account_number=sr.recipient).first()
        if match is not None:
            dest += f" (internal user: {match.username})"
        lines.append(dest)
        if sr.recipient_bank:
            lines.append(f"Bank: {sr.recipient_bank} (routing {sr.routing_number})")
        elif sr.routing_number:
            lines.append(f"Routing: {sr.routing_number}")
    else:
        lines.append(f"Source platform: {sr.platform}")
        if sr.external_ref:
            lines.append(f"External ref: {sr.external_ref}")
    if sr.note:
        lines.append(f'Note: "{sr.note}"')
    lines.append(f"Sender balance: ${get_balance(u):,}")
    lines.append(f"Submitted: {sr.created_at:%b %d, %H:%M}")
    lines.append(f"Status: {sr.status}")
    if sr.status == ServiceRequest.Status.PENDING:
        lines.append(
            "\nApproving debits the sender automatically."
            if sr.kind == ServiceRequest.Kind.TRANSFER
            else "\nApproving is a status change only — add the funds with /credit."
        )
        rows = [
            [("✅ Approve", f"req:approve:{sr.reference}"), ("❌ Decline", f"req:decline:{sr.reference}")],
            [("‹ Requests", "menu:requests"), ("⌂ Menu", "menu:main")],
        ]
    else:
        rows = [[("‹ Requests", "menu:requests"), ("⌂ Menu", "menu:main")]]
    return "\n".join(lines), _kb(rows)


def _menu_users_view():
    users = list(User.objects.order_by("username")[:25])
    text = f"👤 Users — {len(users)} accounts. Tap one to view and manage it, or create a new one."
    rows = []
    for u in users:
        mark = "" if u.is_active else " · 🚫 inactive"
        rows.append([(f"{u.username} · ${get_balance(u):,}{mark}", f"usr:view:{u.username}")])
    rows.append([("➕ Create user", "usr:new")])
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _user_detail_view(username):
    u = find_user(username)
    if u is None:
        return f"No such user '{username}'.", _kb([[("‹ Users", "menu:users")]])
    app = CardApplication.objects.filter(user=u).first()
    pending = u.service_requests.filter(status=ServiceRequest.Status.PENDING).count()
    lines = [f"👤 {u.username}" + (f" — {u.first_name} {u.last_name}".rstrip() if u.first_name or u.last_name else "")]
    lines.append("")
    lines.append(f"Balance: ${get_balance(u):,}")
    lines.append(f"Status: {'Active' if u.is_active else '🚫 Deactivated'}")
    lines.append(f"Email: {u.email}")
    lines.append(f"Case ID: {u.account_id} · Acct #: {u.account_number}")
    lines.append(f"Member since: {u.created_at:%b %d, %Y}")
    lines.append(f"Created by: {describe_admin(u.created_by_id) if u.created_by_id else '—'}")
    lines.append(f"Card application: {'yes — tap Card below' if app else 'none'}")
    lines.append(f"Delivery address: {'on file' if u.has_address else 'none'}")
    lines.append(f"Pending requests: {pending}")
    lines.append("")
    lines.append("Typed actions for this user (copy and fill in):")
    lines.append(f"/rename {u.username} <new_username>")
    lines.append(f"/setpassword {u.username} <password>")
    toggle = ("🚫 Deactivate", f"usr:act:{u.username}") if u.is_active else ("✅ Reactivate", f"usr:act:{u.username}")
    rows = [
        [("➕ Credit", f"usr:credit:{u.username}"), ("➖ Debit", f"usr:debit:{u.username}")],
        [("📒 History", f"usr:hist:{u.username}"), ("💳 Card", f"card:view:{u.username}")],
        [("💬 Message", f"usr:msg:{u.username}"), ("✉️ Resend invite", f"usr:inv:{u.username}")],
        [toggle, ("🏠 Address", f"usr:addr:{u.username}")],
        [("‹ Users", "menu:users"), ("⌂ Menu", "menu:main")],
    ]
    return "\n".join(lines), _kb(rows)


def _user_history_view(username):
    u = find_user(username)
    if u is None:
        return f"No such user '{username}'.", _kb([[("‹ Users", "menu:users")]])
    entries = list(u.ledger_entries.all()[:10])
    lines = [f"📒 {u.username} — last {len(entries)} entries (balance ${get_balance(u):,})", ""]
    if not entries:
        lines.append("No ledger entries yet.")
    for e in entries:
        lines.append(f"{e.created_at:%b %d %H:%M} {e.amount:+,.2f} - {e.reason} (by {describe_admin(e.authorized_by)})")
    lines.append(f"\n/history {u.username} 50 shows more.")
    rows = [[(f"‹ {u.username}", f"usr:view:{u.username}"), ("⌂ Menu", "menu:main")]]
    return "\n".join(lines), _kb(rows)


def _toggle_active_core(username):
    user = find_user(username)
    if user is None:
        return ("not_found", username)
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    return ("ok", user)


CARD_STATE_SYMBOLS = {
    "received": "✅", "missing": "⚠️",
    "in_review": "⏳", "reviewed": "✅",
    "pending": "▫️", "verified": "✅", "resubmit": "⚠️", "info_needed": "⏳",
    "waiting": "▫️", "in_progress": "⏳", "ready": "✅",
    "shipped": "✅",
}

CARD_STAGE_TITLES = [
    ("docs", "docs_status", "Account verified"),
    ("review", "review_status", "Under review"),
    ("result", "result_status", "Verification result"),
    ("production", "production_status", "Card production"),
    ("shipping", "shipping_status", "Shipping"),
]


def _card_stage_actions(app, username):
    """One-tap buttons for whatever needs deciding next on this application."""
    if app.docs_status == "missing":
        return [[("✅ Docs received", f"card:set:{username}:docs:received")]]
    if app.review_status == "in_review":
        return [
            [("✅ Mark reviewed", f"card:set:{username}:review:reviewed")],
            [("⚠️ Docs missing", f"card:set:{username}:docs:missing")],
        ]
    if app.result_status in ("pending", "resubmit", "info_needed"):
        return [
            [("✅ Verified", f"card:set:{username}:result:verified")],
            [("⚠️ Needs resubmission", f"card:set:{username}:result:resubmit"),
             ("⏳ Info requested", f"card:set:{username}:result:info_needed")],
        ]
    if app.production_status == "waiting":
        return [[("▶️ Start production", f"card:set:{username}:production:in_progress")]]
    if app.production_status == "in_progress":
        return [[("✅ Card ready", f"card:set:{username}:production:ready")]]
    if app.shipping_status == "pending":
        return [[("📦 Mark shipped", f"card:set:{username}:shipping:shipped")]]
    return []


def _card_detail_view(username):
    u = find_user(username)
    if u is None:
        return f"No such user '{username}'.", _kb([[("‹ Cards", "menu:cards")]])
    app = CardApplication.objects.filter(user=u).first()
    if app is None:
        text = (
            f"💳 {u.username} has no card application.\n\n"
            "Opening one marks their documents as Received — only verified "
            "accounts should get this far."
        )
        rows = [
            [("＋ Open application", f"card:start:{u.username}")],
            [("‹ Cards", "menu:cards"), ("⌂ Menu", "menu:main")],
        ]
        return text, _kb(rows)
    lines = [f"💳 Card application — {u.username}", ""]
    for _key, field, title in CARD_STAGE_TITLES:
        state = getattr(app, field)
        label = getattr(app, f"get_{field}_display")()
        lines.append(f"{CARD_STATE_SYMBOLS[state]} {title}: {label}")
    if app.tracking_number:
        lines.append(f"\nTracking: {app.tracking_number}")
    lines.append("\nThe buttons below are the next decisions for this application.")
    if app.shipping_status == "pending":
        lines.append(f"To ship WITH a tracking number: /card {u.username} shipping shipped <tracking>")
    rows = _card_stage_actions(app, u.username)
    rows.append([("‹ Cards", "menu:cards"), ("⌂ Menu", "menu:main")])
    return "\n".join(lines), _kb(rows)


def _menu_cards_view():
    apps = list(CardApplication.objects.select_related("user").order_by("user__username")[:25])
    text = (
        f"💳 Cards — {len(apps)} open applications. Tap one to review and "
        "advance it, stage by stage."
    )
    rows = []
    for app in apps:
        current = "complete"
        for _key, field, title in CARD_STAGE_TITLES:
            if CARD_STATE_SYMBOLS[getattr(app, field)] != "✅":
                current = title
                break
        rows.append([(f"{app.user.username} · {current}", f"card:view:{app.user.username}")])
    rows.append([("＋ Start a new application", "card:new")])
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _card_new_view():
    have_app = CardApplication.objects.values_list("user_id", flat=True)
    users = list(User.objects.filter(is_active=True).exclude(id__in=have_app).order_by("username")[:25])
    if not users:
        return "Every active user already has a card application.", _kb([[("‹ Cards", "menu:cards")]])
    text = "＋ Start a card application — tap the user it's for:"
    rows = [[(u.username, f"card:start:{u.username}")] for u in users]
    rows.append([("‹ Cards", "menu:cards")])
    return text, _kb(rows)


def _menu_admins_view():
    admins = list(Admin.objects.all())
    text = (
        "🛡️ Admins — tap one to view or remove.\n\n"
        "Adding a new admin needs their numeric Telegram ID typed:\n"
        "/addadmin <telegram_id> [name]"
    )
    rows = [
        [(f"{describe_admin(a.telegram_id)} · {a.role}", f"adm:view:{a.telegram_id}")]
        for a in admins
    ]
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _admin_detail_view(telegram_id):
    a = Admin.objects.filter(telegram_id=telegram_id).first()
    if a is None:
        return "That admin no longer exists.", _kb([[("‹ Admins", "menu:admins")]])
    created = a.created_users.count()
    lines = [f"🛡️ {describe_admin(a.telegram_id)}", ""]
    lines.append(f"Role: {a.role}")
    lines.append(f"Users created: {created}")
    lines.append(f"Specialist profile: {(a.first_name + ' ' + a.last_name).strip() or 'not set'}")
    lines.append(f"Admin since: {a.created_at:%b %d, %Y}")
    rows = []
    if a.role != Admin.Role.OWNER:
        rows.append([("🗑 Remove this admin", f"adm:rm:{a.telegram_id}")])
    else:
        lines.append("\nThe owner cannot be removed.")
    rows.append([("‹ Admins", "menu:admins"), ("⌂ Menu", "menu:main")])
    return "\n".join(lines), _kb(rows)


def _admin_remove_confirm_view(telegram_id):
    a = Admin.objects.filter(telegram_id=telegram_id).first()
    if a is None:
        return "That admin no longer exists.", _kb([[("‹ Admins", "menu:admins")]])
    text = (
        f"Remove {describe_admin(a.telegram_id)} as an admin?\n\n"
        "They immediately lose all bot access. Users they created keep working "
        "and stay attributed to them."
    )
    rows = [
        [("✅ Yes, remove", f"adm:rmyes:{a.telegram_id}")],
        [("‹ Cancel", f"adm:view:{a.telegram_id}")],
    ]
    return text, _kb(rows)


def _menu_specialist_view(telegram_id):
    admin = Admin.objects.get(telegram_id=telegram_id)
    text = (
        "🧑‍💼 My specialist profile\n\n"
        "Shown as \"Admin Specialist\" on the dashboard of every user you create, "
        "so they know who to contact.\n\n"
        f"{_format_specialist(admin)}\n\n"
        "Update any field:\n"
        "/specialist name <first> <last>\n"
        "/specialist email <address>\n"
        "/specialist phone <number>\n"
        "/specialist avatar <woman|man>"
    )
    return text, _kb([BACK_ROW])


def _menu_audit_view():
    entries = _audit_rows(10)
    lines = ["📒 Audit — the last 10 ledger entries across all users\n"]
    if not entries:
        lines.append("No ledger entries yet.")
    for e in entries:
        lines.append(
            f"{e.created_at:%b %d %H:%M} {e.user.username}: {e.amount:+,.2f} - {e.reason} "
            f"(by {describe_admin(e.authorized_by)})"
        )
    lines.append("\n/audit [n] shows more; /history <username> follows one user.")
    return "\n".join(lines), _kb([[("🔄 Refresh", "menu:audit")], BACK_ROW])


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = context.user_data["admin"]
    text, kb = _main_menu(admin)
    await reply(update, text, reply_markup=kb)


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = context.user_data["admin"]
    context.user_data.pop("pending_input", None)
    section = query.data.split(":", 1)[1]

    if section == "admins" and not is_owner(admin):
        await query.answer("Owner only.", show_alert=True)
        return

    if section == "main":
        text, kb = _main_menu(admin)
    elif section == "users":
        text, kb = await sync_to_async(_menu_users_view, thread_sensitive=True)()
    elif section == "credits":
        text = MENU_CREDITS_TEXT
        kb = _kb([
            [("➕ Credit a user", "flow:credit"), ("➖ Debit a user", "flow:debit")],
            [("👤 Browse users", "menu:users")],
            BACK_ROW,
        ])
    elif section == "cards":
        text, kb = await sync_to_async(_menu_cards_view, thread_sensitive=True)()
    elif section == "requests":
        text, kb = await sync_to_async(_menu_requests_view, thread_sensitive=True)()
    elif section == "specialist":
        text, kb = await sync_to_async(_menu_specialist_view, thread_sensitive=True)(admin.telegram_id)
    elif section == "audit":
        text, kb = await sync_to_async(_menu_audit_view, thread_sensitive=True)()
    elif section == "admins":
        text, kb = await sync_to_async(_menu_admins_view, thread_sensitive=True)()
    elif section == "help":
        text = COMMON_HELP + (("\n\n" + OWNER_HELP) if is_owner(admin) else "")
        kb = _kb([BACK_ROW])
    else:
        await query.answer()
        return

    await query.answer()
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# --- guided input flows ---------------------------------------------------------
# A button arms context.user_data["pending_input"]; the admin's next plain-text
# message supplies the values (no command prefix needed).


def _flow_prompt_create():
    text = (
        "➕ Create a user\n\n"
        "Send the new user's details in ONE message, in this order:\n\n"
        "username email first_name last_name\n\n"
        "Example (copy and edit):\n"
        "jsmith jane.smith@example.com Jane Smith\n\n"
        "Username: 3-32 chars, lowercase letters/digits/underscores. Names are "
        "single words. The account is created immediately and the invite email "
        "goes out with their Case ID."
    )
    return text, _kb([[("✖ Cancel", "flow:cancel")]])


def _flow_prompt_amount(action, username=None):
    doing = "➕ Credit" if action == "credit" else "➖ Debit"
    if username:
        head = f"{doing} {username}\n\nSend the amount and the reason in ONE message:\n\namount reason"
        example = "500 Weekly bonus" if action == "credit" else "250 Store purchase"
    else:
        head = f"{doing} a user\n\nSend the username, amount, and reason in ONE message:\n\nusername amount reason"
        example = "jsmith 500 Weekly bonus" if action == "credit" else "jsmith 250 Store purchase"
    text = (
        f"{head}\n\nExample (copy and edit):\n{example}\n\n"
        "The reason appears on their statement. Large amounts ask for a "
        "Confirm/Cancel step before anything is written."
    )
    return text, _kb([[("✖ Cancel", "flow:cancel")]])


async def _finish_flow_create(update, context, admin, tokens):
    if len(tokens) != 4:
        await reply(
            update,
            "That doesn't look right — send exactly four items:\n"
            "username email first_name last_name\n"
            "Example: jsmith jane.smith@example.com Jane Smith",
        )
        return
    username, email, first_name, last_name = tokens
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _create_core, admin.telegram_id, username, email, first_name, last_name
    )
    if result is DUPLICATE:
        return
    status, payload = result
    if status == "username_taken":
        await reply(update, f"Username '{payload}' is already taken — send different details, or Cancel above.")
        return
    if status == "email_taken":
        await reply(update, f"Email '{payload}' is already registered — send different details, or Cancel above.")
        return
    if status == "invalid":
        await reply(update, f"Could not create user: {payload}\nFix it and send again, or Cancel above.")
        return
    context.user_data.pop("pending_input", None)
    user, token = payload
    logger.info("admin=%s created user=%s (guided flow)", admin.telegram_id, user.username)
    await reply(
        update,
        f"Created user '{user.username}' ({user.email}).\n"
        f"Case ID: {user.account_id}\n"
        f"Account number: {user.account_number}",
    )
    try:
        await sync_to_async(send_invite_email, thread_sensitive=True)(user, token.token)
        await reply(update, "Invite email sent.")
    except EmailSendError as exc:
        await reply(
            update,
            f"Warning: invite email could not be sent ({exc}). "
            f"Use /resendinvite {user.username} to retry once fixed.",
        )


def _user_address_view(username):
    u = find_user(username)
    if u is None:
        return f"No such user '{username}'.", _kb([[("‹ Users", "menu:users")]])
    text = (
        f"🏠 Delivery address — {u.username}\n\n"
        f"{_format_address(u)}\n\n"
        "Used when their card ships. The user can also edit this from their "
        "Profile page — whichever change is most recent applies."
    )
    rows = [[("✏️ Edit", f"usr:addredit:{u.username}")]]
    if u.has_address:
        rows[0].append(("🗑 Clear", f"usr:addrclear:{u.username}"))
    rows.append([(f"‹ {u.username}", f"usr:view:{u.username}"), ("⌂ Menu", "menu:main")])
    return text, _kb(rows)


def _flow_prompt_address(username):
    text = (
        f"🏠 Set delivery address for {username}\n\n"
        "Send it in ONE message as:\n"
        "street, [apt/suite,] city, state zip\n\n"
        "Example (copy and edit):\n"
        "123 Main St, Apt 4B, Springfield, IL 62704"
    )
    return text, _kb([[("✖ Cancel", "flow:cancel")]])


async def _finish_flow_address(update, context, admin, body, username):
    fields = parse_address(body)
    if fields is None:
        await reply(
            update,
            "Couldn't parse that — send: street, [apt,] city, state zip\n"
            "Example: 123 Main St, Apt 4B, Springfield, IL 62704",
        )
        return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _address_set_core, username, fields
    )
    if result is DUPLICATE:
        return
    status, payload = result
    context.user_data.pop("pending_input", None)
    if status == "not_found":
        await reply(update, f"No such user '{payload}' anymore.")
        return
    uname, text = payload
    logger.info("admin=%s set address for user=%s (guided flow)", admin.telegram_id, uname)
    await reply(update, f"Saved. Delivery address for '{uname}':\n{text}")


def _flow_prompt_message(username):
    text = (
        f"💬 Message {username}\n\n"
        "Type the message now, exactly as they should read it — it appears on "
        "their portal Messages page as a reply from you.\n\n"
        "Example:\nHi Jane, your transfer request was approved this morning."
    )
    return text, _kb([[("✖ Cancel", "flow:cancel")]])


async def _finish_flow_message(update, context, admin, body, username):
    body = body.strip()
    if not body:
        await reply(update, "The message was empty — type it again, or Cancel above.")
        return
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _msg_core, admin.telegram_id, username, body
    )
    if result is DUPLICATE:
        return
    status, payload = result
    if status == "not_found":
        context.user_data.pop("pending_input", None)
        await reply(update, f"No such user '{payload}' anymore.")
        return
    context.user_data.pop("pending_input", None)
    logger.info("admin=%s messaged user=%s (guided flow)", admin.telegram_id, payload)
    await reply(update, f"Sent — '{payload}' will see it on their Messages page.")


async def _finish_flow_amount(update, context, admin, flow, tokens, fixed_username):
    if fixed_username is None:
        if len(tokens) < 3:
            await reply(update, "Send at least: username amount reason — e.g. jsmith 500 Weekly bonus")
            return
        username, amount_token, reason = tokens[0], tokens[1], " ".join(tokens[2:])
    else:
        if len(tokens) < 2:
            await reply(update, "Send at least: amount reason — e.g. 500 Weekly bonus")
            return
        username, amount_token, reason = fixed_username, tokens[0], " ".join(tokens[1:])
    amount = parse_money(amount_token)
    if amount is None:
        await reply(update, "The amount must be a positive number — e.g. 500 Weekly bonus or 500.43 Refund")
        return
    user = await sync_to_async(find_user, thread_sensitive=True)(username)
    if user is None:
        await reply(update, f"No such user '{username}' — check the username and send again, or Cancel above.")
        return
    context.user_data.pop("pending_input", None)
    if flow == "credit":
        await _execute_credit(update, context, admin, user, amount, reason)
    else:
        await _execute_debit(update, context, admin, user, amount, reason)


async def on_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await sync_to_async(get_admin, thread_sensitive=True)(update.effective_user.id)
    if admin is None:
        return
    context.user_data["admin"] = admin
    pending = context.user_data.get("pending_input")
    if not pending:
        await reply(update, "No guided action in progress — open /menu to get started.")
        return
    text = update.effective_message.text
    flow = pending["flow"]
    if flow == "create":
        await _finish_flow_create(update, context, admin, text.split())
    elif flow in ("credit", "debit"):
        await _finish_flow_amount(update, context, admin, flow, text.split(), pending.get("username"))
    elif flow == "message":
        await _finish_flow_message(update, context, admin, text, pending.get("username"))
    elif flow == "address":
        await _finish_flow_address(update, context, admin, text, pending.get("username"))


async def _edit_menu_message(query, text, kb):
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def on_ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drill-down views and one-tap actions for the /menu UI
    (usr:*, card:*, adm:*, req:view:*)."""
    query = update.callback_query
    admin = context.user_data["admin"]
    context.user_data.pop("pending_input", None)
    parts = query.data.split(":")
    ns, action = parts[0], parts[1]
    arg = parts[2] if len(parts) > 2 else None

    if ns == "adm" and not is_owner(admin):
        await query.answer("Owner only.", show_alert=True)
        return

    view = None  # (builder, args) resolved below; actions set it after acting

    if ns == "flow":
        if action == "cancel":
            await query.answer("Cancelled.")
            text, kb = _main_menu(admin)
            await _edit_menu_message(query, text, kb)
            return
        if action in ("credit", "debit"):
            context.user_data["pending_input"] = {"flow": action}
            await query.answer()
            text, kb = _flow_prompt_amount(action)
            await _edit_menu_message(query, text, kb)
            return

    elif ns == "req" and action == "view":
        view = (_request_detail_view, (arg,))

    elif ns == "usr":
        if action == "new":
            context.user_data["pending_input"] = {"flow": "create"}
            await query.answer()
            text, kb = _flow_prompt_create()
            await _edit_menu_message(query, text, kb)
            return
        if action in ("credit", "debit"):
            context.user_data["pending_input"] = {"flow": action, "username": arg}
            await query.answer()
            text, kb = _flow_prompt_amount(action, arg)
            await _edit_menu_message(query, text, kb)
            return
        if action == "msg":
            context.user_data["pending_input"] = {"flow": "message", "username": arg}
            await query.answer()
            text, kb = _flow_prompt_message(arg)
            await _edit_menu_message(query, text, kb)
            return
        if action == "addr":
            view = (_user_address_view, (arg,))
        elif action == "addredit":
            context.user_data["pending_input"] = {"flow": "address", "username": arg}
            await query.answer()
            text, kb = _flow_prompt_address(arg)
            await _edit_menu_message(query, text, kb)
            return
        elif action == "addrclear":
            result = await sync_to_async(process_once, thread_sensitive=True)(
                update.update_id, _address_set_core, arg, None
            )
            if result is DUPLICATE:
                await query.answer("Already cleared.")
            elif result[0] == "not_found":
                await query.answer(f"No such user '{result[1]}'.", show_alert=True)
            else:
                logger.info("admin=%s cleared address for user=%s (menu)", admin.telegram_id, arg)
                await query.answer("Address removed.")
            view = (_user_address_view, (arg,))
        if action == "view":
            view = (_user_detail_view, (arg,))
        elif action == "hist":
            view = (_user_history_view, (arg,))
        elif action == "act":
            result = await sync_to_async(process_once, thread_sensitive=True)(
                update.update_id, _toggle_active_core, arg
            )
            if result is DUPLICATE:
                await query.answer("Already done.")
            elif result[0] == "not_found":
                await query.answer(f"No such user '{result[1]}'.", show_alert=True)
            else:
                user = result[1]
                state = "reactivated" if user.is_active else "deactivated"
                logger.info("admin=%s %s user=%s (menu)", admin.telegram_id, state, user.username)
                await query.answer(f"{user.username} {state}.")
            view = (_user_detail_view, (arg,))
        elif action == "inv":
            result = await sync_to_async(process_once, thread_sensitive=True)(
                update.update_id, _resendinvite_core, arg
            )
            if result is DUPLICATE:
                await query.answer("Already sent.")
            elif result[0] == "not_found":
                await query.answer(f"No such user '{result[1]}'.", show_alert=True)
            else:
                user, token = result[1]
                logger.info("admin=%s resent invite for user=%s (menu)", admin.telegram_id, user.username)
                try:
                    await sync_to_async(send_invite_email, thread_sensitive=True)(user, token.token)
                    await query.answer(f"Invite emailed to {user.email}.", show_alert=True)
                except EmailSendError as exc:
                    await query.answer(f"Email failed: {exc}", show_alert=True)
            view = (_user_detail_view, (arg,))

    elif ns == "card":
        if action == "view":
            view = (_card_detail_view, (arg,))
        elif action == "new":
            view = (_card_new_view, ())
        elif action == "start":
            result = await sync_to_async(process_once, thread_sensitive=True)(
                update.update_id, _card_start_core, arg
            )
            if result is DUPLICATE:
                await query.answer("Already opened.")
            elif result[0] == "not_found":
                await query.answer(f"No such user '{result[1]}'.", show_alert=True)
            else:
                logger.info("admin=%s opened card application for %s (menu)", admin.telegram_id, arg)
                await query.answer("Application opened.")
            view = (_card_detail_view, (arg,))
        elif action == "set" and len(parts) == 5:
            stage, state = parts[3], parts[4]
            result = await sync_to_async(process_once, thread_sensitive=True)(
                update.update_id, _card_set_core, arg, stage, state, None
            )
            if result is DUPLICATE:
                await query.answer("Already done.")
            elif result[0] != "ok":
                await query.answer("Couldn't update that stage.", show_alert=True)
            else:
                logger.info(
                    "admin=%s set card %s=%s for %s (menu)", admin.telegram_id, stage, state, arg
                )
                await query.answer(f"{stage} → {state}")
            view = (_card_detail_view, (arg,))

    elif ns == "adm":
        if action == "view":
            view = (_admin_detail_view, (int(arg),))
        elif action == "rm":
            view = (_admin_remove_confirm_view, (int(arg),))
        elif action == "rmyes":
            result = await sync_to_async(process_once, thread_sensitive=True)(
                update.update_id, _removeadmin_core, int(arg)
            )
            if result is DUPLICATE:
                await query.answer("Already removed.")
            elif result[0] == "is_owner":
                await query.answer("The owner can't be removed.", show_alert=True)
            elif result[0] == "not_found":
                await query.answer("Already gone.")
            else:
                logger.warning("owner=%s removed admin %s (menu)", admin.telegram_id, arg)
                await query.answer("Admin removed.")
            view = (_menu_admins_view, ())

    if view is None:
        await query.answer()
        return
    builder, args = view
    text, kb = await sync_to_async(builder, thread_sensitive=True)(*args)
    await query.answer()
    await _edit_menu_message(query, text, kb)


async def on_request_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = context.user_data["admin"]
    _, action, reference = query.data.split(":", 2)
    status = (
        ServiceRequest.Status.APPROVED if action == "approve" else ServiceRequest.Status.DECLINED
    )
    result = await sync_to_async(process_once, thread_sensitive=True)(
        update.update_id, _decide_request_core, admin.telegram_id, reference, status
    )
    if result is DUPLICATE:
        await query.answer("Already handled.")
    else:
        outcome, payload = result
        if outcome == "not_found":
            await query.answer(f"No request {reference}.", show_alert=True)
        elif outcome == "already_decided":
            await query.answer(f"{payload.reference} was already {payload.status}.", show_alert=True)
        elif outcome == "insufficient":
            sr, balance = payload
            logger.info(
                "admin=%s approve blocked (menu): %s needs $%s, balance $%s",
                admin.telegram_id, sr.reference, sr.amount, balance,
            )
            await query.answer(
                f"Not approved — {sr.user.username} has ${balance:,} but {sr.reference} "
                f"is for ${sr.amount:,}. Credit the account first, or decline it.",
                show_alert=True,
            )
        else:
            sr, _line, new_balance = payload
            logger.info("admin=%s marked request %s %s (menu)", admin.telegram_id, sr.reference, sr.status)
            if new_balance is not None:
                msg = (f"{sr.reference} approved. Debited ${sr.amount:,} from "
                       f"{sr.user.username}. New balance: ${new_balance:,}.")
            elif action == "approve":
                msg = f"{sr.reference} approved. Add the funds with /credit."
            else:
                msg = f"{sr.reference} declined."
            await query.answer(msg, show_alert=True)
    # Re-render the queue so the decided request drops off the button list.
    text, kb = await sync_to_async(_menu_requests_view, thread_sensitive=True)()
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# --- error handler -------------------------------------------------------------


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("unhandled error processing update=%s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong. Please try again.")
        except Exception:
            pass
