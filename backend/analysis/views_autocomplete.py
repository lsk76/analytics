"""Custom Select2 autocomplete endpoints for admin filters (indirect relations)."""
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import JsonResponse

from .models import Channel


@staff_member_required
def channel_autocomplete(request):
    """Channels for the Event 'канал' filter (Event -> posts -> channel)."""
    term = (request.GET.get("term") or "").strip()
    qs = Channel.objects.all()
    if term:
        qs = qs.filter(Q(username__icontains=term) | Q(title__icontains=term))
    qs = qs.order_by("-subscribers")[:20]
    results = [{"id": c.id, "text": (c.username or c.title or f"#{c.tg_id}")} for c in qs]
    return JsonResponse({"results": results, "pagination": {"more": False}})


@staff_member_required
def source_autocomplete(request):
    """Джерела інформпростору для фільтра «Джерело» у подіях (Event -> posts -> source)."""
    from .models import Source
    term = (request.GET.get("term") or "").strip()
    qs = Source.objects.select_related("channel")
    if term:
        qs = qs.filter(Q(channel__title__icontains=term) | Q(channel__url__icontains=term))
    qs = qs.order_by("channel__title")[:20]
    return JsonResponse({"results": [{"id": s.id, "text": s.name or s.url} for s in qs],
                         "pagination": {"more": False}})
