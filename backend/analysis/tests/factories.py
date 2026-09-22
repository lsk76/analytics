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
    """Джерело через Source.ensure: name/url/region ідуть у рядок довідника."""
    class Meta:
        model = models.Source

    kind = models.Source.KIND_RSS
    name = factory.Sequence(lambda n: f"Джерело {n}")
    url = factory.Sequence(lambda n: f"https://example.org/feed-{n}.xml")

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        name = kwargs.pop("name", "")
        url = kwargs.pop("url")
        region = kwargs.pop("region_subject", None)
        language = kwargs.pop("language", "")
        src, _ = model_class.ensure(kwargs.pop("kind"), url, name=name, region=region,
                                    language=language, **kwargs)
        return src


class SubscriptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SourceSubscription

    task = factory.SubFactory(TaskFactory)
    source = factory.SubFactory(SourceFactory)
