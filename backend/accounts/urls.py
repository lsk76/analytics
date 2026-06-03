from rest_framework.routers import DefaultRouter

from .views import TelegramAccountViewSet, ProxyViewSet

router = DefaultRouter()
router.register("accounts", TelegramAccountViewSet, basename="account")
router.register("proxies", ProxyViewSet, basename="proxy")

urlpatterns = router.urls
