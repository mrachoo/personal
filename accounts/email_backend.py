import logging

import resend
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger("bot")


class ResendEmailBackend(BaseEmailBackend):
    """Routes every Django-emitted email (password reset, invites, ...)
    through Resend, so there is exactly one email-sending path."""

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        if not settings.RESEND_API_KEY:
            if self.fail_silently:
                return 0
            raise RuntimeError("RESEND_API_KEY is not configured.")

        resend.api_key = settings.RESEND_API_KEY
        sent = 0
        for message in email_messages:
            html_body = None
            for content, mimetype in getattr(message, "alternatives", []) or []:
                if mimetype == "text/html":
                    html_body = content
                    break
            payload = {
                "from": message.from_email or settings.DEFAULT_FROM_EMAIL,
                "to": list(message.to),
                "subject": message.subject,
                "text": message.body,
            }
            if html_body:
                payload["html"] = html_body
            try:
                resend.Emails.send(payload)
                sent += 1
            except Exception as exc:
                logger.warning("email send failed to=%s subject=%s: %s", message.to, message.subject, exc)
                if not self.fail_silently:
                    raise
        return sent
