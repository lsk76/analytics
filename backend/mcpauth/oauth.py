"""OAuth-провайдер MCP на Django-моделях.

Розкладка ролей між процесами:

* **MCP-процес** тримає самі OAuth-ендпоінти (`/authorize`, `/token`,
  `/register`, метадані) — їх піднімає SDK, щойно передати цей провайдер;
* **Django-веб** тримає ЛЮДСЬКУ частину: логін (уже наявні користувачі) і
  сторінку згоди. У MCP-процесі сесій немає, тож питати людину там нічим.

Місток між ними — таблиці: провайдер кладе `McpAuthRequest`, сторінка згоди
створює `McpAuthCode`, провайдер обмінює його на токени.

Скоупи НІКОЛИ не перевищують права користувача в адмінці: клієнт може
попросити `mcp:admin`, але читач отримає лише `mcp:read`.
"""
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone
from mcp.server.auth.provider import (AccessToken, AuthorizationCode,
                                      AuthorizationParams, OAuthAuthorizationServerProvider,
                                      RefreshToken)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .models import (McpAuthCode, McpAuthRequest, McpClient, McpRole, McpToken,
                     new_secret, sha256)
from .policy import (ACCESS_TTL, ALL_SCOPES, REFRESH_TTL, REQUEST_TTL,  # noqa: F401
                     SCOPE_READ, granted_scopes, scope_summary)


def consent_url(key: str) -> str:
    base = getattr(settings, "MCP_CONSENT_BASE_URL", "").rstrip("/")
    return f"{base}/mcp/consent/?req={key}"


