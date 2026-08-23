import secrets

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Admin, User


class Command(BaseCommand):
    help = (
        "Create a Django staff login for an existing bot Admin (identified by "
        "Telegram ID), so they can log into /staff-admin/ scoped to the portal "
        "users they've created via the bot."
    )

    def add_arguments(self, parser):
        parser.add_argument("telegram_id", type=int)
        parser.add_argument("email")
        parser.add_argument(
            "--username",
            help="Login username for the staff account. Defaults to admin_<telegram_id>.",
        )

    def handle(self, *args, **options):
        telegram_id = options["telegram_id"]
        email = options["email"].strip().lower()
        username = options["username"] or f"admin_{telegram_id}"

        try:
            admin = Admin.objects.get(telegram_id=telegram_id)
        except Admin.DoesNotExist:
            raise CommandError(
                f"No bot Admin with telegram_id={telegram_id}. Add them via /addadmin first."
            )

        if admin.portal_login_id is not None:
            raise CommandError(
                f"{admin} already has a linked staff login: {admin.portal_login}."
            )

        password = secrets.token_urlsafe(18)
        user = User.objects.create_user(
            email=email,
            username=username,
            password=password,
            is_staff=True,
            is_superuser=False,
        )
        admin.portal_login = user
        admin.save(update_fields=["portal_login"])

        self.stdout.write(self.style.SUCCESS(f"Created staff login for {admin}:"))
        self.stdout.write(f"  URL:      /staff-admin/")
        self.stdout.write(f"  Email:    {email}")
        self.stdout.write(f"  Password: {password}")
        self.stdout.write("Give these to the admin directly; they aren't stored anywhere else.")
