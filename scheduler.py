"""
scheduler.py

Управление фоновой задачей автопостинга через APScheduler.

Логика:
- start_scheduler(bot)  — вызывается один раз при старте бота, поднимает
  AsyncIOScheduler и, если постинг активен в settings, ставит задачу.
- apply_settings(bot)   — вызывается каждый раз, когда админ меняет
  настройки (интервал, вкл/выкл, канал, бренд), и пересобирает задачу
  под новые параметры.
- stop_scheduler()      — полная остановка планировщика (например, при
  завершении работы бота).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database
from publisher import publish_next_post

logger = logging.getLogger(__name__)

_JOB_ID = "autopost_job"

_scheduler: AsyncIOScheduler = AsyncIOScheduler(timezone="UTC")


def start_scheduler(bot: Bot) -> None:
    """Запускает планировщик и применяет текущие настройки из БД."""
    if not _scheduler.running:
        _scheduler.start()
        logger.info("Планировщик APScheduler запущен.")

    apply_settings(bot)


def apply_settings(bot: Bot) -> None:
    """
    Пересобирает задачу автопостинга согласно текущим настройкам в БД.
    Вызывать после любого изменения settings (интервал, is_active, канал, бренд).
    """
    settings = database.get_settings()

    existing_job: Job | None = _scheduler.get_job(_JOB_ID)

    if not settings.is_active or settings.target_channel_id is None:
        if existing_job is not None:
            existing_job.remove()
            logger.info("Задача автопостинга остановлена.")
        return

    interval = max(1, settings.interval_minutes)

    if existing_job is not None:
        existing_job.reschedule(trigger="interval", minutes=interval)
        logger.info("Интервал автопостинга обновлён: %s мин.", interval)
    else:
        _scheduler.add_job(
            publish_next_post,
            trigger="interval",
            minutes=interval,
            args=[bot],
            id=_JOB_ID,
            replace_existing=True,
            next_run_time=None,  # первая публикация — через интервал, не сразу
        )
        logger.info("Задача автопостинга запущена с интервалом %s мин.", interval)


def stop_scheduler() -> None:
    """Полностью останавливает планировщик (например, при shutdown бота)."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Планировщик APScheduler остановлен.")


def is_job_active() -> bool:
    """Удобная проверка — есть ли сейчас активная задача автопостинга."""
    return _scheduler.get_job(_JOB_ID) is not None
