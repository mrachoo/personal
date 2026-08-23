from django.db import migrations

from accounts.models import generate_account_number


def backfill_account_number(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.filter(account_number__isnull=True):
        while True:
            candidate = generate_account_number()
            if not User.objects.filter(account_number=candidate).exists():
                break
        user.account_number = candidate
        user.save(update_fields=["account_number"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_user_account_number_nullable"),
    ]

    operations = [
        migrations.RunPython(backfill_account_number, noop),
    ]
