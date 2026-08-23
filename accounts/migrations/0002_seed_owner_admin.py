import os

from django.db import migrations


def seed_owner_admin(apps, schema_editor):
    raw_owner_id = os.environ.get("OWNER_TELEGRAM_ID", "").strip()
    if not raw_owner_id:
        return

    try:
        owner_id = int(raw_owner_id)
    except ValueError:
        raise ValueError(
            f"OWNER_TELEGRAM_ID={raw_owner_id!r} is not a valid Telegram ID (must be an integer)."
        )

    Admin = apps.get_model("accounts", "Admin")
    Admin.objects.update_or_create(
        telegram_id=owner_id,
        defaults={"role": "owner"},
    )


def unseed_owner_admin(apps, schema_editor):
    raw_owner_id = os.environ.get("OWNER_TELEGRAM_ID", "").strip()
    if not raw_owner_id:
        return
    Admin = apps.get_model("accounts", "Admin")
    Admin.objects.filter(telegram_id=int(raw_owner_id), role="owner").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_owner_admin, unseed_owner_admin),
    ]
