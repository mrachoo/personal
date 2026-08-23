from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordChangeView,
    PasswordResetConfirmView,
)
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from django.shortcuts import get_object_or_404

from django.http import JsonResponse

from bot.notify import (
    notify_address_changed,
    notify_new_portal_message,
    notify_new_service_request,
    notify_password_changed,
)

from django.urls import reverse_lazy

from .amounts import parse_money
from .forms import StyledPasswordChangeForm, StyledSetPasswordForm
from .models import Admin, CardApplication, InviteToken, PortalMessage, ServiceRequest, User
from .routing_banks import lookup_bank

PAGE_SIZE = 20
ACCOUNT_GATE_SESSION_KEY = "account_gate_passed"


def _login_url(next_url=None):
    url = reverse("login")
    if next_url:
        url += "?" + urlencode({"next": next_url})
    return url


class PortalLoginView(LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated and not request.session.get(ACCOUNT_GATE_SESSION_KEY):
            next_url = request.GET.get("next") or request.POST.get("next")
            gate_url = reverse("account_gate")
            if next_url:
                gate_url += "?" + urlencode({"next": next_url})
            return redirect(gate_url)
        return super().dispatch(request, *args, **kwargs)


class PortalLogoutView(LogoutView):
    next_page = "login"

    def dispatch(self, request, *args, **kwargs):
        request.session.pop(ACCOUNT_GATE_SESSION_KEY, None)
        return super().dispatch(request, *args, **kwargs)


class PortalPasswordResetConfirmView(PasswordResetConfirmView):
    def form_valid(self, form):
        response = super().form_valid(form)
        notify_password_changed(self.user, "the user reset their password via the forgot-password link")
        return response


class PortalPasswordChangeView(PasswordChangeView):
    template_name = "accounts/password_change.html"
    form_class = StyledPasswordChangeForm
    success_url = reverse_lazy("profile")

    def form_valid(self, form):
        response = super().form_valid(form)
        notify_password_changed(self.request.user, "the user changed their password from the portal")
        messages.success(self.request, "Your password has been updated.")
        return response


def account_gate(request):
    next_url = request.GET.get("next") or request.POST.get("next", "")
    if request.session.get(ACCOUNT_GATE_SESSION_KEY):
        return redirect(_login_url(next_url))

    error = None
    if request.method == "POST":
        entered = request.POST.get("account_id", "").strip().upper()
        user = User.objects.filter(account_id=entered).first()
        if user is not None and user.is_active:
            request.session[ACCOUNT_GATE_SESSION_KEY] = True
            return redirect(_login_url(next_url))
        error = "That Account ID wasn't found. Please check and try again."
    return render(request, "accounts/account_gate.html", {"error": error, "next": next_url})


def _balance(user):
    return user.ledger_entries.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _specialist_for(user):
    """The admin whose specialist profile appears on this user's dashboard:
    the admin who created them, falling back to an owner. Only returned once
    the admin has set at least a name via /specialist."""
    candidates = [user.created_by] if user.created_by is not None else []
    candidates += list(Admin.objects.filter(role=Admin.Role.OWNER))
    for admin in candidates:
        if admin is not None and admin.first_name:
            return admin
    return None


@login_required
def dashboard(request):
    user = request.user
    entries = user.ledger_entries.all()
    paginator = Paginator(entries, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    stats = entries.aggregate(
        credits_in=Sum("amount", filter=Q(amount__gt=0)),
        credits_out=Sum("amount", filter=Q(amount__lt=0)),
        tx_count=Count("id"),
    )
    return render(
        request,
        "accounts/dashboard.html",
        {
            "balance": _balance(user),
            "page_obj": page_obj,
            "credits_in": stats["credits_in"] or 0,
            "credits_out": -(stats["credits_out"] or 0),
            "tx_count": stats["tx_count"],
            "specialist": _specialist_for(user),
        },
    )


@login_required
def profile(request):
    address_errors = {}
    if request.method == "POST":
        user = request.user
        fields = {
            f: request.POST.get(f, "").strip()
            for f in ("address_line1", "address_line2", "city", "state", "postal_code")
        }
        if not any(fields.values()):
            # Everything blank = remove the address.
            for f in fields:
                setattr(user, f, "")
            user.save(update_fields=list(fields))
            notify_address_changed(user)
            messages.success(request, "Your delivery address has been removed.")
            return redirect("profile")
        for f, label in (
            ("address_line1", "street address"),
            ("city", "city"),
            ("state", "state"),
            ("postal_code", "ZIP code"),
        ):
            if not fields[f]:
                address_errors[f] = f"Enter your {label}."
        if not address_errors:
            for f, value in fields.items():
                setattr(user, f, value)
            user.save(update_fields=list(fields))
            notify_address_changed(user)
            messages.success(request, "Your delivery address has been saved.")
            return redirect("profile")
    return render(request, "accounts/profile.html", {"address_errors": address_errors})


@login_required
def accounts_page(request):
    return render(request, "accounts/accounts_page.html", {"balance": _balance(request.user)})


@login_required
def transactions(request):
    paginator = Paginator(request.user.ledger_entries.all(), PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "accounts/transactions.html", {"page_obj": page_obj})


@login_required
def messages_page(request):
    error = None
    if request.method == "POST":
        body = request.POST.get("body", "").strip()
        if not body:
            error = "Write a message before sending."
        elif len(body) > 2000:
            error = "Messages can be at most 2,000 characters."
        else:
            msg = PortalMessage.objects.create(
                user=request.user, sender=PortalMessage.Sender.USER, body=body
            )
            notify_new_portal_message(msg)
            messages.success(request, "Message sent to your administrator.")
            return redirect("messages")

    thread = list(request.user.portal_messages.all())
    # Opening the thread clears the unread badge.
    request.user.portal_messages.filter(
        sender=PortalMessage.Sender.ADMIN, read_at__isnull=True
    ).update(read_at=timezone.now())
    admin_ids = {m.sent_by_admin for m in thread if m.sent_by_admin}
    admins = {a.telegram_id: a for a in Admin.objects.filter(telegram_id__in=admin_ids)}
    for m in thread:
        a = admins.get(m.sent_by_admin)
        m.admin_name = (
            f"{a.first_name} {a.last_name}".strip() if a and a.first_name else "Administrator"
        )
    return render(request, "accounts/messages.html", {"thread": thread, "error": error})


def _parse_request_amount(request, errors):
    amount = parse_money(request.POST.get("amount", ""))
    if amount is None:
        errors["amount"] = "Enter a valid amount, e.g. 500 or 500.43."
        return None
    return amount


@login_required
def routing_lookup(request):
    number = request.GET.get("number", "").strip()
    valid, bank = lookup_bank(number)
    return JsonResponse({"valid": valid, "bank": bank})


@login_required
def transfers(request):
    errors = {}
    if request.method == "POST":
        recipient = request.POST.get("recipient", "").strip()
        routing_number = request.POST.get("routing_number", "").strip()
        note = request.POST.get("note", "").strip()
        amount = _parse_request_amount(request, errors)
        routing_valid, recipient_bank = lookup_bank(routing_number)
        if not routing_number:
            errors["routing_number"] = "Enter the recipient's routing number."
        elif not routing_valid:
            errors["routing_number"] = "That isn't a valid routing number."
        if not recipient:
            errors["recipient"] = "Enter the recipient's account number."
        elif not recipient.isdigit():
            errors["recipient"] = "Account numbers contain digits only."
        if amount is not None and amount > _balance(request.user):
            errors["amount"] = "Amount exceeds your available balance."
        if not errors:
            sr = ServiceRequest.objects.create(
                user=request.user,
                kind=ServiceRequest.Kind.TRANSFER,
                amount=amount,
                recipient=recipient,
                routing_number=routing_number,
                recipient_bank=recipient_bank,
                note=note,
            )
            notify_new_service_request(sr)
            return redirect("request_detail", reference=sr.reference)
    recent = request.user.service_requests.filter(kind=ServiceRequest.Kind.TRANSFER)[:5]
    return render(
        request,
        "accounts/transfers.html",
        {"balance": _balance(request.user), "errors": errors, "recent_requests": recent},
    )


@login_required
def external_deposits(request):
    errors = {}
    if request.method == "POST":
        platform = request.POST.get("platform", "").strip()
        external_ref = request.POST.get("reference", "").strip()
        amount = _parse_request_amount(request, errors)
        if not platform:
            errors["platform"] = "Enter the source platform."
        if not errors:
            sr = ServiceRequest.objects.create(
                user=request.user,
                kind=ServiceRequest.Kind.DEPOSIT,
                amount=amount,
                platform=platform,
                external_ref=external_ref,
            )
            notify_new_service_request(sr)
            return redirect("request_detail", reference=sr.reference)
    recent = request.user.service_requests.filter(kind=ServiceRequest.Kind.DEPOSIT)[:5]
    return render(
        request,
        "accounts/external_deposits.html",
        {"balance": _balance(request.user), "errors": errors, "recent_requests": recent},
    )


@login_required
def request_detail(request, reference):
    sr = get_object_or_404(ServiceRequest, reference=reference, user=request.user)
    return render(request, "accounts/request_detail.html", {"sr": sr})


CARD_STAGE_STATES = {
    # stage field -> state -> (chip label, color, guidance note or None)
    "docs_status": {
        "received": ("Received", "green", None),
        "missing": ("Missing / incomplete", "red",
                    "A required document is missing or incomplete. Contact your administrator to provide it."),
    },
    "review_status": {
        "in_review": ("In review", "amber", None),
        "reviewed": ("Reviewed", "green", None),
    },
    "result_status": {
        "pending": ("Pending", "gray", None),
        "verified": ("Verified", "green", None),
        "resubmit": ("Needs resubmission", "red",
                     "One or more documents were flagged. Please resubmit them to your administrator."),
        "info_needed": ("Additional info requested", "amber",
                        "Your administrator needs more information before verification can complete."),
    },
    "production_status": {
        "waiting": ("Waiting", "gray", None),
        "in_progress": ("In progress", "amber", None),
        "ready": ("Ready", "green", None),
    },
    "shipping_status": {
        "pending": ("Pending", "gray", None),
        "shipped": ("Shipped", "green", None),
    },
}

CARD_STAGES = [
    ("docs_status", "Account verified", "Your account and required documents on file."),
    ("review_status", "Under review", "Our team verifies the submitted documents."),
    ("result_status", "Verification result", "The outcome of document verification."),
    ("production_status", "Card production", "Your card is personalized and printed once verification clears."),
    ("shipping_status", "Shipping", "Your card is dispatched to the address on file."),
]


@login_required
def card(request):
    app = CardApplication.objects.filter(user=request.user).first()
    stages = []
    completed = 0
    current_index = None
    if app is not None:
        for i, (field, title, desc) in enumerate(CARD_STAGES):
            label, color, note = CARD_STAGE_STATES[field][getattr(app, field)]
            if color == "green":
                completed += 1
            elif current_index is None:
                current_index = i
            stages.append({
                "number": i + 1,
                "title": title,
                "desc": desc,
                "label": label,
                "color": color,
                "note": note,
                "tracking": app.tracking_number if field == "shipping_status" else "",
            })
        for i, stage in enumerate(stages):
            stage["is_current"] = i == current_index
    return render(
        request,
        "accounts/card.html",
        {
            "app": app,
            "stages": stages,
            "completed": completed,
            "total": len(CARD_STAGES),
            "percent": int(completed * 100 / len(CARD_STAGES)),
            "current_title": stages[current_index]["title"] if current_index is not None and stages else None,
        },
    )


def invite_accept(request, token):
    invite = InviteToken.objects.filter(token=token).select_related("user").first()
    if invite is None or not invite.is_valid():
        return render(request, "accounts/invite_invalid.html", status=404)

    user = invite.user
    if not user.is_active:
        return render(request, "accounts/invite_invalid.html", status=404)

    if request.method == "POST":
        form = StyledSetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            invite.used_at = timezone.now()
            invite.save(update_fields=["used_at"])
            notify_password_changed(user, "the user set their initial password via their invite link")
            auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, "Your password has been set. Welcome!")
            return redirect("dashboard")
    else:
        form = StyledSetPasswordForm(user)
    return render(request, "accounts/invite_accept.html", {"form": form, "invite_user": user})
