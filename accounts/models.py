import secrets
from datetime import timedelta

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

USERNAME_REGEX = r"^[a-z0-9_]{3,32}$"

username_validator = RegexValidator(
    regex=USERNAME_REGEX,
    message="Username must be 3-32 characters: lowercase letters, digits, and underscores only.",
)


def invite_token_expiry():
    return timezone.now() + timedelta(hours=72)


def generate_token():
    return secrets.token_urlsafe(32)


# Excludes visually-ambiguous characters (0/O, 1/I/L).
ACCOUNT_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_account_id():
    raw = "".join(secrets.choice(ACCOUNT_ID_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def generate_account_number():
    """Cosmetic, bank-statement-style account number. Distinct from
    account_id, which gates access to the login page."""
    return "".join(secrets.choice("0123456789") for _ in range(12))


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, username, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        if not username:
            raise ValueError("Users must have a username.")
        email = self.normalize_email(email).lower()
        username = username.lower()
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_user(self, email, username, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, username, password, **extra_fields)

    def create_superuser(self, email, username, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, username, password, **extra_fields)


class User(AbstractUser):
    """End-user portal account. Login is by email; username is the
    admin-facing handle used in Telegram bot commands."""

    username = models.CharField(
        max_length=32,
        unique=True,
        validators=[username_validator],
        help_text="3-32 characters: lowercase letters, digits, underscores.",
    )
    email = models.EmailField(unique=True)
    account_id = models.CharField(
        max_length=11,
        unique=True,
        default=generate_account_id,
        editable=False,
        help_text="Shown to the user to gate access to the login page.",
    )
    account_number = models.CharField(
        max_length=12,
        unique=True,
        default=generate_account_number,
        editable=False,
        help_text="Cosmetic bank-style account number, unrelated to login.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "Admin",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_users",
        help_text="Which bot admin ran /create for this account, if known.",
    )
    # Optional delivery address (used for card shipping). Editable by both the
    # user (portal Profile page) and admins (bot /address) — last write wins.
    address_line1 = models.CharField(max_length=128, blank=True)
    address_line2 = models.CharField(max_length=128, blank=True)
    city = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=32, blank=True)
    postal_code = models.CharField(max_length=16, blank=True)

    @property
    def has_address(self):
        return bool(self.address_line1)

    def address_lines(self):
        if not self.has_address:
            return []
        lines = [self.address_line1]
        if self.address_line2:
            lines.append(self.address_line2)
        lines.append(f"{self.city}, {self.state} {self.postal_code}".strip(", "))
        return lines

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    objects = UserManager()

    def clean(self):
        if self.username:
            self.username = self.username.lower()
        if self.email:
            self.email = self.email.lower()
        super().clean()

    def __str__(self):
        return self.username


class Admin(models.Model):
    """A Telegram user authorized to operate the bot. Unrelated to the
    portal `User` model / Django auth — this is bot-side authorization only."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"

    class Avatar(models.TextChoices):
        WOMAN = "woman", "Woman"
        MAN = "man", "Man"

    telegram_id = models.BigIntegerField(primary_key=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.ADMIN)
    display_name = models.CharField(max_length=255, blank=True)
    # Specialist profile: shown on the portal dashboard of every user this
    # admin created. Managed by the admin themselves via the bot's /specialist.
    first_name = models.CharField(max_length=64, blank=True)
    last_name = models.CharField(max_length=64, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    avatar = models.CharField(max_length=8, choices=Avatar.choices, default=Avatar.WOMAN)
    created_at = models.DateTimeField(auto_now_add=True)
    portal_login = models.OneToOneField(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="admin_profile",
        help_text="Django staff account this admin uses to log into /staff-admin/, if any.",
    )

    def __str__(self):
        return self.display_name or str(self.telegram_id)


class LedgerEntry(models.Model):
    """A single, immutable balance change. A user's balance is always the
    SUM of their entries — never store a mutable balance column."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="ledger_entries")
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, help_text="Positive = credit, negative = debit."
    )
    reason = models.TextField()
    authorized_by = models.BigIntegerField(help_text="Admin's telegram_id at time of write.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Ledger entries"
        constraints = [
            models.CheckConstraint(
                check=~models.Q(amount=0),
                name="ledger_amount_nonzero",
            ),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.amount:+,.2f}"


class InviteToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="invite_tokens")
    token = models.CharField(max_length=64, unique=True, default=generate_token)
    expires_at = models.DateTimeField(default=invite_token_expiry)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        return self.used_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"invite:{self.user.username}"


