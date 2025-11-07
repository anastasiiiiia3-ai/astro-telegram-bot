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

# Регистрация шрифта DejaVuSans
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "/app/DejaVuSans.ttf"))
    print("✅ Шрифт DejaVuSans зарегистрирован успешно")
except Exception as e:
    print(f"❌ Ошибка регистрации шрифта: {e}")
    raise

# ====== ENV ======
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

# ====== ASTRO ======
from astro_calc import get_location, calculate_chart, calculate_horary, calculate_synastry

# ====== GPT ======
async def gpt_interpret(prompt: str, max_tokens: int = 2000) -> str:
    try:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "Ты профессиональный астролог с 15-летним опытом. Пиши тепло и понятно на русском."},
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

# ====== PDF СТИЛИ ======
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVuSans", fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=12))
styles.add(ParagraphStyle(name="HeadRu", fontName="DejaVuSans", fontSize=12, leading=16, alignment=TA_LEFT, spaceBefore=8, spaceAfter=6))
styles.add(ParagraphStyle(name="TextRu", fontName="DejaVuSans", fontSize=11, leading=16, alignment=TA_LEFT, spaceAfter=6))

def _table(data: List[List[str]]) -> Table:
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), "DejaVuSans"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.whitesmoke),
        ("ALIGN", (0,0), (-1,0), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
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

Дай развёрнутую интерпретацию на русском:
1. Основные черты личности и жизненный путь
2. Таланты и сильные стороны
3. Зоны роста и рекомендации
4. Краткое резюме

Пиши понятно и по-человечески, избегай сложных терминов."""

    interpretation = await gpt_interpret(prompt, max_tokens=3000)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story = [
        Paragraph("Натальная карта (Placidus)", styles["TitleRu"]),
        Paragraph(f"Дата и время: {dt_loc} ({tz})", styles["TextRu"]),
        Spacer(1, 8),
        _table([["Элемент", "Значение"], ["ASC", chart_data.get("asc", "—")], ["MC", chart_data.get("mc", "—")]]),
        Spacer(1, 12)
    ]
    rows = [["Планета","Долгота","Знак","R"]]
    for p in planets:
        rows.append([p["name"], f"{round(p['lon'], 2)}°", p.get("sign", "—"), "R" if p.get("retro") else ""])
    story += [Paragraph("Планеты", styles["HeadRu"]), _table(rows), PageBreak()]
    story += [Paragraph("Интерпретация", styles["HeadRu"]), Paragraph(interpretation.replace('\n', '<br/>'), styles["TextRu"])]
    doc.build(story)
    return buf.getvalue()

async def build_pdf_horary(chart_data: Dict[str, Any], question: str) -> bytes:
    planets = chart_data.get("planets", [])
    dt_loc = chart_data.get("datetime_local", "—")
    tz = chart_data.get("iana_tz", "—")
    planets_str = "\n".join([f"{p['name']}: {p.get('sign', '?')} {round(p['lon'] % 30, 1)}°" for p in planets])

    prompt = f"""Проанализируй хорарную карту для вопроса: "{question}"

Момент вопроса: {dt_loc}
ASC: {chart_data.get('asc', '—')}
MC: {chart_data.get('mc', '—')}

Планеты:
{planets_str}

Дай чёткий ответ на хорарный вопрос:
1. Основной вывод (да/нет/при условии)
2. Астрологическое обоснование
3. Сроки (если применимо)
4. Рекомендации

Пиши ясно и конкретно."""

    interpretation = await gpt_interpret(prompt, max_tokens=2000)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story = [
        Paragraph("Хорарная карта (Regiomontanus)", styles["TitleRu"]),
        Paragraph(f"Вопрос: {question}", styles["HeadRu"]),
        Paragraph(f"Момент: {dt_loc} ({tz})", styles["TextRu"]),
        Spacer(1, 8),
        _table([["ASC", chart_data.get("asc", "—")], ["MC", chart_data.get("mc", "—")]]),
        Spacer(1, 12),
        Paragraph("Ответ", styles["HeadRu"]),
        Paragraph(interpretation.replace('\n', '<br/>'), styles["TextRu"])
    ]
    doc.build(story)
    return buf.getvalue()

async def build_pdf_synastry(synastry_data: Dict[str, Any]) -> bytes:
    chart_a = synastry_data["chart_a"]
    chart_b = synastry_data["chart_b"]

    planets_a = "\n".join([f"{p['name']}: {p.get('sign', '?')}" for p in chart_a.get("planets", [])])
    planets_b = "\n".join([f"{p['name']}: {p.get('sign', '?')}" for p in chart_b.get("planets", [])])

    prompt = f"""Проанализируй синастрию двух людей:

Карта A:
{planets_a}

Карта B:
{planets_b}

Опиши совместимость:
1. Зоны притяжения и гармонии
2. Зоны напряжения и роста
3. Как лучше взаимодействовать
4. Общий прогноз отношений

Пиши тепло и практично."""

    interpretation = await gpt_interpret(prompt, max_tokens=2500)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story = [
        Paragraph("Синастрия", styles["TitleRu"]),
        Paragraph("Анализ совместимости", styles["HeadRu"]),
        Spacer(1, 8),
        Paragraph(interpretation.replace('\n', '<br/>'), styles["TextRu"])
    ]
    doc.build(story)
    return buf.getvalue()

# ====== КНОПКИ ======
def upsell_keyboard(service_type: str) -> InlineKeyboardMarkup:
    buttons = []
    if service_type == "horary":
        buttons = [
            [InlineKeyboardButton(text="🔮 Ещё один вопрос (300₽)", callback_data="buy_horary")],
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
        await bot.send_message(chat_id, "⏳ Рассчитываю карту и готовлю интерпретацию...", parse_mode=None)

        if kind == "natal":
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_chart(args["dt"], lat, lon, tz, house_system="P")
            pdf = await build_pdf_natal(chart)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "natal.pdf"), caption="✨ Ваша натальная карта готова!\n\nХотите узнать больше?", reply_markup=upsell_keyboard("natal"))

        elif kind == "horary":
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_horary(args["dt"], lat, lon, tz)
            question = user_questions.get(chat_id, "Ваш вопрос")
            pdf = await build_pdf_horary(chart, question)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "horary.pdf"), caption="🔮 Ответ на ваш вопрос готов!\n\nЧто ещё вас интересует?", reply_markup=upsell_keyboard("horary"))

        else:  # synastry
            a, b = args["a"], args["b"]
            lat_a, lon_a, tz_a = await get_location(a["city"], a["country"])
            lat_b, lon_b, tz_b = await get_location(b["city"], b["country"])
            syn = calculate_synastry(a["dt"], lat_a, lon_a, tz_a, b["dt"], lat_b, lon_b, tz_b)
            pdf = await build_pdf_synastry(syn)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "synastry.pdf"), caption="💑 Анализ совместимости готов!\n\nХотите углубиться?", reply_markup=upsell_keyboard("synastry"))

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

# ====== HANDLERS ======
@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Натальная карта", callback_data="info_natal")],
        [InlineKeyboardButton(text="🔮 Хорарный вопрос", callback_data="info_horary")],
        [InlineKeyboardButton(text="💑 Синастрия (совместимость)", callback_data="info_synastry")],
    ])
    await m.answer("Привет! Я астролог-бот с ИИ. Выберите услугу:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("info_"))
async def info_callback(callback: types.CallbackQuery):
    service = callback.data.replace("info_", "")
    texts = {
        "natal": "⭐ <b>Натальная карта</b>\n\nФормат: /natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\nПример: /natal 17.08.2002, 15:20, Кострома, Россия\nСтоимость: 1000₽",
        "horary": "🔮 <b>Хорарный вопрос</b>\n\nЗадайте вопрос, отправьте дату. Пример:\n/horary 07.11.2025, 14:30, Москва, Россия\nСтоимость: 300₽",
        "synastry": "💑 <b>Синастрия</b>\n\nФормат:\n/synastry\nA: ДД.ММ.ГГГГ, ЧЧ:ММ, Город,Страна\nB: ДД.ММ.ГГГГ, ЧЧ:ММ, Город,Страна\nСтоимость: 900₽"
    }
    await callback.message.answer(texts.get(service, "Неизвестная услуга"))
    await callback.answer()

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def save_question(m: types.Message):
    user_questions[m.chat.id] = m.text
    await m.answer("Вопрос принят! Теперь отправьте данные:\n/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")

@dp.message(Command("natal"))
async def natal(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer("Формат: /natal 17.08.2002, 15:20, Кострома, Россия")
    await m.answer("✅ Принято! Считаю натальную карту...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "natal", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("horary"))
async def horary(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer("Формат: /horary 03.11.2025, 19:05, Москва, Россия")
    await m.answer("✅ Принято! Считаю хорарную карту...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "horary", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("synastry"))
async def synastry(m: types.Message):
    lines = m.text.splitlines()
    if len(lines) < 3:
        return await m.answer("После /synastry пришлите две строки:\nA: ...\nB: ...")
    try:
        a_str = lines[1].split(":", 1)[-1].strip()
        b_str = lines[2].split(":", 1)[-1].strip()
        dt_a, city_a, country_a = _parse_line(a_str)
        dt_b, city_b, country_b = _parse_line(b_str)
    except Exception:
        return await m.answer("Пример:\nA: 17.08.2002, 15:20, Кострома, Россия\nB: 04.07.1995, 12:00, Москва, Россия")
    await m.answer("✅ Принято! Считаю синастрию...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "synastry", {
        "a": {"dt": dt_a, "city": city_a, "country": country_a},
        "b": {"dt": dt_b, "city": city_b, "country": country_b}
    }))

@dp.callback_query(lambda c: c.data.startswith("buy_"))
async def handle_purchase(callback: types.CallbackQuery):
    service = callback.data.replace("buy_", "")
    await callback.message.answer(f"🛒 Для покупки услуги '{service}' напишите @your_username")
    await callback.answer()

# ====== FASTAPI ======
@app.get("/")
async def root():
    return PlainTextResponse("Astro Bot is running")

@app.get("/health")
async def health():
    return {"ok": True}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        update = types.Update(**await request.json())
        await dp.feed_update(bot, update)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

@app.on_event("startup")
async def on_startup():
    await bot.delete_webhook(drop_pending_updates=True)
    print("🗑️ Старый webhook удалён")
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(webhook_url, drop_pending_updates=True)
            info = await bot.get_webhook_info()
            print(f"✅ Webhook установлен: {info.url}")
        except Exception as e:
            print(f"❌ Ошибка webhook: {e}")
            print("⚠️ Запускаю polling...")
            asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    else:
        print("⚠️ WEBHOOK_URL не установлен, запускаю polling")
        asyncio.create_task(dp.start_polling(bot, skip_updates=True))

@app.on_event("shutdown")
async def on_shutdown():
    await client.aclose()
    await bot.session.close()
