from rest_framework import serializers

from .models import AnalysisTask, Channel, Tag, Post, Event


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


class PostLinkSerializer(serializers.ModelSerializer):
    subscribers = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = ["url", "channel_name", "subscribers", "posted_at"]

    def get_subscribers(self, obj):
        return (obj.channel.subscribers or 0) if obj.channel_id else 0


class EventSerializer(serializers.ModelSerializer):
    tags = TagSerializer(many=True, read_only=True)
    region_subject = serializers.StringRelatedField()
    posts = serializers.SerializerMethodField()   # top-3 links by subscribers (desc)

    class Meta:
        model = Event
        fields = ["id", "event_date", "region_subject", "settlement", "region",
                  "tags", "summary", "post_count", "is_corroborated", "posts"]

    def get_posts(self, obj):
        posts = sorted(
            obj.posts.all(),
            key=lambda p: ((p.channel.subscribers or 0) if p.channel_id else 0,
                           bool(p.channel_name)),
            reverse=True,
        )[:3]
        return PostLinkSerializer(posts, many=True).data
