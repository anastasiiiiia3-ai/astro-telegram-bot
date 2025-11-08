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
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
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
client = httpx.AsyncClient(timeout=120)

# ====== ASTRO CALCULATION MODULE ======
from astro_calc import get_location, calculate_chart, calculate_horary, calculate_synastry

# ====== UTILS ======
def split_into_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in text.split('\n\n') if p.strip()]

def paragraph_flowables(text: str) -> List[Paragraph]:
    return [Paragraph(p, styles["TextRu"]) for p in split_into_paragraphs(text)]

# ====== GPT INTERPRETATION ======
async def gpt_interpret(prompt: str, max_tokens: int = 3000) -> str:
    try:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system",
                     "content": "Ты профессиональный астролог с 15-летним опытом. "
                                "Пиши простым, понятным языком, избегай сложной терминологии."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7
            },
            timeout=90.0
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return "⚠️ Не удалось получить интерпретацию. Попробуйте позже."

# ====== PDF STYLE ======
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVuSans", fontSize=20, leading=24,
                          alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#2c3e50")))
styles.add(ParagraphStyle(name="SectionRu", fontName="DejaVuSans", fontSize=14, leading=18,
                          alignment=TA_LEFT, spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#34495e")))
styles.add(ParagraphStyle(name="TextRu", fontName="DejaVuSans", fontSize=11, leading=16,
                          alignment=TA_JUSTIFY, spaceAfter=10))
styles.add(ParagraphStyle(name="IntroRu", fontName="DejaVuSans", fontSize=11, leading=14,
                          alignment=TA_CENTER, spaceAfter=15, textColor=colors.grey))

# ====== PDF BUILDERS ======

async def build_pdf_natal(chart_data: Dict[str, Any]) -> bytes:
    dt_raw = chart_data.get("datetime_local", "—")
    city = chart_data.get("city", "—")
    country = chart_data.get("country", "—")
    try:
        from datetime import datetime
        dt_obj = datetime.fromisoformat(dt_raw)
        dt_str = dt_obj.strftime("%H:%M, %d.%m.%Y")
    except Exception:
        dt_str = dt_raw
    location_str = f"{city}, {country}"
    header_line = f"Дата и время рождения: {dt_str}\nМесто рождения: {location_str}"

    official_data_note = (
        "Обратите внимание: я не просто чат-бот на базе GPT. "
        "Все мои расчёты основаны на данных официальных астрологических сервисов, "
        "поэтому результаты максимально точные и надёжные."
    )

    prompt = f"""Дата рождения: {dt_str}
Место рождения: {location_str}

Опиши общую характеристику личности простым языком, без терминов астрологии.
Также учитывай, что данные получены из официальных астросервисов, чтобы анализ был максимально точным.

{official_data_note}"""

    interpretation = await gpt_interpret(prompt, 3000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50,
                            leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("НАТАЛЬНАЯ КАРТА", styles["TitleRu"]))
    story.append(Paragraph(header_line, styles["IntroRu"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(official_data_note, styles["IntroRu"]))
    story.append(Spacer(1, 15))
    story.extend(paragraph_flowables(interpretation))

    doc.build(story)
    return buf.getvalue()

async def build_pdf_horary(chart_data: Dict[str, Any], question: str) -> bytes:
    dt_raw = chart_data.get("datetime_local", "—")
    city = chart_data.get("city", "—")
    country = chart_data.get("country", "—")
    try:
        from datetime import datetime
        dt_obj = datetime.fromisoformat(dt_raw)
        dt_str = dt_obj.strftime("%H:%M, %d.%m.%Y")
    except Exception:
        dt_str = dt_raw
    location_str = f"{city}, {country}"
    header_line = f"Дата и время вопроса: {dt_str}\nМесто нахождения: {location_str}"

    prompt = f"""Вопрос: "{question}"
Дата и время вопроса: {dt_str}
Место нахождения: {location_str}

Дай развёрнутый, плавный и понятный ответ на вопрос, избегая терминов планет, домов, аспектов.
Объясни, что в карте есть указатели, которые показывают ситуацию, и постепенно раскрой суть.
Начни с основного вывода, затем расскажи детали, потом дай рекомендации для действий.
Пиши дружелюбно и емко."""

    interpretation = await gpt_interpret(prompt, 1500)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50,
                            leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("ХОРАРНЫЙ ВОПРОС", styles["TitleRu"]))
    story.append(Paragraph(header_line, styles["IntroRu"]))
    story.append(Spacer(1, 18))

    story.append(Paragraph("Ответ астролога", styles["SectionRu"]))
    story.extend(paragraph_flowables(interpretation))

    doc.build(story)
    return buf.getvalue()

async def build_pdf_synastry(synastry_data: Dict[str, Any]) -> bytes:
    prompt = (
        "Опиши совместимость двух людей простыми словами, "
        "объясни сильные стороны пары, возможные сложности и дай практические советы для гармоничных отношений."
    )
    interpretation = await gpt_interpret(prompt, 3000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50,
                            leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("АНАЛИЗ СОВМЕСТИМОСТИ", styles["TitleRu"]))
    story.append(Spacer(1, 10))
    story.extend(paragraph_flowables(interpretation))

    doc.build(story)
    return buf.getvalue()

# ====== INLINE KEYBOARD ======
def upsell_keyboard(service_type: str) -> InlineKeyboardMarkup:
    buttons = []
    if service_type == "horary":
        buttons = [
            [InlineKeyboardButton(text="🔮 Задать ещё вопрос (100₽)", callback_data="buy_horary")],
            [InlineKeyboardButton(text="⭐ Заказать натальную карту (300₽)", callback_data="buy_natal")],
            [InlineKeyboardButton(text="💑 Заказать синастрию (300₽)", callback_data="buy_synastry")]
        ]
    elif service_type == "natal":
        buttons = [
            [InlineKeyboardButton(text="💑 Заказать синастрию (300₽)", callback_data="buy_synastry")],
            [InlineKeyboardButton(text="🔮 Задать хорарный вопрос (100₽)", callback_data="buy_horary")]
        ]
    else:
        buttons = [
            [InlineKeyboardButton(text="🔮 Задать хорарный вопрос (100₽)", callback_data="buy_horary")],
            [InlineKeyboardButton(text="⭐ Заказать натальную карту (300₽)", callback_data="buy_natal")]
        ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

user_questions: Dict[int, str] = {}

async def build_and_send_pdf(chat_id: int, kind: str, args: Dict[str, Any]):
    try:
        await bot.send_message(chat_id, "⏳ Готовлю анализ... Это займёт около минуты.", parse_mode=None)

        if kind == "horary":
            question = user_questions.get(chat_id)
            if not question or question.strip() == "":
                await bot.send_message(chat_id, "⚠️ Пожалуйста, сначала отправьте мне ваш вопрос текстом, а затем команду /horary с датой и местом.")
                return
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_horary(args["dt"], lat, lon, tz)
            chart["city"] = args["city"]
            chart["country"] = args["country"]
            pdf = await build_pdf_horary(chart, question)
            await bot.send_document(
                chat_id,
                types.BufferedInputFile(pdf, "horarny_otvet.pdf"),
                caption="🔮 Ответ на ваш вопрос готов!",
                reply_markup=upsell_keyboard("horary")
            )
            user_questions.pop(chat_id, None)
            return

        if kind == "natal":
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_chart(args["dt"], lat, lon, tz, house_system="P")
            chart["city"] = args["city"]
            chart["country"] = args["country"]
            pdf = await build_pdf_natal(chart)
            await bot.send_document(
                chat_id,
                types.BufferedInputFile(pdf, "natalnaya_karta.pdf"),
                caption="✨ Ваша натальная карта готова!",
                reply_markup=upsell_keyboard("natal")
            )
            return

        if kind == "synastry":
            a, b = args["a"], args["b"]
            lat_a, lon_a, tz_a = await get_location(a["city"], a["country"])
            lat_b, lon_b, tz_b = await get_location(b["city"], b["country"])
            syn = calculate_synastry(a["dt"], lat_a, lon_a, tz_a, b["dt"], lat_b, lon_b, tz_b)
            pdf = await build_pdf_synastry(syn)
            await bot.send_document(
                chat_id,
                types.BufferedInputFile(pdf, "sinastriya.pdf"),
                caption="💑 Анализ совместимости готов!",
                reply_markup=upsell_keyboard("synastry")
            )
            return

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        await bot.send_message(chat_id, "⚠️ Ошибка при создании анализа. Попробуйте ещё раз позже.")

def _parse_line(s: str):
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 4:
        raise ValueError("Формат: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    dd, mm, yy = parts[0].split(".")
    dt = f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}T{parts[1]}"
    return dt, parts[2], ",".join(parts[3:])

# ====== HANDLERS ======

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def save_question(m: types.Message):
    user_questions[m.chat.id] = m.text.strip()
    await m.answer(
        "✅ Вопрос принят!\n\n"
        "Теперь отправьте команду /horary с датой и временем, и указанием места.\n"
        "Пример:\n"
        "/horary 15.12.2025, 14:30, Москва, Россия"
    )

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Натальная карта (300₽)", callback_data="info_natal")],
        [InlineKeyboardButton(text="🔮 Хорарный вопрос (100₽)", callback_data="info_horary")],
        [InlineKeyboardButton(text="💑 Синастрия (300₽)", callback_data="info_synastry")],
    ])
    await m.answer(
        "Привет! 👋\n\n"
        "Я астролог-бот на базе GPT, но мои расчёты основаны на официальных астрологических сервисах, "
        "что гарантирует высокую точность.\n\n"
        "Доступные услуги:\n"
        "- Натальная карта: подробный разбор\n"
        "- Хорарный вопрос: ответ на конкретный вопрос (сначала задайте вопрос, потом дату и место)\n"
        "- Синастрия: совместимость пары\n\n"
        "Выберите услугу:",
        reply_markup=keyboard
    )

@dp.callback_query(lambda c: c.data.startswith("info_"))
async def info_callback(callback: types.CallbackQuery):
    service = callback.data.replace("info_", "")
    texts = {
        "natal": (
            "⭐ <b>Натальная карта (300₽)</b>\n"
            "Подробный анализ личности, отношений, карьеры на 5+ страниц.\n\n"
            "Команда для заказа:\n"
            "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "Пример:\n"
            "/natal 17.08.2002, 15:20, Кострома, Россия\n\n"
            "🔎 Мои расчёты основаны на официальных сервисах, точность гарантирована."
        ),
        "horary": (
            "🔮 <b>Хорарный вопрос (100₽)</b>\n"
            "Получите ответ на конкретный вопрос.\n"
            "Примеры:\n"
            "- Заработаю ли я денег в новом проекте?\n"
            "- Сложатся ли у меня отношения с Васей?\n"
            "- Вернут ли мне долг?\n\n"
            "Сначала отправьте вопрос, затем используйте команду с датой и местом.\n"
            "Формат:\n"
            "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "Пример:\n"
            "/horary 07.11.2025, 14:30, Москва, Россия"
        ),
        "synastry": (
            "💑 <b>Синастрия (300₽)</b>\n"
            "Анализ совместимости двух людей на 3+ страницы.\n\n"
            "Команда для заказа:\n"
            "/synastry\n"
            "A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
            "Пример:\n"
            "/synastry\n"
            "A: 17.08.2002, 15:20, Кострома, Россия\n"
            "B: 04.07.1995, 12:00, Москва, Россия\n\n"
            "🔎 Все расчёты основаны на данных официальных астрологических сервисов."
        )
    }
    await callback.message.answer(texts.get(service, "Нет информации по этой услуге."))
    await callback.answer()

@dp.message(Command("natal"))
async def natal(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer("❌ Формат неверный!\nИспользуйте:\n/natal 17.08.2002, 15:20, Кострома, Россия")
    await m.answer("✅ Принято! Считаю натальную карту...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "natal", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("horary"))
async def horary(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer(
            "❌ Формат неверный!\nСначала отправьте вопрос текстом,\n"
            "затем используйте команду:\n"
            "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
        )
    await m.answer("✅ Принято! Ищу ответ на ваш вопрос...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "horary", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("synastry"))
async def synastry(m: types.Message):
    lines = m.text.splitlines()
    if len(lines) < 3:
        return await m.answer(
            "❌ Формат неверный!\nИспользуйте:\n"
            "/synastry\n"
            "A: 17.08.2002, 15:20, Кострома, Россия\n"
            "B: 04.07.1995, 12:00, Москва, Россия"
        )
    try:
        a_str = lines[1].split(":", 1)[-1].strip()
        b_str = lines[2].split(":", 1)[-1].strip()
        dt_a, city_a, country_a = _parse_line(a_str)
        dt_b, city_b, country_b = _parse_line(b_str)
    except Exception:
        return await m.answer("❌ Неверные данные! Проверьте формат.\nПример:\nA: 17.08.2002, 15:20, Кострома, Россия\nB: 04.07.1995, 12:00, Москва, Россия")
    await m.answer("✅ Принято! Анализирую совместимость...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "synastry", {
        "a": {"dt": dt_a, "city": city_a, "country": country_a},
        "b": {"dt": dt_b, "city": city_b, "country": country_b}
    }))

@dp.callback_query(lambda c: c.data.startswith("buy_"))
async def handle_purchase(callback: types.CallbackQuery):
    service_map = {
        "horary": "хорарный вопрос (100₽)",
        "natal": "натальную карту (300₽)",
        "synastry": "синастрию (300₽)"
    }
    service = callback.data.replace("buy_", "")
    await callback.message.answer(
        f"🛒 Чтобы заказать {service_map.get(service, service)}, напишите @your_username\n"
        "Или используйте соответствующую команду бота."
    )
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
