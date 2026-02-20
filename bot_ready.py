"""Telegram-бот модерации комментариев через Yandex GPT."""

import asyncio
import json
import logging
import os
import re
import ssl
import time
import urllib.request
import urllib.error

import certifi
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import TelegramError

# ── Логирование ──────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logger.propagate = False
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_sh)
try:
    _fh = logging.FileHandler("bot.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
except Exception:
    pass

# ── Конфигурация ─────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.environ.get("CHANNEL", "@lidsnvknevckwebcow9")
ADMIN_ID = int(os.environ["ADMIN_ID"])
FOLDER_ID = os.environ["FOLDER_ID"]
API_KEY = os.environ["API_KEY"]
SPAM_THRESHOLD = float(os.environ.get("SPAM_THRESHOLD", "0.7"))

_SSL = ssl.create_default_context(cafile=certifi.where())

# ── Yandex GPT ───────────────────────────────────────────────

def _yandex_http(url, *, body=None, method="GET", timeout=30):
    headers = {"Authorization": f"Api-Key {API_KEY}", "x-folder-id": FOLDER_ID}
    raw = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode()
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.fp.read().decode("utf-8", errors="replace") if e.fp else ""
        except Exception:
            pass
        logger.error("Yandex %s %s: %s", e.code, url.split("/")[-1], (detail or e.reason)[:500])
        raise


def ask_gpt(prompt: str, timeout: int = 55) -> str:
    """completionAsync → poll operations → текст ответа."""
    data = _yandex_http(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completionAsync",
        body={
            "modelUri": f"gpt://{FOLDER_ID}/yandexgpt/latest",
            "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": 800},
            "messages": [{"role": "user", "text": prompt}],
        },
        method="POST",
        timeout=30,
    )
    op_id = data.get("id")
    if not op_id:
        raise ValueError("completionAsync: нет id")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _yandex_http(f"https://llm.api.cloud.yandex.net/operations/{op_id}", timeout=15)
        if r.get("done"):
            if r.get("error"):
                raise RuntimeError(str(r["error"]))
            alts = (r.get("response") or {}).get("alternatives") or []
            if not alts:
                raise ValueError("Пустой alternatives")
            return (alts[0].get("message") or {}).get("text", "")
        time.sleep(1)
    raise TimeoutError(f"Yandex: таймаут {timeout}с")


PROMPT_TEMPLATE = """Ты — модератор Telegram-канала интернет-магазина. Определи, является ли комментарий ВНЕШНИМ спамом — то есть продвижением ЧУЖИХ сервисов, каналов или схем заработка.

## Комментарий
{text}

## Профиль автора
- username: @{username}
- имя: {name}
- био: {bio}

## Что считать спамом (ТОЛЬКО это)
- Ссылки на ЧУЖИЕ Telegram-каналы, чаты, боты (t.me/...)
- Продвижение ЧУЖИХ сервисов: крипта, трейдинг, ставки, казино, схемы заработка
- Призывы подписаться на СТОРОННИЙ ресурс

## Что НЕ спам (важно!)
- Промокоды, скидки, акции — это нормальная активность магазина и покупателей
- Ссылки на маркетплейсы (Wildberries, Ozon, Яндекс Маркет и т.д.) — покупатели часто сравнивают цены
- Упоминание цен, сравнение цен на разных площадках
- Сообщения от админов канала и самого канала
- Ссылки на юридическую информацию, оферты, политику конфиденциальности
- Обычные комментарии: мнения, вопросы, благодарности, жалобы, отзывы
- Упоминание собственного опыта покупки
- Короткие реакции: «круто», «спасибо», эмодзи

Если сомневаешься — это НЕ спам. Помечай как спам только очевидное продвижение ЧУЖИХ ресурсов.

Ответь СТРОГО JSON без пояснений:
{{"spam": true/false, "confidence": 0.0-1.0, "reason": "краткая причина на русском"}}"""


async def check_spam(text: str, username: str, name: str, bio: str) -> dict | None:
    """Возвращает dict с полями spam/confidence/reason или None при ошибке GPT."""
    prompt = PROMPT_TEMPLATE.format(text=text, username=username, name=name, bio=bio or "не указано")
    try:
        logger.info("GPT запрос...")
        raw = await asyncio.to_thread(ask_gpt, prompt, 55)
        logger.info("GPT ответ (%d симв.)", len(raw))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            logger.error("GPT: нет JSON: %s", raw[:300])
            return None
        cleaned = re.sub(r",\s*}", "}", m.group())
        result = json.loads(cleaned)
        result.setdefault("confidence", 0.0)
        result.setdefault("reason", "")
        return result
    except (json.JSONDecodeError, Exception) as e:
        logger.error("GPT: %s: %s", type(e).__name__, e)
        return None


# ── Telegram ─────────────────────────────────────────────────

async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    msg = update.message
    user = msg.from_user
    username = user.username or "нет"
    name = (f"{user.first_name or ''} {user.last_name or ''}".strip() or "—")
    text = msg.text

    bio = None
    try:
        chat = await context.bot.get_chat(user.id)
        bio = getattr(chat, "bio", None)
    except Exception:
        pass

    logger.info("@%s: %s...", username, text[:60])

    result = await check_spam(text, username, name, bio)
    if result is None:
        logger.warning("GPT не ответил, пропускаем")
        return

    confidence = float(result.get("confidence", 0))
    logger.info("Спам: %.0f%%", confidence * 100)

    if confidence < SPAM_THRESHOLD:
        return

    chat_id = msg.chat.id
    msg_id = msg.message_id
    cb_del = f"spam_del:{chat_id}:{msg_id}"[:64]

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"<b>Спам ({confidence:.0%})</b>\n\n"
            f"<b>Автор:</b> @{username}\n"
            f"<b>Текст:</b>\n{text}\n\n"
            f"<b>Причина:</b> {result.get('reason', '—')}\n\n"
            f"https://t.me/c/{str(chat_id)[4:]}/{msg_id}"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Пропустить", callback_data="spam_ok"),
            InlineKeyboardButton("Удалить", callback_data=cb_del),
        ]]),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "spam_ok":
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return

    if data.startswith("spam_del:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            try:
                await context.bot.delete_message(chat_id=int(parts[1]), message_id=int(parts[2]))
                logger.info("Удалён")
            except (ValueError, TelegramError) as e:
                logger.error("Удаление: %s", e)
        try:
            await q.message.delete()
        except TelegramError:
            pass


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Ошибка: %s", context.error)


# ── Запуск ───────────────────────────────────────────────────

def main():
    if not FOLDER_ID or not API_KEY:
        logger.error("Нет FOLDER_ID / API_KEY")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(
        filters.TEXT & (filters.ChatType.CHANNEL | filters.ChatType.SUPERGROUP),
        handle_comment,
    ))
    app.add_handler(CallbackQueryHandler(handle_button, pattern="^spam_(del|ok)"))
    app.add_error_handler(on_error)

    logger.info("Бот запущен, канал %s, порог %d%%", CHANNEL, SPAM_THRESHOLD * 100)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
