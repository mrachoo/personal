from django.contrib import admin
from django.contrib.auth.models import Group

from .models import Admin as BotAdmin
from .models import LedgerEntry, User

# The bot is the only write path for balances — LedgerEntry stays fully
# read-only here for everyone, including superusers. That guarantee (no
# confirmation bypass, no missing authorized_by, no missing audit trail)
# is the whole point of the ledger design; it doesn't get a carve-out.
admin.site.unregister(Group)


def _linked_admin(request):
    """The bot Admin record a logged-in staff user is scoped to, or None
    for a superuser / a staff account with no such link."""
    if not request.user.is_authenticated or request.user.is_superuser:
        return None
    return BotAdmin.objects.filter(portal_login=request.user).first()


class ReadOnlyModelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(User)
class PortalUserAdmin(admin.ModelAdmin):
    """Superusers see and edit every user. A staff account linked to a bot
    Admin (via Admin.portal_login) is scoped to only the users that admin
    created through /create — never to other admins' users, and never to
    creating or deleting accounts (that stays the bot's job, since it's what
    wires up the invite token and email)."""

    list_display = ("username", "email", "account_id", "is_active", "created_by", "created_at")
    search_fields = ("username", "email", "account_id")
    list_filter = ("is_active",)
    ordering = ("-created_at",)
    fields = ("username", "email", "is_active", "account_id", "created_by", "created_at")
    readonly_fields = ("account_id", "created_by", "created_at")

    def has_module_permission(self, request):
        return request.user.is_superuser or _linked_admin(request) is not None

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        linked = _linked_admin(request)
        if linked is None:
            return False
        if obj is None:
            return True
        return obj.created_by_id == linked.telegram_id

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        linked = _linked_admin(request)
        if linked is None:
            return qs.none()
        return qs.filter(created_by=linked)


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "user", "amount", "reason", "authorized_by")
    search_fields = ("user__username", "reason")
    list_filter = ("created_at",)
    ordering = ("-created_at",)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(BotAdmin)
class BotAdminAdmin(admin.ModelAdmin):
    """Owner-only: regular admins never see or manage other admins here —
    that mirrors /addadmin and /removeadmin already being owner-only bot
    commands."""

    list_display = ("telegram_id", "role", "display_name", "portal_login", "created_at")
    list_filter = ("role",)
    ordering = ("-created_at",)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        if not request.user.is_superuser:
            return False
        return obj is None or obj.role != BotAdmin.Role.OWNER

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.role == BotAdmin.Role.OWNER:
            return ("role",)
        return ()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "portal_login":
            kwargs["queryset"] = User.objects.filter(is_staff=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
