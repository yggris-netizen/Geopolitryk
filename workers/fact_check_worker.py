# -*- coding: utf-8 -*-
"""Бот 3: еженедельный фактчекинг прогнозов (встроен в Бот 1).

Использует scripts/test_factcheck.py (Tavily + Groq).
Раз в неделю: выбрать прогнозы за 7 дней -> проверить -> записать вердикт -> опубликовать.
"""
import asyncio

from aiogram.types import LinkPreviewOptions

from config import (
    CHANNEL_ID, FACT_CHECK_WINDOW_DAYS, FACT_CHECK_MAX_REQUESTS,
)
from db.predictions import (
    get_predictions_for_factcheck, set_fact_check_result,
)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _run_factcheck_batch(days: int, limit: int) -> list:
    """Синхронная пакетная проверка (Tavily + Groq). Возвращает список dict."""
    # Импорт внутри функции — чтобы bot.py не падал, если scripts/ нет
    from scripts.test_factcheck import factcheck_one

    preds = get_predictions_for_factcheck(days=days, limit=limit)
    if not preds:
        return []

    results = []
    for p in preds:
        try:
            r = factcheck_one(p)
        except Exception as e:
            print(f"[FACT] #{p.get('id')} error: {e!r}")
            r = {"id": p.get("id"), "verdict": "unverifiable",
                 "source_url": "", "reasoning": f"error: {e!r}"[:200]}
        pid = r.get("id")
        if pid:
            set_fact_check_result(
                pid, r["verdict"],
                source_url=r.get("source_url", ""),
                reasoning=r.get("reasoning", ""),
            )
        results.append(r)
        print(f"[FACT] #{pid}: {r['verdict']} — {r['reasoning'][:80]}")
    return results


async def fact_check_job(bot):
    """Задача планировщика: раз в неделю. Публикует итоги в CHANNEL_ID."""
    print(f"\n{'=' * 60}")
    print("[FACT] Еженедельный фактчекинг...")
    print(f"{'=' * 60}")
    try:
        results = await asyncio.to_thread(
            _run_factcheck_batch, FACT_CHECK_WINDOW_DAYS, FACT_CHECK_MAX_REQUESTS
        )
    except Exception as e:
        print(f"[FACT] Критическая ошибка: {e!r}")
        return

    if not results:
        print("[FACT] Нет прогнозов для проверки")
        return

    confirmed = sum(1 for r in results if r["verdict"] == "confirmed")
    refuted = sum(1 for r in results if r["verdict"] == "refuted")
    unverifiable = sum(1 for r in results if r["verdict"] == "unverifiable")
    print(f"[FACT] Проверено {len(results)}: "
          f"confirmed={confirmed}, refuted={refuted}, unverifiable={unverifiable}")

    lines = [f"<b>[FACT] Фактчекинг ({len(results)} прогнозов)</b>", ""]
    lines.append(f"✅ Подтверждено: {confirmed}")
    lines.append(f"❌ Опровергнуто: {refuted}")
    lines.append(f"❓ Непроверяемо: {unverifiable}")
    lines.append("")
    for r in results[:10]:
        icon = {"confirmed": "✅", "refuted": "❌", "unverifiable": "❓"}.get(r["verdict"], "•")
        line = f"{icon} <b>#{r['id']}</b>"
        if r.get("reasoning"):
            line += f" — {r['reasoning'][:120]}"
        if r.get("source_url"):
            line += f"\n   <a href='{r['source_url']}'>источник</a>"
        lines.append(line)
    text = "\n".join(lines)

    try:
        if len(text) <= 4096:
            await bot.send_message(chat_id=CHANNEL_ID, text=text,
                                   link_preview_options=NO_PREVIEW)
        else:
            # на случай переполнения — режем на 2 части
            mid = len(lines) // 2
            part1 = "\n".join(lines[:mid])
            part2 = "\n".join(lines[mid:])
            await bot.send_message(chat_id=CHANNEL_ID, text=part1[:4096],
                                   link_preview_options=NO_PREVIEW)
            await asyncio.sleep(1)
            await bot.send_message(chat_id=CHANNEL_ID, text=part2[:4096],
                                   link_preview_options=NO_PREVIEW)
        print("[FACT] Опубликовано")
    except Exception as e:
        print(f"[FACT] Ошибка публикации: {e!r}")
