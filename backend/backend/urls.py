from django.contrib import admin
from django.urls import path, include

admin.site.site_header = "Delta4Аналітик"
admin.site.site_title = "Delta4Аналітик"
admin.site.index_title = "Дослідження"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("analysis.urls")),
    path("api/accounts/", include("accounts.urls")),
    path("mcp/", include("mcpauth.urls")),      # згода на доступ до MCP-сервера
]
