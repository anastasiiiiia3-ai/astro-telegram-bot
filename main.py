import os
import io
import asyncio
from typing import Any, Dict

import httpx

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

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
                    {"role": "system", "content": "Ты профессиональный астролог. Пиши тепло и понятно на русском."},
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
        return f"Ошибка: {e}"

# ====== ПРОСТОЙ PDF ======

async def build_simple_pdf(title: str, content: str) -> bytes:
    """Создаёт простейший PDF без сложных стилей"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    
    # Заголовок
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, title)
    
    # Контент построчно
    c.setFont("Helvetica", 10)
    y = height - 100
    for line in content.split('\n'):
        if y < 50:
            c.showPage()
            y = height - 50
        c.drawString(50, y, line[:80])  # Обрезаем длинные строки
        y -= 15
    
    c.save()
    return buf.getvalue()

async def build_pdf_natal(chart_data: Dict[str, Any]) -> bytes:
    dt_loc = chart_data.get("datetime_local", "—")
    planets = chart_data.get("planets", [])
    
    planets_str = "\n".join([f"{p['name']}: {p.get('sign', '?')} {round(p['lon'] % 30, 1)}" for p in planets])
    prompt = f"Натальная карта: {dt_loc}\nASC: {chart_data.get('asc', '—')}\nПланеты:\n{planets_str}\n\nДай краткую интерпретацию."
    
    interpretation = await gpt_interpret(prompt, 1500)
    content = f"Дата: {dt_loc}\n\nПланеты:\n{planets_str}\n\nИнтерпретация:\n{interpretation}"
    
    return await build_simple_pdf("Натальная карта", content)

async def build_pdf_horary(chart_data: Dict[str, Any], question: str) -> bytes:
    dt_loc = chart_data.get("datetime_local", "—")
    planets = chart_data.get("planets", [])
    
    planets_str = "\n".join([f"{p['name']}: {p.get('sign', '?')}" for p in planets])
    prompt = f"Хорарный вопрос: {question}\nМомент: {dt_loc}\nПланеты:\n{planets_str}\n\nДай ответ."
    
    interpretation = await gpt_interpret(prompt, 1500)
    content = f"Вопрос: {question}\nМомент: {dt_loc}\n\nОтвет:\n{interpretation}"
    
    return await build_simple_pdf("Хорарная карта", content)

async def build_pdf_synastry(synastry_data: Dict[str, Any]) -> bytes:
    chart_a = synastry_data["chart_a"]
    chart_b = synastry_data["chart_b"]
    
    planets_a = "\n".join([f"{p['name']}: {p.get('sign', '?')}" for p in chart_a.get("planets", [])])
    planets_b = "\n".join([f"{p['name']}: {p.get('sign', '?')}" for p in chart_b.get("planets", [])])
    
    prompt = f"Синастрия двух людей.\nКарта A:\n{planets_a}\n\nКарта B:\n{planets_b}\n\nОпиши совместимость."
    interpretation = await gpt_interpret(prompt, 1500)
    
    content = f"Синастрия\n\nА: {planets_a}\n\nB: {planets_b}\n\nАнализ:\n{interpretation}"
    return await build_simple_pdf("Синастрия", content)

# ====== КНОПКИ ======

def upsell_keyboard(service_type: str) -> InlineKeyboardMarkup:
    buttons = []
    if service_type == "horary":
        buttons = [[InlineKeyboardButton(text="🔮 Ещё вопрос (300₽)", callback_data="buy_horary")]]
    elif service_type == "natal":
        buttons = [[InlineKeyboardButton(text="💑 Синастрия (900₽)", callback_data="buy_synastry")]]
    else:
        buttons = [[InlineKeyboardButton(text="📊 Транзиты (500₽)", callback_data="buy_transits")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

user_questions = {}

async def build_and_send_pdf(chat_id: int, kind: str, args: Dict[str, Any]):
    try:
        await bot.send_message(chat_id, "⏳ Рассчитываю...", parse_mode=None)

        if kind == "natal":
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_chart(args["dt"], lat, lon, tz, house_system="P")
            pdf = await build_pdf_natal(chart)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "natal.pdf"),
                                    caption="✨ Готово!", reply_markup=upsell_keyboard("natal"))

        elif kind == "horary":
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_horary(args["dt"], lat, lon, tz)
            question = user_questions.get(chat_id, "Ваш вопрос")
            pdf = await build_pdf_horary(chart, question)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "horary.pdf"),
                                    caption="🔮 Готово!", reply_markup=upsell_keyboard("horary"))

        else:
            a, b = args["a"], args["b"]
            lat_a, lon_a, tz_a = await get_location(a["city"], a["country"])
            lat_b, lon_b, tz_b = await get_location(b["city"], b["country"])
            syn = calculate_synastry(a["dt"], lat_a, lon_a, tz_a, b["dt"], lat_b, lon_b, tz_b)
            pdf = await build_pdf_synastry(syn)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "synastry.pdf"),
                                    caption="💑 Готово!", reply_markup=upsell_keyboard("synastry"))

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
        [InlineKeyboardButton(text="💑 Синастрия", callback_data="info_synastry")],
    ])
    await m.answer("Привет! Я астролог-бот. Выберите услугу:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("info_"))
async def info_callback(callback: types.CallbackQuery):
    service = callback.data.replace("info_", "")
    texts = {
        "natal": "⭐ Натальная карта\n/natal 17.08.2002, 15:20, Кострома, Россия",
        "horary": "🔮 Хорарный вопрос\n/horary 07.11.2025, 14:30, Москва, Россия",
        "synastry": "💑 Синастрия\n/synastry\nA: 17.08.2002, 15:20, Кострома, Россия\nB: 04.07.1995, 12:00, Москва, Россия"
    }
    await callback.message.answer(texts.get(service, "?"))
    await callback.answer()

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def save_question(m: types.Message):
    user_questions[m.chat.id] = m.text
    await m.answer("Вопрос принят! /horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")

@dp.message(Command("natal"))
async def natal(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except:
        return await m.answer("Формат: /natal 17.08.2002, 15:20, Кострома, Россия")
    await m.answer("✅ Принято!")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "natal", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("horary"))
async def horary(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except:
        return await m.answer("Формат: /horary 03.11.2025, 19:05, Москва, Россия")
    await m.answer("✅ Принято!")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "horary", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("synastry"))
async def synastry(m: types.Message):
    lines = m.text.splitlines()
    if len(lines) < 3:
        return await m.answer("После /synastry:\nA: ...\nB: ...")
    try:
        a_str = lines[1].split(":", 1)[-1].strip()
        b_str = lines[2].split(":", 1)[-1].strip()
        dt_a, city_a, country_a = _parse_line(a_str)
        dt_b, city_b, country_b = _parse_line(b_str)
    except:
        return await m.answer("Пример:\nA: 17.08.2002, 15:20, Кострома, Россия\nB: 04.07.1995, 12:00, Москва, Россия")
    await m.answer("✅ Принято!")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "synastry", {
        "a": {"dt": dt_a, "city": city_a, "country": country_a},
        "b": {"dt": dt_b, "city": city_b, "country": country_b}
    }))

@dp.callback_query(lambda c: c.data.startswith("buy_"))
async def handle_purchase(callback: types.CallbackQuery):
    await callback.message.answer("🛒 Для покупки напишите @your_username")
    await callback.answer()

# ====== FASTAPI ======

@app.get("/")
async def root():
    return PlainTextResponse("Bot OK")

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
    print("🗑️ Webhook удалён")
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(webhook_url, drop_pending_updates=True)
            info = await bot.get_webhook_info()
            print(f"✅ Webhook: {info.url}")
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    else:
        print("⚠️ Polling")
        asyncio.create_task(dp.start_polling(bot, skip_updates=True))

@app.on_event("shutdown")
async def on_shutdown():
    await client.aclose()
    await bot.session.close()
