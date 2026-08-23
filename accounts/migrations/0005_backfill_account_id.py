from django.db import migrations

from accounts.models import generate_account_id


def backfill_account_id(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.filter(account_id__isnull=True):
        while True:
            candidate = generate_account_id()
            if not User.objects.filter(account_id=candidate).exists():
                break
        user.account_id = candidate
        user.save(update_fields=["account_id"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_user_account_id_nullable"),
    ]

    operations = [
        migrations.RunPython(backfill_account_id, noop),
    ]
