from rest_framework import viewsets, filters

from .models import AnalysisTask, Channel, Nationality, ConflictType, Event
from .serializers import (
    AnalysisTaskSerializer, ChannelSerializer, NationalitySerializer,
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
        qs = Event.objects.prefetch_related("sides", "posts").select_related("conflict_type")
        task = self.request.query_params.get("task")
        if task:
            qs = qs.filter(task__slug=task)
        if self.request.query_params.get("corroborated") == "1":
            qs = qs.filter(is_corroborated=True)
        return qs


class ChannelViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Channel.objects.all()
    serializer_class = ChannelSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["username", "title", "inferred_region"]


class NationalityViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Nationality.objects.all()
    serializer_class = NationalitySerializer


class ConflictTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ConflictType.objects.all()
    serializer_class = ConflictTypeSerializer
