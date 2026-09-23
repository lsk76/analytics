"""Людська частина OAuth: логін наявним акаунтом і згода на доступ.

MCP-процес не має ні сесій, ні шаблонів — він лише кладе `McpAuthRequest` і
відправляє браузер сюди. Тут людина входить звичайним акаунтом адмінки, бачить,
який клієнт і по які права прийшов, і або видає код, або відмовляє.
"""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import McpAuthCode, McpAuthRequest, McpRole, new_secret, sha256
from .policy import CODE_TTL, SCOPE_LABELS, granted_scopes


def _redirect_with(req: McpAuthRequest, **params) -> HttpResponseRedirect:
    sep = "&" if "?" in req.redirect_uri else "?"
    if req.state:
        params["state"] = req.state
    return HttpResponseRedirect(f"{req.redirect_uri}{sep}{urlencode(params)}")


@login_required
@require_http_methods(["GET", "POST"])
def consent(request):
    key = request.GET.get("req") or request.POST.get("req") or ""
    req = McpAuthRequest.objects.select_related("client").filter(key=key).first()
    if not req or not req.is_usable:
        return render(request, "mcpauth/consent.html",
                      {"error": "Запит авторизації не знайдено або він застарів "
                                "(дійсний 15 хвилин). Почни під'єднання в клієнті заново."},
                      status=400)

    role = McpRole.objects.filter(user=request.user, is_active=True).first()
    # req.client.scope — те, що ми видали клієнту при реєстрації: повтор цього
    # рядка не є свідомим звуженням (див. policy.granted_scopes)
    scopes = granted_scopes(request.user, req.scopes, (req.client.scope or "").split())
    if not role or not scopes:
        return render(request, "mcpauth/consent.html", {
            "req": req, "no_role": True,
            "error": f"У користувача «{request.user.username}» немає активної ролі MCP. "
                     "Попроси власника видати її в адмінці: Доступ до MCP → Ролі у MCP."},
            status=403)

    if request.method == "POST":
        req.completed_at = timezone.now()
        req.save(update_fields=["completed_at"])
        if request.POST.get("action") != "allow":
            return _redirect_with(req, error="access_denied",
                                  error_description="user refused")
        code = new_secret()
        McpAuthCode.objects.create(
            code_hash=sha256(code), client=req.client, user=request.user,
            redirect_uri=req.redirect_uri,
            redirect_uri_provided_explicitly=req.redirect_uri_provided_explicitly,
            code_challenge=req.code_challenge, scopes=scopes, resource=req.resource,
            expires_at=timezone.now() + CODE_TTL)
        return _redirect_with(req, code=code)

    return render(request, "mcpauth/consent.html", {
        "req": req, "role": role,
        "grants": [(s, SCOPE_LABELS.get(s, s)) for s in scopes],
        "asked": req.scopes,
        "narrowed": [s for s in (req.scopes or []) if s not in scopes],
    })
