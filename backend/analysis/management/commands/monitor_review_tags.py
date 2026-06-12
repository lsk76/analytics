"""
Review opinion-tag taxonomy after agent runs.

Prints, per category, the list of all Tags with their Post-count and a sample
text — so the human can spot dups (крит_путіна / крит_путіну / крит_путин),
nonsensical proposed_tags, and renames needed.

Examples:
  python manage.py monitor_review_tags --task dagestan-criticism-monitor
  python manage.py monitor_review_tags --task dagestan-criticism-monitor \
      --category criticism_target --min-count 1
  python manage.py monitor_review_tags --task dagestan-criticism-monitor \
      --merge крит_путіну,крит_путіна:крит_путін
  python manage.py monitor_review_tags --task dagestan-criticism-monitor \
      --drop крит_сучкарь,крит_фигня
  python manage.py monitor_review_tags --task dagestan-criticism-monitor \
      --rename крит_меликов:крит_меликова
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from analysis.models import AnalysisTask, Post, Tag


CATEGORIES = ["criticism_target", "topic", "opinion"]


class Command(BaseCommand):
    help = "List, merge, rename, drop tags for an opinion-monitor task."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True)
        parser.add_argument("--category", default="",
                            help="One of criticism_target/topic/opinion. "
                                 "Empty = all 3. ВАЖЛИВО: --merge/--rename/--drop "
                                 "діють ЛИШЕ в межах цієї категорії (щоб не зачепити "
                                 "однойменний тег іншої категорії).")
        parser.add_argument("--min-count", type=int, default=1,
                            help="Hide tags with fewer Post-uses.")
        parser.add_argument("--samples", type=int, default=2,
                            help="Show N sample posts per tag.")
        parser.add_argument("--merge", default="",
                            help="Format: 'src1,src2:dst' — merge src tags into dst. "
                                 "Can repeat with ';' separator: 'a,b:c;d,e:f'.")
        parser.add_argument("--rename", default="",
                            help="Format: 'old:new'. Multiple via ';'.")
        parser.add_argument("--drop", default="",
                            help="Comma-separated tag names to delete (with M2M cleanup).")

    def handle(self, *args, **opts):
        try:
            task = AnalysisTask.objects.get(slug=opts["task"])
        except AnalysisTask.DoesNotExist:
            raise CommandError(f"Task {opts['task']!r} not found.")

        # Категорія обмежує мутації — без неї однойменний тег іншої категорії
        # (напр. opinion=теорія_змови vs topic=теорія_змови) можна зачепити.
        cat = opts["category"] or None

        # ---- mutations first --------------------------------------------
        if opts["merge"]:
            self._merge(opts["merge"], task, cat)
        if opts["rename"]:
            self._rename(opts["rename"], task, cat)
        if opts["drop"]:
            self._drop(opts["drop"], task, cat)

        # ---- listing ----------------------------------------------------
        cats = [opts["category"]] if opts["category"] else CATEGORIES
        for cat in cats:
            tags_qs = (Tag.objects.filter(category=cat,
                                           posts__task=task)
                       .annotate(post_count=Count("posts", distinct=True))
                       .filter(post_count__gte=opts["min_count"])
                       .order_by("-post_count", "name"))
            tags = list(tags_qs)
            self.stdout.write(self.style.HTTP_INFO(
                f"\n=== {cat} ({len(tags)} unique tags) ==="
            ))
            for t in tags:
                self.stdout.write(f"  {t.post_count:>5d}  {t.name}")
                if opts["samples"]:
                    samples = (Post.objects.filter(task=task, tags=t)
                               .order_by("?")[:opts["samples"]])
                    for p in samples:
                        txt = (p.text or "").replace("\n", " ")[:120]
                        ch = p.channel.username if p.channel else "-"
                        self.stdout.write(f"         · @{ch}: {txt}")

    @staticmethod
    def _q(name, cat):
        qs = Tag.objects.filter(name=name)
        return qs.filter(category=cat) if cat else qs

    @transaction.atomic
    def _merge(self, spec: str, task: AnalysisTask, cat=None):
        for one in spec.split(";"):
            if ":" not in one: continue
            srcs, dst = one.split(":", 1)
            srcs = [s.strip() for s in srcs.split(",") if s.strip()]
            dst = dst.strip()
            if not srcs or not dst: continue
            dst_tag = self._q(dst, cat).first()
            if not dst_tag:
                src_tag = self._q(srcs[0], cat).first()
                use_cat = cat or (src_tag.category if src_tag else "criticism_target")
                dst_tag, _ = Tag.objects.get_or_create(name=dst, category=use_cat)
            n_moved = 0
            for src_name in srcs:
                src_tag = self._q(src_name, cat).first()
                if not src_tag: continue
                posts = Post.objects.filter(task=task, tags=src_tag)
                for p in posts:
                    p.tags.add(dst_tag)
                    p.tags.remove(src_tag)
                    n_moved += 1
                if not Post.objects.filter(tags=src_tag).exists():
                    src_tag.delete()
            self.stdout.write(self.style.SUCCESS(
                f"  merged {srcs} → {dst}: {n_moved} posts updated"
            ))

    @transaction.atomic
    def _rename(self, spec: str, task: AnalysisTask, cat=None):
        for one in spec.split(";"):
            if ":" not in one: continue
            old, new = one.split(":", 1)
            old, new = old.strip(), new.strip()
            t = self._q(old, cat).first()
            if not t:
                self.stdout.write(self.style.WARNING(f"  rename: {old} not found"))
                continue
            t.name = new
            t.save(update_fields=["name"])
            self.stdout.write(self.style.SUCCESS(f"  renamed {old} → {new}"))

    @transaction.atomic
    def _drop(self, spec: str, task: AnalysisTask, cat=None):
        names = [s.strip() for s in spec.split(",") if s.strip()]
        for name in names:
            t = self._q(name, cat).first()
            if not t:
                self.stdout.write(self.style.WARNING(f"  drop: {name} not found"))
                continue
            n_unlink = 0
            for p in Post.objects.filter(task=task, tags=t):
                p.tags.remove(t); n_unlink += 1
            if not Post.objects.filter(tags=t).exists():
                t.delete()
                self.stdout.write(self.style.SUCCESS(
                    f"  dropped {name}: unlinked from {n_unlink} posts, tag deleted"
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    f"  unlinked {name} from {n_unlink} posts of this task; "
                    f"tag still used elsewhere, kept."
                ))
