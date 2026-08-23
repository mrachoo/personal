from .models import PortalMessage


def unread_messages(request):
    """Unread admin-message count, for the badge on the Messages nav item."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    count = PortalMessage.objects.filter(
        user=user, sender=PortalMessage.Sender.ADMIN, read_at__isnull=True
    ).count()
    return {"unread_messages": count}