def _client_to_sdk(row: McpClient) -> OAuthClientInformationFull:
    """Рядок БД → модель SDK.

    `client_secret` віддаємо як є: SDK звіряє його прямим порівнянням у
    `ClientAuthenticator`. Спроба віддати None при виданому секреті дає
    401 «registered for secret-based authentication but has no stored secret».
    """
    return OAuthClientInformationFull(
        client_id=row.client_id,
        client_secret=row.client_secret or None,
        client_name=row.name or row.client_id,
        redirect_uris=row.redirect_uris or [],
        grant_types=row.grant_types or ["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=row.scope or SCOPE_READ,
        token_endpoint_auth_method="client_secret_post" if row.client_secret else "none",
    )


class DjangoOAuthProvider(OAuthAuthorizationServerProvider):
    """Реалізація контракту SDK поверх таблиць mcpauth."""

    # ---------------------------------------------------------------- клієнти
    async def get_client(self, client_id: str):
        return await sync_to_async(self._get_client)(client_id)

    def _get_client(self, client_id):
        row = McpClient.objects.filter(client_id=client_id, is_active=True).first()
        return _client_to_sdk(row) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await sync_to_async(self._register_client)(client_info)

    def _register_client(self, info):
        McpClient.objects.update_or_create(
            client_id=info.client_id,
            defaults={
                "client_secret": info.client_secret or "",
                "name": info.client_name or "",
                "redirect_uris": [str(u) for u in (info.redirect_uris or [])],
                "scope": info.scope or "",
                "grant_types": list(info.grant_types or []),
            })

    # ------------------------------------------------------------ авторизація
    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        return await sync_to_async(self._authorize)(client, params)

    def _authorize(self, client, params):
        row = McpClient.objects.get(client_id=client.client_id)
        req = McpAuthRequest.objects.create(
            key=new_secret(24), client=row,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge or "",
            scopes=list(params.scopes or []),
            state=params.state or "",
            resource=params.resource or "",
            expires_at=timezone.now() + REQUEST_TTL)
        # людину відправляємо в Django: там сесія, логін і сторінка згоди
        return consent_url(req.key)

    async def load_authorization_code(self, client, authorization_code: str):
        return await sync_to_async(self._load_code)(client, authorization_code)

    def _load_code(self, client, code):
        row = (McpAuthCode.objects.select_related("client")
               .filter(code_hash=sha256(code), client__client_id=client.client_id).first())
        if not row or not row.is_usable:
            return None
        return AuthorizationCode(
            code=code, scopes=row.scopes, expires_at=row.expires_at.timestamp(),
            client_id=client.client_id, code_challenge=row.code_challenge,
            redirect_uri=row.redirect_uri,
            redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
            resource=row.resource or None, subject=row.user.username)

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        return await sync_to_async(self._exchange_code)(client, authorization_code)

    def _exchange_code(self, client, auth_code):
        row = (McpAuthCode.objects.select_related("user", "client")
               .filter(code_hash=sha256(auth_code.code)).first())
        if not row or not row.is_usable:
            raise ValueError("код авторизації недійсний або вже використаний")
        row.used_at = timezone.now()
        row.save(update_fields=["used_at"])
        return self._issue(row.user, row.client, row.scopes, row.resource)

    # ----------------------------------------------------------------- токени
    def _issue(self, user, client, scopes, resource="") -> OAuthToken:
        access, refresh = new_secret(), new_secret()
        now = timezone.now()
        McpToken.objects.create(token_hash=sha256(access), kind=McpToken.ACCESS,
                                client=client, user=user, scopes=list(scopes),
                                resource=resource or "", expires_at=now + ACCESS_TTL)
        McpToken.objects.create(token_hash=sha256(refresh), kind=McpToken.REFRESH,
                                client=client, user=user, scopes=list(scopes),
                                resource=resource or "", expires_at=now + REFRESH_TTL)
        return OAuthToken(access_token=access, refresh_token=refresh,
                          expires_in=int(ACCESS_TTL.total_seconds()),
                          scope=" ".join(scopes))

    async def load_access_token(self, token: str):
        return await sync_to_async(self._load_access)(token)

    def _load_access(self, token):
        row = (McpToken.objects.select_related("user", "client")
               .filter(token_hash=sha256(token), kind=McpToken.ACCESS).first())
        if not row or not row.is_valid:
            return None
        # роль могли забрати вже після видачі токена — перевіряємо щоразу
        role = McpRole.objects.filter(user=row.user, is_active=True).first()
        if not role:
            return None
        scopes = [s for s in row.scopes if s in role.scopes]
        McpToken.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
        return AccessToken(token=token, client_id=row.client.client_id, scopes=scopes,
                           expires_at=int(row.expires_at.timestamp()) if row.expires_at else None,
                           resource=row.resource or None, subject=row.user.username,
                           claims={"role": scope_summary(scopes)})

    async def load_refresh_token(self, client, refresh_token: str):
        return await sync_to_async(self._load_refresh)(client, refresh_token)

    def _load_refresh(self, client, token):
        row = (McpToken.objects.select_related("user", "client")
               .filter(token_hash=sha256(token), kind=McpToken.REFRESH,
                       client__client_id=client.client_id).first())
        if not row or not row.is_valid:
            return None
        return RefreshToken(token=token, client_id=client.client_id, scopes=row.scopes,
                            expires_at=int(row.expires_at.timestamp()) if row.expires_at else None,
                            resource=row.resource or None, subject=row.user.username)

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        return await sync_to_async(self._exchange_refresh)(client, refresh_token, scopes)

    def _exchange_refresh(self, client, refresh_token, scopes):
        row = (McpToken.objects.select_related("user", "client")
               .filter(token_hash=sha256(refresh_token.token), kind=McpToken.REFRESH).first())
        if not row or not row.is_valid:
            raise ValueError("refresh-токен недійсний")
        # ротація: старий refresh одразу вмирає, щоб украдений не жив 30 днів
        row.revoked_at = timezone.now()
        row.save(update_fields=["revoked_at"])
        asked = list(scopes) if scopes else row.scopes
        allowed = granted_scopes(row.user, asked, (row.client.scope or "").split())
        if not allowed:
            raise ValueError("у користувача немає активної ролі MCP")
        return self._issue(row.user, row.client, allowed, row.resource)

    async def revoke_token(self, token) -> None:
        await sync_to_async(self._revoke)(token)

    def _revoke(self, token):
        McpToken.objects.filter(token_hash=sha256(getattr(token, "token", str(token))),
                                revoked_at__isnull=True).update(revoked_at=timezone.now())

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise NotImplementedError("identity assertion не увімкнено")
