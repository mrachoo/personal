from django.db import migrations, models

import accounts.models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_backfill_account_number"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="account_number",
            field=models.CharField(
                max_length=12,
                unique=True,
                default=accounts.models.generate_account_number,
                editable=False,
                help_text="Cosmetic bank-style account number, unrelated to login.",
            ),
        ),
    ]
