from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import EventViewSet, ChannelViewSet, AnalysisTaskViewSet, TagViewSet
from .views_autocomplete import channel_autocomplete

router = DefaultRouter()
router.register("tasks", AnalysisTaskViewSet, basename="task")
router.register("events", EventViewSet, basename="event")
router.register("channels", ChannelViewSet, basename="channel")
router.register("tags", TagViewSet, basename="tag")

urlpatterns = router.urls + [
    path("channel-autocomplete/", channel_autocomplete, name="channel-autocomplete"),
]
