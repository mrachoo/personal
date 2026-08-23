from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("staff-admin/", admin.site.urls),
    path("", include("accounts.urls")),
]
