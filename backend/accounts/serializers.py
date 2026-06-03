from rest_framework import serializers

from .models import Proxy, TelegramAccount


class ProxySerializer(serializers.ModelSerializer):
    class Meta:
        model = Proxy
        fields = ["id", "proxy_string", "proxy_type", "is_active", "is_working", "fail_count"]


class TelegramAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = TelegramAccount
        fields = ["id", "name", "phone_number", "api_id", "api_hash",
                  "is_authenticated", "is_active", "proxy", "last_used_at", "created_at"]
        extra_kwargs = {"api_hash": {"write_only": True}}
