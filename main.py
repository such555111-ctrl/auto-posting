"""
main.py

Точка входа: инициализация базы данных, регистрация роутеров,
запуск планировщика автопостинга и long polling.
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from dotenv import load_dotenv

import database
import scheduler
from admin import admin_router
from collector import collector_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
TARGET_CHANNEL_ID: str | None = os.environ.get("TARGET_CHANNEL_ID")


def _apply_env_target_channel() -> None:
    """
    Если целевой канал ещё не задан в settings, но указан в .env —
    подставляем его один раз при первом запуске. Дальнейшие изменения
    делаются только через панель (если появится соответствующая кнопка)
    либо повторным изменением .env при следующем перезапуске.
    """
    if TARGET_CHANNEL_ID is None:
        return

    settings = database.get_settings()
    if settings.target_channel_id is None:
        database.update_settings(target_channel_id=int(TARGET_CHANNEL_ID))
        logger.info("TARGET_CHANNEL_ID из .env применён: %s", TARGET_CHANNEL_ID)


async def on_startup(bot: Bot) -> None:
    database.init_db()
    database.ensure_primary_admin(int(os.environ["ADMIN_ID"]))
    _apply_env_target_channel()
    scheduler.start_scheduler(bot)
    logger.info("Бот запущен.")


async def on_shutdown(bot: Bot) -> None:
    scheduler.stop_scheduler()
    logger.info("Бот остановлен.")


# --------------------------------------------------------------------------- #
# Health-check сервер
# --------------------------------------------------------------------------- #
#
# Render на бесплатном (и обычном web-service) тарифе требует, чтобы сервис
# слушал HTTP-порт из переменной окружения PORT — иначе деплой считается
# упавшим, а бесплатный инстанс "засыпает" без входящих HTTP-запросов.
# Если PORT не задан (например, вы деплоите как Background Worker на
# платном тарифе), сервер просто не запускается — бот работает как обычно.

async def _handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _start_health_server(port: int) -> None:
    app = web.Application()
    app.router.add_get("/", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    logger.info("Health-check сервер запущен на порту %s.", port)


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(admin_router)
    dp.include_router(collector_router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    port_str = os.environ.get("PORT")
    if port_str is not None:
        await _start_health_server(int(port_str))

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем.")
