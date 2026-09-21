"""Простий інтерфейс /app/ — карта екранів у docs/simple-ui-design.md §2."""
from django.urls import path

from . import views

app_name = "simpleui"

urlpatterns = [
    path("", views.sections, name="sections"),
    path("<int:task_id>/", views.section, name="section"),
    path("<int:task_id>/event/<int:event_id>/", views.event, name="event"),
    path("<int:task_id>/settings/", views.settings_page, name="settings"),
    path("<int:task_id>/collect/", views.collect, name="collect"),
]
