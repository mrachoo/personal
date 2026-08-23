from accounts.models import Admin


def get_admin(telegram_id):
    return Admin.objects.filter(telegram_id=telegram_id).first()


def is_owner(admin):
    return admin is not None and admin.role == Admin.Role.OWNER
