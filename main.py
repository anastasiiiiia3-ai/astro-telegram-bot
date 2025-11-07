import os
import io
import asyncio
from typing import Any, Dict, List, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ====== ENV ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ASTRO_API = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # https://your-app.onrender.com
WEBHOOK_PATH = "/webhook"

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

bot = Bot(TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
app = FastAPI()

# ====== HTTP CLIENT ======
client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0, read=90.0))

class EphemerisTemporaryError(Exception):
    pass

@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    retry=retry_if_exception_type((httpx.TimeoutException, EphemerisTemporaryError)),
)
async def astro_post(path: str, json: dict):
    url = f"{ASTRO_API}{path}"
    try:
        r = await client.post(url, json=json)
    except httpx.TimeoutException:
        raise
    if r.status_code >= 500:
        raise EphemerisTemporaryError(f"{r.status_code} on {url}")
    r.raise_for_status()
    return r.json()

async def astro_health() -> bool:
    try:
        r = await client.get(f"{ASTRO_API}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False

# ====== OPENAI GPT ======
async def gpt_interpret(prompt: str, max_tokens: int = 2000) -> str:
    """Получить интерпретацию от ChatGPT"""
    try:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",  # или gpt-4o для лучшего качества
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты профессиональный астролог с 15-летним опытом. "
                                   "Твои интерпретации тёплые, понятные, без перегруза терминами. "
                                   "Фокус на практической пользе и поддержке человека. "
                                   "Пиши на русском языке."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7
            },
            timeout=60.0
        )
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"⚠️ Не удалось получить интерпретацию: {str(e)}"

# ====== PDF ======
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors

try:
    pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
except Exception:
    pass

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVu", fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=12))
styles.add(ParagraphStyle(name="HeadRu", fontName="DejaVu", fontSize=12, leading=16, alignment=TA_LEFT, spaceBefore=8, spaceAfter=6))
styles.add(ParagraphStyle(name="TextRu", fontName="DejaVu", fontSize=11, leading=16, alignment=TA_LEFT, spaceAfter=6))

