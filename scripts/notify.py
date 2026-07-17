#!/usr/bin/env python3
"""
Telegram-уведомления для Smart Money Dashboard.

Читает секреты из окружения (задаются в GitHub Actions как secrets):
  TELEGRAM_BOT_TOKEN  — токен бота от @BotFather
  TELEGRAM_CHAT_ID    — id чата/канала, куда слать (можно получить у @userinfobot)

Если секреты не заданы — всё тихо пропускается (workflow не падает).
Никаких внешних зависимостей: только стандартная библиотека.
"""

import os
import html
import time
import json
import urllib.request
import urllib.parse

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TG_LIMIT = 4000  # запас под лимит Telegram в 4096 символов


def enabled() -> bool:
    return bool(TG_TOKEN and TG_CHAT)


def esc(s) -> str:
    """Экранирование под parse_mode=HTML (& < > обязательны)."""
    return html.escape(str(s), quote=False)


def _chunks(text: str, limit: int = TG_LIMIT):
    """Режем длинный текст по строкам, чтобы уложиться в лимит Telegram."""
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                yield buf
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        yield buf


def send(text: str, disable_preview: bool = True) -> bool:
    """
    Отправляет сообщение в Telegram. Возвращает True при успехе.
    При отсутствии секретов — печатает и возвращает False (не ошибка).
    """
    if not enabled():
        print("  [tg] секреты не заданы — сообщение не отправлено:")
        print("       " + text.replace("\n", "\n       ")[:500])
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    ok = True
    for chunk in _chunks(text):
        payload = urllib.parse.urlencode({
            "chat_id": TG_CHAT,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true" if disable_preview else "false",
        }).encode()
        req = urllib.request.Request(url, data=payload)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode("utf-8"))
                if not resp.get("ok"):
                    print(f"  [tg] Telegram вернул ошибку: {resp}")
                    ok = False
        except Exception as e:
            print(f"  [tg] отправка не удалась: {e}")
            ok = False
        time.sleep(0.4)  # мягкий троттлинг, чтобы не ловить 429
    return ok
