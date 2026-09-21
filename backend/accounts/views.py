from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Proxy, TelegramAccount
from .serializers import ProxySerializer, TelegramAccountSerializer
from .services import registry
from .services.managed import gw_result


class ProxyViewSet(viewsets.ModelViewSet):
    serializer_class = ProxySerializer
    queryset = Proxy.objects.all()


class TelegramAccountViewSet(viewsets.ModelViewSet):
    serializer_class = TelegramAccountSerializer

    def get_queryset(self):
        return TelegramAccount.objects.visible_to(self.request.user)

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(user=None if user.is_superuser else user)

    @action(detail=True, methods=["post"])
    def send_code(self, request, pk=None):
        account = self.get_object()
        return Response(gw_result(lambda: registry.get(account.id).send_code()))

    @action(detail=True, methods=["post"])
    def verify_code(self, request, pk=None):
        account = self.get_object()
        res = gw_result(lambda: registry.get(account.id).verify_code(
            request.data.get("code", ""), request.data.get("password")))
        return Response(res)
