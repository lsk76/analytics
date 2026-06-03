from django.contrib import admin
from django.urls import path, include

admin.site.site_header = "Аналіз подій у Telegram"
admin.site.site_title = "Аналіз подій"
admin.site.index_title = "Адміністрування"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("analysis.urls")),
    path("api/accounts/", include("accounts.urls")),
]
