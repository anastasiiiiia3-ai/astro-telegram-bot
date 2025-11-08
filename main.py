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

# ====== Утилита для разбивки на абзацы ======
def split_into_paragraphs(text: str) -> List[str]:
    # Разбиваем по парам переводов строк, игнорируем пустые
    paras = [p.strip() for p in text.split('\n\n') if p.strip()]
    return paras

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
    except Exception:
        return "К сожалению, не удалось получить интерпретацию. Пожалуйста, попробуйте позже."

# ====== PDF СТИЛИ ======
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVuSans", fontSize=20, leading=24, alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#2c3e50")))
styles.add(ParagraphStyle(name="SectionRu", fontName="DejaVuSans", fontSize=14, leading=18, alignment=TA_LEFT, spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#34495e"), bold=True))
styles.add(ParagraphStyle(name="TextRu", fontName="DejaVuSans", fontSize=11, leading=16, alignment=TA_JUSTIFY, spaceAfter=10))
styles.add(ParagraphStyle(name="IntroRu", fontName="DejaVuSans", fontSize=11, leading=14, alignment=TA_CENTER, spaceAfter=15, textColor=colors.grey))

def paragraph_flowables(text: str) -> List[Paragraph]:
    paras = split_into_paragraphs(text)
    return [Paragraph(p, styles["TextRu"]) for p in paras]

async def build_pdf_natal(chart_data: Dict[str, Any]) -> bytes:
    # Формируем понятную строку с датой и местом
    dt_raw = chart_data.get("datetime_local", "—")
    city = chart_data.get("city", "—")
    country = chart_data.get("country", "—")
    # Ожидаемая строка примерно '2025-11-08T12:34:00'
    # Переформатируем для читаемого вывода
    try:
        from datetime import datetime
        dt_obj = datetime.fromisoformat(dt_raw)
        dt_str = dt_obj.strftime("%H:%M, %d.%m.%Y")
    except Exception:
        dt_str = dt_raw

    location_str = f"{city}, {country}"
    header_line = f"Дата и время рождения: {dt_str}\nМесто рождения: {location_str}"

    # Промпты как раньше, без решёток, простой язык
    prompt_overview = f"""Дата рождения: {dt_str}
Место рождения: {location_str}

Опиши общую характеристику личности, основные качества, жизненный путь и предназначение простыми словами. Избегай технических терминов и перечислений планет, домов или знаков."""

    prompt_love = f"""Дата рождения: {dt_str}
Место рождения: {location_str}

Расскажи про любовь и отношения: как человек проявляется в чувствах, каким партнёр подходит, особенности в близких отношениях, советы для гармонии. Пиши простым языком."""

    prompt_career = f"""Дата рождения: {dt_str}
Место рождения: {location_str}

Опиши карьеру и финансы: в каких сферах человек успешен, таланты, отношение к деньгам и советы для роста. Простой и понятный язык."""

    prompt_health = f"""Дата рождения: {dt_str}
Место рождения: {location_str}

Расскажи про здоровье и образ жизни: на что обратить внимание, рекомендуемый образ жизни, общие советы. Пиши понятно, без медицинских терминов."""

    prompt_growth = f"""Дата рождения: {dt_str}
Место рождения: {location_str}

Дай советы для личностного роста: что развивать, на что обращать внимание, как раскрыть потенциал. Пиши вдохновляюще и понятно."""

    overview = await gpt_interpret(prompt_overview, 800)
    love = await gpt_interpret(prompt_love, 900)
    career = await gpt_interpret(prompt_career, 900)
    health = await gpt_interpret(prompt_health, 700)
    growth = await gpt_interpret(prompt_growth, 700)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50, leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("НАТАЛЬНАЯ КАРТА", styles["TitleRu"]))
    story.append(Paragraph(header_line, styles["IntroRu"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Общая характеристика личности", styles["SectionRu"]))
    story.extend(paragraph_flowables(overview))

    story.append(PageBreak())
    story.append(Paragraph("Любовь и отношения", styles["SectionRu"]))
    story.extend(paragraph_flowables(love))

    story.append(PageBreak())
    story.append(Paragraph("Карьера и финансы", styles["SectionRu"]))
    story.extend(paragraph_flowables(career))

    story.append(PageBreak())
    story.append(Paragraph("Здоровье и образ жизни", styles["SectionRu"]))
    story.extend(paragraph_flowables(health))

    story.append(PageBreak())
    story.append(Paragraph("Рекомендации для развития", styles["SectionRu"]))
    story.extend(paragraph_flowables(growth))

    doc.build(story)
    return buf.getvalue()


async def build_pdf_horary(chart_data: Dict[str, Any], question: str) -> bytes:
    # Для хорарной карты берем дату и город где задают вопрос, не рождения
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

    examples_text = (
        "Примеры вопросов, которые вы можете задать:\n"
        "- Договорится ли бизнес-партнёрство?\n"
        "- Как сложится ситуация с работой в ближайшие месяцы?\n"
        "- Стоит ли принимать это предложение?\n"
        "- Когда будет лучшее время для важных решений?\n\n"
        "Важно: в командах указывайте дату и время момент задавания вопроса, " 
        "а также место, где вы находитесь в этот момент, а не дату рождения."
    )

    prompt = f"""Вопрос: "{question}"
Дата и время вопроса: {dt_str}
Место нахождения: {location_str}

Дай чёткий и простой ответ на этот вопрос, избегая сложной астрологической терминологии. Объясни основную суть и дай практические советы."""

    interpretation = await gpt_interpret(prompt, 1500)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50, leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("ХОРАРНЫЙ ВОПРОС", styles["TitleRu"]))
    story.append(Paragraph(header_line, styles["IntroRu"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Описание", styles["SectionRu"]))
    for p in split_into_paragraphs(examples_text):
        story.append(Paragraph(p, styles["TextRu"]))
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 16))
    story.append(Paragraph("Ответ астролога", styles["SectionRu"]))
    story.extend(paragraph_flowables(interpretation))

    doc.build(story)
    return buf.getvalue()


async def build_pdf_synastry(synastry_data: Dict[str, Any]) -> bytes:
    prompt_overview = (
        "Оцените совместимость двух людей, расскажите, насколько они подходят друг другу, "
        "какие у них сильные стороны в отношениях, и дайте понятные советы для гармонии. "
        "Избегайте астрологических терминов."
    )
    prompt_harmony = (
        "Опишите, что объединяет эту пару, какие у них взаимные плюсы, "
        "и что приносит радость в их общении."
    )
    prompt_challenges = (
        "Расскажите о сложностях, которые могут возникать, и как их лучше решать "
        "для благополучия пары."
    )
    prompt_advice = (
        "Дайте конкретные практические рекомендации для улучшения отношений и их укрепления."
    )

    overview = await gpt_interpret(prompt_overview, 800)
    harmony = await gpt_interpret(prompt_harmony, 900)
    challenges = await gpt_interpret(prompt_challenges, 900)
    advice = await gpt_interpret(prompt_advice, 800)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50, leftMargin=60, rightMargin=60)
    story = []

    story.append(Paragraph("АНАЛИЗ СОВМЕСТИМОСТИ", styles["TitleRu"]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Общая совместимость", styles["SectionRu"]))
    story.extend(paragraph_flowables(overview))

    story.append(PageBreak())
    story.append(Paragraph("Гармоничные стороны отношений", styles["SectionRu"]))
    story.extend(paragraph_flowables(harmony))

    story.append(PageBreak())
    story.append(Paragraph("Сложности и вызовы", styles["SectionRu"]))
    story.extend(paragraph_flowables(challenges))

    story.append(PageBreak())
    story.append(Paragraph("Рекомендации для пары", styles["SectionRu"]))
    story.extend(paragraph_flowables(advice))

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
            [InlineKeyboardButton(text="💑 Синастрия (300₽)", callback_data="buy_synastry")],
            [InlineKeyboardButton(text="🔮 Хорарный вопрос (100₽)", callback_data="buy_horary")]
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
            # Добавляем в chart_data город и страну для отображения
            chart = calculate_chart(args["dt"], lat, lon, tz, house_system="P")
            chart["city"] = args["city"]
            chart["country"] = args["country"]
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
            chart["city"] = args["city"]
            chart["country"] = args["country"]
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
            "Сначала напишите свой вопрос, затем используйте команду с датой и местом.\n"
            "При указании даты и места учитывайте момент задавания вопроса, а не дату рождения.\n\n"
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
        "Теперь отправьте дату, время и место задавания вопроса:\n"
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
