from django.urls import path

from . import views

app_name = "mcpauth"

urlpatterns = [
    path("consent/", views.consent, name="consent"),
]
