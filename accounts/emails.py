import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger("bot")


class EmailSendError(Exception):
    pass


def send_invite_email(user, token):
    if not settings.RESEND_API_KEY:
        raise EmailSendError("RESEND_API_KEY is not configured.")

    link = f"{settings.PORTAL_BASE_URL.rstrip('/')}/invite/{token}"
    text = (
        f"Hi {user.username},\n\n"
        "An account has been created for you on the Member Portal.\n"
        f"Set your password here: {link}\n\n"
        f"Your Case ID is: {user.account_id}\n"
        "You'll need this every time you log in, so keep it somewhere safe.\n\n"
        "This link expires in 72 hours."
    )
    html = (
        f"<p>Hi {user.username},</p>"
        "<p>An account has been created for you on the Member Portal. "
        f'Click <a href="{link}">here</a> to set your password and log in.</p>'
        f"<p>Your Case ID is: <strong>{user.account_id}</strong><br>"
        "You'll need this every time you log in, so keep it somewhere safe.</p>"
        "<p>This link expires in 72 hours.</p>"
    )
    message = EmailMultiAlternatives(
        subject="You've been invited to the Member Portal",
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    message.attach_alternative(html, "text/html")
    try:
        message.send()
    except Exception as exc:
        logger.warning("invite email failed for user=%s: %s", user.username, exc)
        raise EmailSendError(str(exc)) from exc
