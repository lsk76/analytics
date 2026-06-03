from rest_framework import viewsets, filters

from .models import AnalysisTask, Channel, Tag, ConflictType, Event
from .serializers import (
    AnalysisTaskSerializer, ChannelSerializer, TagSerializer,
    ConflictTypeSerializer, EventSerializer,
)


class AnalysisTaskViewSet(viewsets.ModelViewSet):
    queryset = AnalysisTask.objects.all()
    serializer_class = AnalysisTaskSerializer


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = EventSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["summary", "region"]
    ordering_fields = ["event_date", "post_count"]

    def get_queryset(self):
        qs = Event.objects.prefetch_related("tags", "posts").select_related("conflict_type")
        task = self.request.query_params.get("task")
        if task:
            qs = qs.filter(task__slug=task)
        if self.request.query_params.get("corroborated") == "1":
            qs = qs.filter(is_corroborated=True)
        tag_cat = self.request.query_params.get("tag_category")
        if tag_cat:
            qs = qs.filter(tags__category=tag_cat).distinct()
        return qs


class ChannelViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Channel.objects.all()
    serializer_class = ChannelSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["username", "title", "inferred_region"]


class TagViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TagSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name"]

    def get_queryset(self):
        qs = Tag.objects.all()
        cat = self.request.query_params.get("category")
        return qs.filter(category=cat) if cat else qs


class ConflictTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ConflictType.objects.all()
    serializer_class = ConflictTypeSerializer
