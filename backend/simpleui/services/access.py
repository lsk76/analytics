"""Видимість задач у простому інтерфейсі — ТОЙ САМИЙ критерій, що в адмінці
(EventAdmin.get_queryset) і в MCP (mcp_api.common.scope_tasks): суперюзер
бачить усе, інші — лише задачі, де owner == користувач."""
from analysis.models import AnalysisTask, Event


def visible_tasks(user):
    qs = AnalysisTask.objects.all()
    if not user.is_superuser:
        qs = qs.filter(owner=user)
    return qs


def approved_events(task):
    """Події, які бачить адмінка за замовчуванням: лише «Схвалено»."""
    return Event.objects.filter(task=task, review_status=Event.REVIEW_APPROVED)
