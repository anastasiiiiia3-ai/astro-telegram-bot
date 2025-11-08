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
client = httpx.AsyncClient(timeout=120)

# ====== ASTRO ======
from astro_calc import get_location, calculate_chart, calculate_horary, calculate_synastry

# ====== GPT ======
async def gpt_interpret(prompt: str, max_tokens: int = 3000) -> str:
    try:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "Ты профессиональный астролог с 15-летним опытом. Пиши простым, понятным языком на русском. Избегай технических астрологических терминов и деталей. Фокусируйся на практических советах и понятных объяснениях."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.7
            },
            timeout=90.0
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"К сожалению, не удалось получить интерпретацию. Пожалуйста, попробуйте позже."

# ====== PDF СТИЛИ ======
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVuSans", fontSize=20, leading=24, alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#2c3e50")))
styles.add(ParagraphStyle(name="SectionRu", fontName="DejaVuSans", fontSize=14, leading=18, alignment=TA_LEFT, spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#34495e"), bold=True))
styles.add(ParagraphStyle(name="TextRu", fontName="DejaVuSans", fontSize=11, leading=16, alignment=TA_JUSTIFY, spaceAfter=10))
styles.add(ParagraphStyle(name="IntroRu", fontName="DejaVuSans", fontSize=10, leading=14, alignment=TA_CENTER, spaceAfter=12, textColor=colors.grey))

async def build_pdf_natal(chart_data: Dict[str, Any]) -> bytes:
    dt_loc = chart_data.get("datetime_local", "—")
    
    # Промпты для разных разделов
    prompt_overview = f"""Дата рождения: {dt_loc}

Напиши краткую общую характеристику личности человека, родившегося в это время. Расскажи о его основных качествах, жизненном пути и предназначении. 

Пиши простым языком, БЕЗ упоминания планет, знаков зодиака, домов и аспектов. Только понятные характеристики личности."""

    prompt_love = f"""Дата рождения: {dt_loc}

Опиши подробно тему любви и отношений для этого человека:
- Как он проявляется в отношениях
- Какой партнер ему подходит
- Склонность к браку и количество возможных браков
- Особенности в интимной сфере
- Советы для гармоничных отношений

Пиши простым языком, понятно обычному человеку, БЕЗ астрологических терминов."""

    prompt_career = f"""Дата рождения: {dt_loc}

Проанализируй карьеру и финансовую сферу:
- В каких профессиях человек будет успешен
- Какие таланты помогут в работе
- Отношение к деньгам и финансовое благополучие
- Возможные сложности в карьере
- Рекомендации для профессионального роста

Пиши простым языком, без технических астрологических деталей."""

    prompt_health = f"""Дата рождения: {dt_loc}

Расскажи о здоровье и образе жизни:
- На что обратить внимание в здоровье
- Какой образ жизни подходит
- Рекомендации по поддержанию здоровья
- Психологическое состояние и эмоции

Пиши понятно, без медицинских диагнозов, только общие рекомендации."""

    prompt_growth = f"""Дата рождения: {dt_loc}

Дай практические рекомендации для личностного развития:
- Какие качества развивать
- Какие ловушки и слабости учитывать
- Как раскрыть свой потенциал
- Духовное развитие и жизненные уроки

Пиши вдохновляюще и понятно."""

    # Генерация интерпретаций
    overview = await gpt_interpret(prompt_overview, 800)
    love = await gpt_interpret(prompt_love, 900)
    career = await gpt_interpret(prompt_career, 900)
    health = await gpt_interpret(prompt_health, 700)
    growth = await gpt_interpret(prompt_growth, 700)

    # Создание PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50, leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("НАТАЛЬНАЯ КАРТА", styles["TitleRu"]))
    story.append(Paragraph(f"Составлена {dt_loc}", styles["IntroRu"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph("Общая характеристика личности", styles["SectionRu"]))
    story.append(Paragraph(overview, styles["TextRu"]))
    story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Любовь и отношения", styles["SectionRu"]))
    story.append(Paragraph(love, styles["TextRu"]))
    story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Карьера и финансы", styles["SectionRu"]))
    story.append(Paragraph(career, styles["TextRu"]))
    story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Здоровье и образ жизни", styles["SectionRu"]))
    story.append(Paragraph(health, styles["TextRu"]))
    story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Рекомендации для развития", styles["SectionRu"]))
    story.append(Paragraph(growth, styles["TextRu"]))

    doc.build(story)
    return buf.getvalue()

async def build_pdf_horary(chart_data: Dict[str, Any], question: str) -> bytes:
    dt_loc = chart_data.get("datetime_local", "—")

    prompt = f"""Хорарный вопрос: "{question}"
Момент вопроса: {dt_loc}

Дай чёткий ответ на этот вопрос:
1. Прямой ответ (да/нет/зависит от условий)
2. Объяснение ситуации простыми словами
3. Что повлияет на исход
4. Практические советы и действия

Пиши понятно, БЕЗ упоминания планет, домов и аспектов. Как мудрый советчик."""

    interpretation = await gpt_interpret(prompt, 1500)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50, leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("ХОРАРНЫЙ ОТВЕТ", styles["TitleRu"]))
    story.append(Paragraph(f"Вопрос задан {dt_loc}", styles["IntroRu"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph(f"Ваш вопрос: {question}", styles["SectionRu"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(interpretation, styles["TextRu"]))

    doc.build(story)
    return buf.getvalue()

async def build_pdf_synastry(synastry_data: Dict[str, Any]) -> bytes:
    
    prompt_overview = """Проанализируй совместимость двух людей.

Дай общую оценку отношений:
- Насколько они подходят друг другу
- Главные притягательные качества
- Общая энергетика пары

Пиши простым языком, понятно и тепло."""

    prompt_harmony = """Опиши зоны гармонии в отношениях:
- Что объединяет партнеров
- В чем они дополняют друг друга
- Какие сферы будут благоприятными
- Радости и удовольствия в паре

Пиши позитивно и вдохновляюще."""

    prompt_challenges = """Опиши возможные сложности и конфликты:
- Зоны напряжения
- Что может вызывать разногласия
- Как преодолевать трудности
- Уроки для роста пары

Пиши конструктивно, с акцентом на развитие."""

    prompt_advice = """Дай практические советы для улучшения отношений:
- Как лучше взаимодействовать
- На что обратить внимание
- Как укрепить связь
- Прогноз развития отношений

Пиши мудро и с теплотой."""

    overview = await gpt_interpret(prompt_overview, 800)
    harmony = await gpt_interpret(prompt_harmony, 900)
    challenges = await gpt_interpret(prompt_challenges, 900)
    advice = await gpt_interpret(prompt_advice, 800)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50, leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("АНАЛИЗ СОВМЕСТИМОСТИ", styles["TitleRu"]))
    story.append(Paragraph("Синастрия отношений", styles["IntroRu"]))
    story.append(Spacer(1, 20))

    story.append(Paragraph("Общая совместимость", styles["SectionRu"]))
    story.append(Paragraph(overview, styles["TextRu"]))
    story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Гармоничные аспекты", styles["SectionRu"]))
    story.append(Paragraph(harmony, styles["TextRu"]))
    story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Зоны роста и вызовы", styles["SectionRu"]))
    story.append(Paragraph(challenges, styles["TextRu"]))
    story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Рекомендации для пары", styles["SectionRu"]))
    story.append(Paragraph(advice, styles["TextRu"]))

    doc.build(story)
    return buf.getvalue()

# ====== КНОПКИ ======
def upsell_keyboard(service_type: str) -> InlineKeyboardMarkup:
    buttons = []
    if service_type == "horary":
        buttons = [
            [InlineKeyboardButton(text="🔮 Ещё один вопрос (100₽)", callback_data="buy_horary")],
            [InlineKeyboardButton(text="⭐ Натальная карта (300₽)", callback_data="buy_natal")],
            [InlineKeyboardButton(text="💑 Синастрия (300₽)", callback_data="buy_synastry")]
        ]
    elif service_type == "natal":
        buttons = [
            [InlineKeyboardButton(text="💑 Синастрия с партнёром (300₽)", callback_data="buy_synastry")],
            [InlineKeyboardButton(text="🔮 Задать хорарный вопрос (100₽)", callback_data="buy_horary")]
        ]
    else:
        buttons = [
            [InlineKeyboardButton(text="🔮 Хорарный вопрос (100₽)", callback_data="buy_horary")],
            [InlineKeyboardButton(text="⭐ Натальная карта (300₽)", callback_data="buy_natal")]
        ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

user_questions = {}

async def build_and_send_pdf(chat_id: int, kind: str, args: Dict[str, Any]):
    try:
        await bot.send_message(chat_id, "⏳ Готовлю ваш астрологический анализ... Это займёт около минуты.", parse_mode=None)

        if kind == "natal":
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_chart(args["dt"], lat, lon, tz, house_system="P")
            pdf = await build_pdf_natal(chart)
            await bot.send_document(
                chat_id, 
                types.BufferedInputFile(pdf, "natalnaya_karta.pdf"), 
                caption="✨ Ваша натальная карта готова!\n\nЭто подробный анализ вашей личности, отношений, карьеры и жизненного пути.",
                reply_markup=upsell_keyboard("natal")
            )

        elif kind == "horary":
            lat, lon, tz = await get_location(args["city"], args["country"])
            chart = calculate_horary(args["dt"], lat, lon, tz)
            question = user_questions.get(chat_id, "Ваш вопрос")
            pdf = await build_pdf_horary(chart, question)
            await bot.send_document(
                chat_id, 
                types.BufferedInputFile(pdf, "horarny_otvet.pdf"), 
                caption="🔮 Ответ на ваш вопрос готов!",
                reply_markup=upsell_keyboard("horary")
            )

        else:  # synastry
            a, b = args["a"], args["b"]
            lat_a, lon_a, tz_a = await get_location(a["city"], a["country"])
            lat_b, lon_b, tz_b = await get_location(b["city"], b["country"])
            syn = calculate_synastry(a["dt"], lat_a, lon_a, tz_a, b["dt"], lat_b, lon_b, tz_b)
            pdf = await build_pdf_synastry(syn)
            await bot.send_document(
                chat_id, 
                types.BufferedInputFile(pdf, "sinastriya.pdf"), 
                caption="💑 Анализ вашей совместимости готов!",
                reply_markup=upsell_keyboard("synastry")
            )

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        await bot.send_message(chat_id, f"⚠️ Произошла ошибка при создании анализа. Пожалуйста, проверьте правильность введённых данных и попробуйте снова.")

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
        [InlineKeyboardButton(text="⭐ Натальная карта (300₽)", callback_data="info_natal")],
        [InlineKeyboardButton(text="🔮 Хорарный вопрос (100₽)", callback_data="info_horary")],
        [InlineKeyboardButton(text="💑 Синастрия (300₽)", callback_data="info_synastry")],
    ])
    await m.answer(
        "Привет! 👋\n\n"
        "Я астролог-бот с искусственным интеллектом. Помогу вам:\n\n"
        "⭐ Понять себя через натальную карту\n"
        "🔮 Ответить на важный вопрос\n"
        "💑 Проанализировать совместимость с партнёром\n\n"
        "Выберите услугу:",
        reply_markup=keyboard
    )

@dp.callback_query(lambda c: c.data.startswith("info_"))
async def info_callback(callback: types.CallbackQuery):
    service = callback.data.replace("info_", "")
    texts = {
        "natal": (
            "⭐ <b>Натальная карта (300₽)</b>\n\n"
            "Подробный анализ вашей личности на 5+ страниц:\n"
            "• Характер и жизненный путь\n"
            "• Любовь и отношения\n"
            "• Карьера и финансы\n"
            "• Здоровье и образ жизни\n"
            "• Рекомендации для развития\n\n"
            "<b>Формат:</b>\n"
            "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
            "<b>Пример:</b>\n"
            "/natal 17.08.2002, 15:20, Кострома, Россия"
        ),
        "horary": (
            "🔮 <b>Хорарный вопрос (100₽)</b>\n\n"
            "Получите ответ на конкретный вопрос:\n"
            "• Прямой ответ да/нет\n"
            "• Объяснение ситуации\n"
            "• Практические советы\n\n"
            "Сначала напишите свой вопрос, затем используйте команду с датой и местом.\n\n"
            "<b>Формат:</b>\n"
            "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
            "<b>Пример:</b>\n"
            "/horary 07.11.2025, 14:30, Москва, Россия"
        ),
        "synastry": (
            "💑 <b>Синастрия (300₽)</b>\n\n"
            "Анализ совместимости двух людей на 3+ страниц:\n"
            "• Общая совместимость\n"
            "• Гармоничные аспекты\n"
            "• Зоны роста\n"
            "• Практические советы\n\n"
            "<b>Формат:</b>\n"
            "/synastry\n"
            "A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
            "<b>Пример:</b>\n"
            "/synastry\n"
            "A: 17.08.2002, 15:20, Кострома, Россия\n"
            "B: 04.07.1995, 12:00, Москва, Россия"
        )
    }
    await callback.message.answer(texts.get(service, "Неизвестная услуга"))
    await callback.answer()

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def save_question(m: types.Message):
    user_questions[m.chat.id] = m.text
    await m.answer(
        "✅ Вопрос принят!\n\n"
        "Теперь отправьте данные для расчёта:\n"
        "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
        "Например:\n"
        "/horary 08.11.2025, 12:00, Москва, Россия"
    )

@dp.message(Command("natal"))
async def natal(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer(
            "❌ Неверный формат!\n\n"
            "Используйте:\n"
            "/natal 17.08.2002, 15:20, Кострома, Россия"
        )
    await m.answer("✅ Принято! Начинаю расчёт вашей натальной карты...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "natal", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("horary"))
async def horary(m: types.Message):
    try:
        arg = m.text.split(" ", 1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer(
            "❌ Неверный формат!\n\n"
            "Используйте:\n"
            "/horary 08.11.2025, 14:30, Москва, Россия"
        )
    await m.answer("✅ Принято! Ищу ответ на ваш вопрос...")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "horary", {"dt": dt, "city": city, "country": country}))

@dp.message(Command("synastry"))
async def synastry(m: types.Message):
    lines = m.text.splitlines()
    if len(lines) < 3:
        return await m.answer(
            "❌ Неверный формат!\n\n"
            "Используйте:\n"
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
        return await m.answer(
            "❌ Неверный формат данных!\n\n"
            "Проверьте пример:\n"
            "A: 17.08.2002, 15:20, Кострома, Россия\n"
            "B: 04.07.1995, 12:00, Москва, Россия"
        )
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
        f"🛒 Для заказа услуги '{service_map.get(service, service)}' свяжитесь с @your_username\n\n"
        f"Или используйте соответствующую команду прямо здесь!"
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
