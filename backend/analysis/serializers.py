from rest_framework import serializers

from .models import AnalysisTask, Channel, Tag, ConflictType, Post, Event


class AnalysisTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisTask
        fields = "__all__"


class ChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Channel
        fields = "__all__"


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name", "category"]


class ConflictTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConflictType
        fields = ["id", "name"]


class PostLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Post
        fields = ["url", "channel_name", "posted_at"]


class EventSerializer(serializers.ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)
    conflict_type = ConflictTypeSerializer(read_only=True)
    region_subject = serializers.StringRelatedField()
    posts = PostLinkSerializer(many=True, read_only=True)   # requirement #2: post links

    class Meta:
        model = Event
        fields = ["id", "event_date", "region_subject", "settlement", "region",
                  "conflict_type", "tags", "summary", "post_count", "is_corroborated", "posts"]