def _table(data: List[List[str]]) -> Table:
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), "DejaVu"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.whitesmoke),
        ("ALIGN", (0,0), (-1,0), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    return t

async def build_pdf_natal(payload: Dict[str, Any]) -> bytes:
    chart = payload["chart"]
    planets = chart.get("planets", [])
    dt_loc = chart.get("datetime_local", "—")
    tz = chart.get("iana_tz", "—")

    # Формируем промпт для GPT
    planets_str = "\n".join([f"{p['name']}: {p.get('sign', '?')} {round(p['lon'], 1)}°" for p in planets])
    gpt_prompt = f"""Проанализируй натальную карту:

Дата: {dt_loc}
ASC: {chart.get('asc', '—')}
MC: {chart.get('mc', '—')}

Планеты:
{planets_str}

Дай развёрнутую интерпретацию на русском:
1. Основные черты личности и жизненный путь
2. Таланты и сильные стороны
3. Зоны роста и рекомендации
4. Краткое резюме

Пиши понятно и по-человечески, избегай сложных терминов."""

    interpretation = await gpt_interpret(gpt_prompt, max_tokens=3000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story: List[Any] = []

    story += [
        Paragraph("Натальная карта (Placidus)", styles["TitleRu"]),
        Paragraph(f"Дата и время: {dt_loc} ({tz})", styles["TextRu"]),
        Spacer(1, 8)
    ]
    
    story += [_table([["Элемент","Значение"],["ASC",chart.get("asc","—")],["MC",chart.get("mc","—")]]), Spacer(1, 12)]

    rows = [["Планета","Долгота","Знак","R"]]
    for p in planets:
        rows.append([p["name"], f"{round(p['lon'],2)}°", p.get("sign","—"), "R" if p.get("retro") else ""])
    story += [Paragraph("Планеты", styles["HeadRu"]), _table(rows), PageBreak()]

    # Добавляем интерпретацию от GPT
    story += [
        Paragraph("Интерпретация", styles["HeadRu"]),
        Paragraph(interpretation.replace('\n', '<br/>'), styles["TextRu"])
    ]

    doc.build(story)
    return buf.getvalue()

async def build_pdf_horary(payload: Dict[str, Any], question: str) -> bytes:
    chart = payload["chart"]
    planets = chart.get("planets", [])
    dt_loc = chart.get("datetime_local", "—")
    tz = chart.get("iana_tz", "—")

    planets_str = "\n".join([f"{p['name']}: {p.get('sign', '?')} {round(p['lon'], 1)}°" for p in planets])
    gpt_prompt = f"""Проанализируй хорарную карту для вопроса: "{question}"

Момент вопроса: {dt_loc}
ASC: {chart.get('asc', '—')}
MC: {chart.get('mc', '—')}

Планеты:
{planets_str}

Дай чёткий ответ на хорарный вопрос:
1. Основной вывод (да/нет/при условии)
2. Астрологическое обоснование
3. Сроки (если применимо)
4. Рекомендации

Пиши ясно и конкретно."""

    interpretation = await gpt_interpret(gpt_prompt, max_tokens=2000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story = []
    
    story += [
        Paragraph("Хорарная карта (Regiomontanus)", styles["TitleRu"]),
        Paragraph(f"Вопрос: {question}", styles["HeadRu"]),
        Paragraph(f"Момент: {dt_loc} ({tz})", styles["TextRu"]),
        Spacer(1, 8),
        _table([["ASC", chart.get("asc","—")], ["MC", chart.get("mc","—")]]),
        Spacer(1, 12),
        Paragraph("Ответ", styles["HeadRu"]),
        Paragraph(interpretation.replace('\n', '<br/>'), styles["TextRu"])
    ]
    
    doc.build(story)
    return buf.getvalue()

async def build_pdf_synastry(payload: Dict[str, Any]) -> bytes:
    da, db = payload["a"], payload["b"]
    
    planets_a = "\n".join([f"{p['name']}: {p.get('sign', '?')}" for p in da["chart"].get("planets", [])])
    planets_b = "\n".join([f"{p['name']}: {p.get('sign', '?')}" for p in db["chart"].get("planets", [])])
    
    gpt_prompt = f"""Проанализируй синастрию двух людей:

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

    interpretation = await gpt_interpret(gpt_prompt, max_tokens=2500)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story = []
    
    story += [
        Paragraph("Синастрия", styles["TitleRu"]),
        Paragraph("Анализ совместимости", styles["HeadRu"]),
        Spacer(1, 8),
        Paragraph(interpretation.replace('\n', '<br/>'), styles["TextRu"])
    ]
    
    doc.build(story)
    return buf.getvalue()

# ====== LOGIC ======
async def resolve_place(city: str, country: str) -> Tuple[float, float, str]:
    data = await astro_post("/api/resolve", {"city": city, "country": country})
    return float(data["lat"]), float(data["lon"]), str(data["iana_tz"])

def upsell_keyboard(service_type: str) -> InlineKeyboardMarkup:
    """Кнопки допродаж после получения результата"""
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
    else:  # synastry
        buttons = [
            [InlineKeyboardButton(text="📊 Транзиты для отношений (500₽)", callback_data="buy_transits_synastry")],
            [InlineKeyboardButton(text="⭐ Композитная карта (600₽)", callback_data="buy_composite")]
        ]
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Словарь для хранения вопросов пользователей (в продакшене использовать БД)
user_questions = {}

async def build_and_send_pdf(chat_id: int, kind: str, args: Dict[str, Any]):
    try:
        await bot.send_message(chat_id, "⏳ Рассчитываю карту и готовлю интерпретацию...")
        
        if kind == "natal":
            lat, lon, tz = await resolve_place(args["city"], args["country"])
            data = await astro_post("/api/chart", {
                "datetime_local": args["dt"], "lat": lat, "lon": lon,
                "iana_tz": tz, "house_system": "Placidus"
            })
            pdf = await build_pdf_natal(data)
            await bot.send_document(
                chat_id, 
                types.BufferedInputFile(pdf, "natal.pdf"), 
                caption="✨ Ваша натальная карта готова!\n\nХотите узнать больше?",
                reply_markup=upsell_keyboard("natal")
            )
            
        elif kind == "horary":
            lat, lon, tz = await resolve_place(args["city"], args["country"])
            data = await astro_post("/api/horary", {
                "datetime_local": args["dt"], "lat": lat, "lon": lon,
                "iana_tz": tz, "house_system": "Regiomontanus"
            })
            question = user_questions.get(chat_id, "Ваш вопрос")
            pdf = await build_pdf_horary(data, question)
            await bot.send_document(
                chat_id, 
                types.BufferedInputFile(pdf, "horary.pdf"), 
                caption="🔮 Ответ на ваш вопрос готов!\n\nЧто ещё вас интересует?",
                reply_markup=upsell_keyboard("horary")
            )
            
        else:  # synastry
            a, b = args["a"], args["b"]
            lat_a, lon_a, tz_a = await resolve_place(a["city"], a["country"])
            lat_b, lon_b, tz_b = await resolve_place(b["city"], b["country"])
            da = await astro_post("/api/chart", {"datetime_local": a["dt"], "lat": lat_a, "lon": lon_a, "iana_tz": tz_a, "house_system": "Placidus"})
            db = await astro_post("/api/chart", {"datetime_local": b["dt"], "lat": lat_b, "lon": lon_b, "iana_tz": tz_b, "house_system": "Placidus"})
            pdf = await build_pdf_synastry({"a": da, "b": db})
            await bot.send_document(
                chat_id, 
                types.BufferedInputFile(pdf, "synastry.pdf"), 
                caption="💑 Анализ совместимости готов!\n\nХотите углубиться?",
                reply_markup=upsell_keyboard("synastry")
            )
            
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Ошибка: {str(e)}\n\nПопробуйте позже или напишите /start")

# ====== PARSE ======
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
    
    await m.answer(
        "Привет! 🌟\n\n"
        "Я астролог-бот с искусственным интеллектом. "
        "Помогу разобраться в натальной карте, ответить на хорарные вопросы и посмотреть совместимость.\n\n"
        "Выбери услугу:",
        reply_markup=keyboard
    )

@dp.callback_query(lambda c: c.data.startswith("info_"))
async def info_callback(callback: types.CallbackQuery):
    service = callback.data.replace("info_", "")
    
    if service == "natal":
        text = (
            "⭐ <b>Натальная карта</b>\n\n"
            "Получите подробный разбор вашей личности, талантов и жизненного пути.\n\n"
            "📝 Формат: /natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "Пример: /natal 17.08.2002, 15:20, Кострома, Россия\n\n"
            "💰 Стоимость: 1000₽"
        )
    elif service == "horary":
        text = (
            "🔮 <b>Хорарный вопрос</b>\n\n"
            "Задайте конкретный вопрос и получите астрологический ответ.\n\n"
            "📝 Формат:\n"
            "1. Напишите вопрос\n"
            "2. /horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
            "Пример:\n"
            "Стоит ли менять работу?\n"
            "/horary 07.11.2025, 14:30, Москва, Россия\n\n"
            "💰 Стоимость: 300₽"
        )
    else:  # synastry
        text = (
            "💑 <b>Синастрия</b>\n\n"
            "Анализ совместимости двух людей.\n\n"
            "📝 Формат:\n"
            "/synastry\n"
            "A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
            "💰 Стоимость: 900₽"
        )
    
    await callback.message.answer(text)
    await callback.answer()

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def save_question(m: types.Message):
    """Сохраняем вопрос пользователя для хорара"""
    user_questions[m.chat.id] = m.text
    await m.answer("Вопрос принят! Теперь отправьте данные для расчёта:\n/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")

@dp.message(Command("natal"))
async def natal(m: types.Message):
    try:
        arg = m.text.split(" ",1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer("Формат: /natal 17.08.2002, 15:20, Кострома, Россия")
    await m.answer("✅ Принято! Считаю натальную карту...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "natal", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("horary"))
async def horary(m: types.Message):
    try:
        arg = m.text.split(" ",1)[1]
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
        a_str = lines[1].split(":",1)[-1].strip()
        b_str = lines[2].split(":",1)[-1].strip()
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
    """Обработка нажатия кнопок допродаж"""
    service = callback.data.replace("buy_", "")
    
    # Здесь будет интеграция с платежами
    await callback.message.answer(
        f"🛒 Отлично! Для покупки услуги '{service}' напишите @your_username или используйте /pay_{service}"
    )
    await callback.answer()

# ====== FASTAPI + WEBHOOK ======
@app.get("/")
async def root():
    return PlainTextResponse("Astro Bot is running")

@app.get("/health")
async def health():
    return {"ok": True, "astro_api": await astro_health()}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """Обработка webhook от Telegram"""
    try:
        update = types.Update(**await request.json())
        await dp.feed_update(bot, update)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

@app.on_event("startup")
async def on_startup():
    """Устанавливаем webhook при запуске"""
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(webhook_url, drop_pending_updates=True)
            print(f"✅ Webhook set to {webhook_url}")
        except Exception as e:
            print(f"❌ Webhook error: {e}")
            print("Starting polling instead...")
            asyncio.create_task(dp.start_polling(bot))
    else:
        print("⚠️ WEBHOOK_URL not set, starting polling mode")
        asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def on_shutdown():
    await client.aclose()
    await bot.session.close()
