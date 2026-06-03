from rest_framework import serializers

from .models import AnalysisTask, Channel, Nationality, ConflictType, Post, Event


class AnalysisTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisTask
        fields = "__all__"


class ChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Channel
        fields = "__all__"


class NationalitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Nationality
        fields = ["id", "name", "family", "region_hint"]


class ConflictTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConflictType
        fields = ["id", "name"]


class PostLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Post
        fields = ["url", "channel_name", "posted_at"]


class EventSerializer(serializers.ModelSerializer):
    sides = NationalitySerializer(many=True, read_only=True)
    conflict_type = ConflictTypeSerializer(read_only=True)
    region_subject = serializers.StringRelatedField()
    posts = PostLinkSerializer(many=True, read_only=True)   # requirement #2: post links

    class Meta:
        model = Event
        fields = ["id", "event_date", "region_subject", "settlement", "region",
                  "conflict_type", "sides", "summary", "post_count", "is_corroborated", "posts"]