def generate_request_reference():
    return "".join(secrets.choice(ACCOUNT_ID_ALPHABET) for _ in range(6))


class ServiceRequest(models.Model):
    """A user-submitted transfer or external-deposit request. Purely a review
    queue: creating, approving, or declining one NEVER touches the ledger —
    credits still only move through the bot's /credit and /debit commands."""

    class Kind(models.TextChoices):
        TRANSFER = "transfer", "Transfer"
        DEPOSIT = "deposit", "External deposit"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    reference = models.CharField(max_length=12, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="service_requests")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    recipient = models.CharField(max_length=64, blank=True, help_text="Transfer: recipient account number.")
    routing_number = models.CharField(max_length=9, blank=True, help_text="Transfer: recipient bank routing number.")
    recipient_bank = models.CharField(max_length=128, blank=True, help_text="Transfer: bank detected from the routing number, if known.")
    platform = models.CharField(max_length=128, blank=True, help_text="Deposit: source platform.")
    external_ref = models.CharField(max_length=128, blank=True, help_text="Deposit: external reference ID.")
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.BigIntegerField(
        null=True, blank=True, help_text="Admin's telegram_id at time of decision."
    )

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.reference:
            prefix = "TR" if self.kind == self.Kind.TRANSFER else "DP"
            self.reference = f"{prefix}-{generate_request_reference()}"
            while ServiceRequest.objects.filter(reference=self.reference).exists():
                self.reference = f"{prefix}-{generate_request_reference()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} ({self.user.username}, {self.status})"


class CardApplication(models.Model):
    """Progress tracker for a user's credits-card application. Stage states are
    set exclusively by bot admins (/card command) — the portal only displays
    them. No self-service state changes exist."""

    class DocsStatus(models.TextChoices):
        RECEIVED = "received", "Received"
        MISSING = "missing", "Missing / incomplete"

    class ReviewStatus(models.TextChoices):
        IN_REVIEW = "in_review", "In review"
        REVIEWED = "reviewed", "Reviewed"

    class ResultStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        RESUBMIT = "resubmit", "Needs resubmission"
        INFO_NEEDED = "info_needed", "Additional info requested"

    class ProductionStatus(models.TextChoices):
        WAITING = "waiting", "Waiting"
        IN_PROGRESS = "in_progress", "In progress"
        READY = "ready", "Ready"

    class ShippingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SHIPPED = "shipped", "Shipped"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="card_application")
    docs_status = models.CharField(max_length=10, choices=DocsStatus.choices, default=DocsStatus.RECEIVED)
    review_status = models.CharField(max_length=10, choices=ReviewStatus.choices, default=ReviewStatus.IN_REVIEW)
    result_status = models.CharField(max_length=12, choices=ResultStatus.choices, default=ResultStatus.PENDING)
    production_status = models.CharField(max_length=12, choices=ProductionStatus.choices, default=ProductionStatus.WAITING)
    shipping_status = models.CharField(max_length=10, choices=ShippingStatus.choices, default=ShippingStatus.PENDING)
    tracking_number = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"card:{self.user.username}"


class PortalMessage(models.Model):
    """A message between a portal user and their admins. User-sent messages are
    pushed to the admin's Telegram; admin replies (sent via the bot) appear on
    the user's Messages page."""

    class Sender(models.TextChoices):
        USER = "user", "User"
        ADMIN = "admin", "Admin"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="portal_messages")
    sender = models.CharField(max_length=5, choices=Sender.choices)
    body = models.TextField(max_length=2000)
    sent_by_admin = models.BigIntegerField(
        null=True, blank=True, help_text="Admin's telegram_id when sender is admin."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.user.username}/{self.sender}@{self.created_at:%Y-%m-%d %H:%M}"


class ProcessedUpdate(models.Model):
    """Dedupe marker for inbound Telegram updates. Created inside the same
    transaction as the effect it guards, so a redelivered update either
    finds this row already committed (no-op) or finds nothing and the
    prior attempt's effect was rolled back too (safe to retry)."""

    update_id = models.BigIntegerField(primary_key=True)
    processed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.update_id)
