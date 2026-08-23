from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_admin_portal_login_user_created_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="account_number",
            field=models.CharField(max_length=12, null=True, editable=False),
        ),
    ]
