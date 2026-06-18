"""Telegram-бот модерации комментариев через Yandex GPT."""
в
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
from telegram import Message, Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, TimedOut, RetryAfter
from telegram.request import HTTPXRequest

DELETE_RETRIES = 3
DELETE_RETRY_DELAY = 2.0
JOIN_DELETE_RETRIES = 8
JOIN_DELETE_RETRY_DELAY = 5.0


def _has_user_content(message: Message) -> bool:
    """Любой пользовательский контент — не сервисное join-сообщение."""
    return bool(
        message.text or message.caption or message.sticker or message.photo
        or message.video or message.animation or message.document
        or message.audio or message.voice or message.video_note
    )


class _JoinServiceMessage(filters.MessageFilter):
    """Сервисное сообщение о входе в чат (в т.ч. new_chat_members=[] в крупных группах)."""

    def filter(self, message: Message) -> bool:
        if message.new_chat_members is None:
            return False
        if _has_user_content(message):
            return False
        return True


JOIN_SERVICE = _JoinServiceMessage()

# ── Логирование ──────────────────────────────────────────────

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
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

BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])
FOLDER_ID   = os.environ["FOLDER_ID"]
API_KEY     = os.environ["API_KEY"]

DELETE_CONFIDENCE  = 0.95  # удалять автоматически
SUSPECT_CONFIDENCE = 0.6   # сообщать администратору

LINK_ENTITY_TYPES = {"url", "text_link", "mention"}

_SSL = ssl.create_default_context(cafile=certifi.where())

# ── Yandex GPT ───────────────────────────────────────────────

def _yandex_http(url: str, *, body=None, method: str = "GET", timeout: int = 30) -> dict:
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


PROMPT_TEMPLATE = """Ты — модератор Telegram-канала интернет-магазина. Твоя задача — найти ТОЛЬКО очевидный внешний спам: продвижение чужих ресурсов со ссылкой на них. Всё остальное — НЕ спам.

## Канал/группа
- название: {channel_title}

## Комментарий
{text}

## Метаданные сообщения
- ответ на другое сообщение: {is_reply}
- сущности в тексте: {entities}

## Профиль автора
- username: @{username}
- имя: {name}
- Telegram Premium: {is_premium}
- фото профиля: {has_photo}
- язык клиента: {language}

## Спам — ТОЛЬКО если одновременно выполнены ОБА условия:
1. В тексте есть ссылка (t.me/..., URL или упоминание @канала/бота)
2. Рядом со ссылкой есть призыв или обещание из любой из этих категорий:
   - Заработок, доход, деньги («зарабатывай», «доход», «от X рублей в день», «без вложений»)
   - Крипта, трейдинг, ставки, казино

Исключение: @yamarketaffbot — не спам.

## Whitelist ссылок — НИКОГДА не спам
Если в сообщении есть ссылка ТОЛЬКО на ресурсы из этого списка (без призыва к стороннему заработку/крипте/казино) — это НЕ спам:
- Яндекс Маркет: market.yandex.ru, yandex.ru/market, yandex.ru/dev/market
- Wildberries: wildberries.ru, wb.ru
- Ozon: ozon.ru
- ВКонтакте: vk.ru, vk.com
- Внутренние ссылки на сообщения в этом же чате: t.me/c/...
- Бот магазина: @yamarketaffbot, t.me/yamarketaffbot

## Однозначно НЕ спам (даже если кажется подозрительным):
- Рассказ о своём заработке, доходе, опыте — БЕЗ ссылки
- Просто ссылка без призыва («вот смотрел», «нашёл тут»)
- Любые ссылки из whitelist выше
- Промокоды, скидки, акции
- Сравнение цен на разных площадках
- Ссылки на юридические документы, оферты, политику конфиденциальности
- Любые вопросы, мнения, отзывы, благодарности, жалобы
- Короткие реакции: «круто», «спасибо», эмодзи
- Ответ на чужое сообщение (is_reply=да) — почти всегда не спам

## Правило принятия решения:
- confidence >= 0.95 и spam=true → удалить (очевидный спам, нет сомнений)
- confidence 0.6–0.94 и spam=true → сомнительно, сообщить администратору без удаления
- всё остальное → не спам

Ответь СТРОГО JSON без пояснений:
{{"spam": true/false, "confidence": 0.0-1.0, "reason": "краткая причина на русском"}}

confidence — уверенность, что это спам. При малейшем сомнении снижай confidence."""


