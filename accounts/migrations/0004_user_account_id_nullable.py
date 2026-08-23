from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_processedupdate"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="account_id",
            field=models.CharField(max_length=11, null=True, editable=False),
        ),
    ]
