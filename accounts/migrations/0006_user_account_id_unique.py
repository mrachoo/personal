from django.db import migrations, models

import accounts.models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_backfill_account_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="account_id",
            field=models.CharField(
                max_length=11,
                unique=True,
                default=accounts.models.generate_account_id,
                editable=False,
                help_text="Shown to the user to gate access to the login page.",
            ),
        ),
    ]