async def check_spam(
    text: str,
    username: str,
    name: str,
    *,
    is_premium: bool = False,
    has_photo: bool = False,
    language: str = "не определён",
    entity_types: list[str] | None = None,
    is_reply: bool = False,
    channel_title: str = "—",
) -> dict | None:
    """Возвращает dict с полями spam/confidence/reason или None при ошибке GPT."""
    prompt = PROMPT_TEMPLATE.format(
        text=text,
        username=username,
        name=name,
        is_premium="да" if is_premium else "нет",
        has_photo="да" if has_photo else "нет",
        language=language,
        entities=", ".join(entity_types) if entity_types else "нет",
        is_reply="да" if is_reply else "нет",
        channel_title=channel_title,
    )
    try:
        logger.info("GPT запрос...")
        raw = await asyncio.to_thread(ask_gpt, prompt, 55)
        logger.info("GPT ответ (%d симв.)", len(raw))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            logger.error("GPT: нет JSON: %s", raw[:300])
            return None
        result = json.loads(re.sub(r",\s*}", "}", m.group()))
        result.setdefault("confidence", 0.0)
        result.setdefault("reason", "")
        return result
    except Exception as e:
        logger.error("GPT: %s: %s", type(e).__name__, e)
        return None


# ── Telegram ─────────────────────────────────────────────────

async def _fetch_user_photo(bot, user_id: int) -> bool:
    try:
        chat = await bot.get_chat(user_id)
        return chat.photo is not None
    except Exception:
        return False


def _admin_text(label: str, confidence: float, username: str, text: str, reason: str) -> str:
    return (
        f"<b>{label} ({confidence:.0%})</b>\n\n"
        f"<b>Автор:</b> @{username}\n"
        f"<b>Текст:</b>\n{text}\n\n"
        f"<b>Причина:</b> {reason or '—'}"
    )


