import os
import io
import asyncio
from typing import Any, Dict, List

import httpx

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors

from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

# Регистрация шрифта DejaVuSans (файл должен лежать рядом с main.py)
font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))

# ====== ENVIRONMENT VARIABLES ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = "/webhook/astrohorary"

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("TELEGRAM_TOKEN или OPENAI_API_KEY не установлены")

bot = Bot(TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
app = FastAPI()
client = httpx.AsyncClient(timeout=90)

# ====== Импорт методов астрологии вашего проекта ======
from astro_calc import get_location, calculate_chart, calculate_horary, calculate_synastry

# ====== GPT-интерпретация ======
async def gpt_interpret(prompt: str, max_tokens: int = 2000) -> str:
    try:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system",
                     "content": "Ты профессиональный астролог с 15-летним опытом. Пиши тёпло и понятно на русском."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7
            },
            timeout=60.0
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"⚠️ Не удалось получить интерпретацию: {e}"

# ====== Стили PDF с DejaVuSans ======
styles = getSampleStyleSheet()
# Переопределяем/добавляем стили с шрифтом DejaVuSans для поддержки кириллицы
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVuSans", fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=12))
styles.add(ParagraphStyle(name="HeadRu", fontName="DejaVuSans", fontSize=12, leading=16, alignment=TA_LEFT, spaceBefore=8, spaceAfter=6))
styles.add(ParagraphStyle(name="TextRu", fontName="DejaVuSans", fontSize=11, leading=16, alignment=TA_LEFT, spaceAfter=6))

def _table(data: List[List[str]]) -> Table:
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "DejaVuSans"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t

async def build_pdf_natal(chart_data: Dict[str, Any]) -> bytes:
    planets = chart_data.get("planets", [])
    dt_loc = chart_data.get("datetime_local", "—")
    tz = chart_data.get("iana_tz", "—")
    planets_str = "\n".join([f"{p['name']}: {p.get('sign', '?')} {round(p['lon'] % 30, 1)}°" for p in planets])

    prompt = f"""Проанализируй натальную карту:

Дата: {dt_loc}
ASC: {chart_data.get('asc', '—')}
MC: {chart_data.get('mc', '—')}

Планеты:
{planets_str}

Дай подробную интерпретацию на русском языке, пишу понятно и тепло."""

    interpretation = await gpt_interpret(prompt, max_tokens=3000)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story = [
        Paragraph("Натальная карта (Placidus)", styles["TitleRu"]),
        Paragraph(f"Дата и время: {dt_loc} ({tz})", styles["TextRu"]),
        Spacer(1, 8),
        _table([
            ["Элемент", "Значение"],
            ["ASC", chart_data.get("asc", "—")],
            ["MC", chart_data.get("mc", "—")]
        ]),
        Spacer(1, 12)
    ]
    rows = [["Планета", "Долгота", "Знак", "R"]]
    for p in planets:
        rows.append([p["name"], f"{round(p['lon'], 2)}°", p.get("sign", "—"), "R" if p.get("retro") else ""])
    story += [Paragraph("Планеты", styles["HeadRu"]), _table(rows), PageBreak()]
    story += [
        Paragraph("Интерпретация", styles["HeadRu"]),
        Paragraph(interpretation.replace('\n', '<br/>'), styles["TextRu"])
    ]
    doc.build(story)
    return buf.getvalue()

# Аналогично реализуйте build_pdf_horary и build_pdf_synastry — в примере ниже только build_and_send_pdf с асинхронным вызовом get_location!

def upsell_keyboard(service_type: str) -> InlineKeyboardMarkup:
    buttons = []
    if service_type == "horary":
        buttons = [
            [InlineKeyboardButton(text="🔮 Ещё вопрос (300₽)", callback_data="buy_horary")],
            [InlineKeyboardButton(text="📊 Транзиты на месяц (400₽)", callback_data="buy_transits")],
            [InlineKeyboardButton(text="⭐ Натальная карта со скидкой 20% (800₽)", callback_data="buy_natal_discount")]
        ]
    elif service_type == "natal":
        buttons = [
            [InlineKeyboardButton(text="💑 Синастрия с партнёром (900₽)", callback_data="buy_synastry")],
            [InlineKeyboardButton(text="📅 Прогноз на год (1200₽)", callback_data="buy_forecast")],
            [InlineKeyboardButton(text="🔮 Задать хорарный вопрос (300₽)", callback_data="buy_horary")]
        ]
    else:
        buttons = [
            [InlineKeyboardButton(text="📊 Транзиты для отношений (500₽)", callback_data="buy_transits_synastry")],
            [InlineKeyboardButton(text="⭐ Композитная карта (600₽)", callback_data="buy_composite")]
        ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

user_questions = {}

async def build_and_send_pdf(chat_id: int, kind: str, args: Dict[str, Any]):
    try:
        await bot.send_message(chat_id, "⏳ Рассчитываю карту и готовлю интерпретацию...")

        if kind == "natal":
            # await перед get_location, чтобы получить координаты
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_chart(args["dt"], lat, lon, tz, house_system="P")
            pdf_bytes = await build_pdf_natal(chart)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf_bytes, filename="natal.pdf"),
                                    caption="✨ Ваша натальная карта готова!",
                                    reply_markup=upsell_keyboard("natal"))

        # Добавьте аналогично блоки для horary и synastry при необходимости

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        await bot.send_message(chat_id, f"⚠️ Ошибка: {e}")

def _parse_line(s: str):
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 4:
        raise ValueError("Формат: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    dd, mm, yy = parts[0].split(".")
    dt = f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}T{parts[1]}"
    return dt, parts[2], ",".join(parts[3:])

# Ваша логика хэндлеров, вебхуков и прочее остаётся как раньше, добавьте только если нужно

# Пример команды /start
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Натальная карта", callback_data="info_natal")],
        [InlineKeyboardButton(text="🔮 Хорарный вопрос", callback_data="info_horary")],
        [InlineKeyboardButton(text="💑 Синастрия", callback_data="info_synastry")]
    ])
    await message.answer("Привет! Выберите услугу:", reply_markup=keyboard)

# Не забудьте добавить остальную логику...

# FastAPI webhook обработчики и т.д.

