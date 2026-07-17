"""Publish-конвеєр: approved-Event → AI-фільтр+рерайт → пост у Telegram-канал.

Стадія `publish` (stages.py) — TASKLESS: ітерує активні PublishConfig, кожен
відбирає свій зріз подій, проганяє через AI і публікує в свій канал через Bot API.
Стан/claim/аудит — модель PublishedEvent (unique config+event).
"""