async def _delete_message_retry(bot, chat_id: int, message_id: int, *, label: str = "") -> bool:
    prefix = f"{label}: " if label else ""
    last_err = None
    for attempt in range(1, DELETE_RETRIES + 1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except RetryAfter as e:
            last_err = e
            wait = min(float(e.retry_after) + 1.0, 30.0)
            logger.warning("%sфлуд-контроль удаления (%d/%d), ждём %.0fс, чат %s", prefix, attempt, DELETE_RETRIES, wait, chat_id)
            if attempt < DELETE_RETRIES:
                await asyncio.sleep(wait)
        except TimedOut as e:
            last_err = e
            logger.warning("%sтаймаут удаления (%d/%d), чат %s", prefix, attempt, DELETE_RETRIES, chat_id)
            if attempt < DELETE_RETRIES:
                await asyncio.sleep(DELETE_RETRY_DELAY * attempt)
        except TelegramError as e:
            logger.error("%sошибка удаления в чате %s: %s", prefix, chat_id, e)
            return False
    logger.error("%sне удалось удалить в чате %s после %d попыток: %s", prefix, chat_id, DELETE_RETRIES, last_err)
    return False


def _message_text_and_entities(msg: Message) -> tuple[str | None, list[str]]:
    if msg.text:
        return msg.text, [e.type for e in (msg.entities or [])]
    if msg.caption:
        return msg.caption, [e.type for e in (msg.caption_entities or [])]
    return None, []


async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    msg = update.message

    if msg.sticker or msg.animation or msg.video_note:
        return

    if msg.sender_chat:
        return

    user = msg.from_user
    if not user:
        return

    try:
        member = await context.bot.get_chat_member(msg.chat.id, user.id)
        if member.status in ("creator", "administrator"):
            return
    except TelegramError:
        pass

    text, entity_types = _message_text_and_entities(msg)
    if not text:
        return
    if not any(e in LINK_ENTITY_TYPES for e in entity_types):
        return

    username = user.username or "нет"
    name = (f"{user.first_name or ''} {user.last_name or ''}".strip() or "—")

    logger.info("@%s: %s...", username, text[:60])

    has_photo = await _fetch_user_photo(context.bot, user.id)

    result = await check_spam(
        text=text,
        username=username,
        name=name,
        is_premium=user.is_premium or False,
        has_photo=has_photo,
        language=user.language_code or "не определён",
        entity_types=entity_types,
        is_reply=msg.reply_to_message is not None,
        channel_title=msg.chat.title or "—",
    )
    if result is None:
        logger.warning("GPT не ответил по @%s, уведомляем админа", username)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "<b>⚠️ Не проверено — GPT не ответил</b>\n\n"
                f"<b>Автор:</b> @{username}\n"
                f"<b>Текст:</b>\n{text}"
            ),
            parse_mode="HTML",
        )
        return

    is_spam = result.get("spam", False)
    confidence = float(result.get("confidence", 0))
    logger.info("spam=%s confidence=%.0f%%", is_spam, confidence * 100)

    if not is_spam or confidence < SUSPECT_CONFIDENCE:
        return

    reason = result.get("reason", "—")

    if confidence >= DELETE_CONFIDENCE:
        deleted = await _delete_message_retry(context.bot, msg.chat.id, msg.message_id, label="Спам")
        if deleted:
            logger.info("Автоудалён спам от @%s", username)
            admin_text = _admin_text("Спам удалён", confidence, username, text, reason)
        else:
            admin_text = _admin_text("Не удалено — ошибка API", confidence, username, text, reason)
    else:
        logger.info("Сомнительное сообщение от @%s, не удалено", username)
        admin_text = _admin_text("Не удалено — сомнительное", confidence, username, text, reason)

    await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")


async def _delete_join_background(bot, chat_id: int, message_id: int) -> None:
    """Фоновое удаление join-сообщения с расширенными повторами (сеть до Telegram нестабильна)."""
    for attempt in range(1, JOIN_DELETE_RETRIES + 1):
        if await _delete_message_retry(bot, chat_id, message_id, label="Join"):
            logger.info("Удалено join-сообщение, чат %s, msg %s (попытка %d)", chat_id, message_id, attempt)
            return
        if attempt < JOIN_DELETE_RETRIES:
            wait = JOIN_DELETE_RETRY_DELAY * attempt
            logger.warning("Join: повтор %d/%d через %.0fс, чат %s", attempt, JOIN_DELETE_RETRIES, wait, chat_id)
            await asyncio.sleep(wait)
    logger.error("Join: не удалось удалить msg %s в чате %s после %d раундов", message_id, chat_id, JOIN_DELETE_RETRIES)


async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or _has_user_content(msg):
        return
    asyncio.create_task(_delete_join_background(context.bot, msg.chat.id, msg.message_id))


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Ошибка: %s", context.error)


# ── Запуск ───────────────────────────────────────────────────

def main():
    if not FOLDER_ID or not API_KEY:
        logger.error("Нет FOLDER_ID / API_KEY")
        return

    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)
    updates_req = HTTPXRequest(connect_timeout=30.0, read_timeout=35.0, write_timeout=30.0, pool_timeout=30.0)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .get_updates_request(updates_req)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & (filters.ChatType.CHANNEL | filters.ChatType.SUPERGROUP),
        handle_comment,
    ))
    app.add_handler(MessageHandler(
        JOIN_SERVICE & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        handle_join,
    ))
    app.add_error_handler(on_error)

    logger.info("Бот запущен (удалять >= %.0f%%, уведомлять >= %.0f%%)",
                DELETE_CONFIDENCE * 100, SUSPECT_CONFIDENCE * 100)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
