import logging

import requests
from django.conf import settings

logger = logging.getLogger("bot")


def send_telegram_message(telegram_id, text):
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": telegram_id, "text": text},
            timeout=5,
        )
    except requests.RequestException:
        logger.warning("telegram notify failed for telegram_id=%s", telegram_id, exc_info=True)


def notify_password_changed(user, source):
    admin = user.created_by
    if admin is None:
        return
    send_telegram_message(
        admin.telegram_id,
        f"Password changed for '{user.username}' ({user.email}) — {source}.",
    )


def notify_address_changed(user):
    lines = user.address_lines()
    detail = "\n".join(lines) if lines else "(address removed)"
    text = (
        f"🏠 Delivery address updated for '{user.username}' by the user from the portal:\n"
        f"{detail}"
    )
    for telegram_id in _admin_recipients_for(user):
        send_telegram_message(telegram_id, text)


def _admin_recipients_for(user):
    from accounts.models import Admin

    if user.created_by is not None:
        return [user.created_by.telegram_id]
    return list(Admin.objects.filter(role=Admin.Role.OWNER).values_list("telegram_id", flat=True))


def notify_new_portal_message(message):
    user = message.user
    text = (
        f"💬 Message from '{user.username}' ({user.first_name} {user.last_name}):\n\n"
        f"{message.body}\n\n"
        f"Reply with /msg {user.username} <text> — it appears on their Messages page."
    )
    for telegram_id in _admin_recipients_for(user):
        send_telegram_message(telegram_id, text)


def notify_new_service_request(service_request):
    # Route to the admin who created the user; fall back to every owner so a
    # request from an unowned account (e.g. seeded test users) isn't silent.
    from accounts.models import Admin, ServiceRequest

    user = service_request.user
    if service_request.kind == ServiceRequest.Kind.TRANSFER:
        detail = f"${service_request.amount:,} to account {service_request.recipient}"
    else:
        detail = (
            f"${service_request.amount:,} from {service_request.platform}"
            f" (ref {service_request.external_ref or 'n/a'})"
        )
    text = (
        f"New {service_request.get_kind_display().lower()} request {service_request.reference} "
        f"from '{user.username}': {detail}.\n"
        f"Review with /requests, then /approve {service_request.reference} "
        f"or /decline {service_request.reference}."
    )
    if user.created_by is not None:
        recipients = [user.created_by.telegram_id]
    else:
        recipients = list(
            Admin.objects.filter(role=Admin.Role.OWNER).values_list("telegram_id", flat=True)
        )
    for telegram_id in recipients:
        send_telegram_message(telegram_id, text)
