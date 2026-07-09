"""Фабрики тестових об'єктів (factory-boy)."""
import factory

from analysis import models


class TaskFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.AnalysisTask

    name = factory.Sequence(lambda n: f"Задача {n}")
    slug = factory.Sequence(lambda n: f"task-{n}")
    telezip_query = "*"
    classify_system_prompt = "-"
    pipeline = models.AnalysisTask.PIPELINE_INFOSPACE


class SourceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Source

    kind = models.Source.KIND_RSS
    name = factory.Sequence(lambda n: f"Джерело {n}")
    url = factory.Sequence(lambda n: f"https://example.org/feed-{n}.xml")


class SubscriptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SourceSubscription

    task = factory.SubFactory(TaskFactory)
    source = factory.SubFactory(SourceFactory)
