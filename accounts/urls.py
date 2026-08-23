from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from .forms import StyledPasswordResetForm, StyledSetPasswordForm

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("profile/", views.profile, name="profile"),
    path("accounts/", views.accounts_page, name="accounts"),
    path("transactions/", views.transactions, name="transactions"),
    path("messages/", views.messages_page, name="messages"),
    path("transfers/", views.transfers, name="transfers"),
    path("deposits/", views.external_deposits, name="external_deposits"),
    path("requests/<str:reference>/", views.request_detail, name="request_detail"),
    path("card/", views.card, name="card"),
    path("password-change/", views.PortalPasswordChangeView.as_view(), name="password_change"),
    path("api/routing-lookup/", views.routing_lookup, name="routing_lookup"),
    path("access/", views.account_gate, name="account_gate"),
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.PortalLogoutView.as_view(), name="logout"),
    path("invite/<str:token>/", views.invite_accept, name="invite_accept"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
            form_class=StyledPasswordResetForm,
            success_url="/password-reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        views.PortalPasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            form_class=StyledSetPasswordForm,
            success_url="/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(template_name="registration/password_reset_complete.html"),
        name="password_reset_complete",
    ),
]
