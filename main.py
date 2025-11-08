import os
import io
import asyncio
from typing import Dict, List

import httpx

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors

from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

# Регистрируем шрифт DejaVuSans для русских символов и читаемого PDF
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
except Exception as err:
    print(f"Ошибка регистрации шрифта DejaVuSans: {err}")
    raise

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Обе переменные окружения TELEGRAM_TOKEN и OPENAI_API_KEY должны быть заданы!")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
client = httpx.AsyncClient(timeout=180)

# Стиль для PDF-документов
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(
    "TitleRu",
    fontName="DejaVuSans",
    fontSize=20,
    alignment=TA_CENTER,
    spaceAfter=20,
    textColor=colors.HexColor("#2c3e50")
))
styles.add(ParagraphStyle(
    "SectionRu",
    fontName="DejaVuSans",
    fontSize=14,
    alignment=TA_LEFT,
    spaceBefore=16,
    spaceAfter=10,
    textColor=colors.HexColor("#34495e")
))
styles.add(ParagraphStyle(
    "TextRu",
    fontName="DejaVuSans",
    fontSize=11,
    leading=16,
    alignment=TA_JUSTIFY,
    spaceAfter=10
))
styles.add(ParagraphStyle(
    "IntroRu",
    fontName="DejaVuSans",
    fontSize=11,
    alignment=TA_CENTER,
    spaceAfter=15,
    textColor=colors.gray
))

def split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]

def paragraphs_to_flowables(text: str) -> List[Paragraph]:
    return [Paragraph(p, styles["TextRu"]) for p in split_paragraphs(text)]

user_questions: Dict[int, str] = {}

async def openai_request(system_prompt: str, user_prompt: str, max_tokens: int = 3000) -> str:
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    try:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return "⚠️ Не удалось получить ответ от сервиса, попробуйте позже."

# Натальная карта — детальный анализ с разделами
async def build_pdf_natal(datetime_str: str, city: str, country: str) -> bytes:
    system_prompt = (
        "Ты профессиональный астролог с 15-летним опытом. Опиши натальную карту подробно и ясно, избегая сложной астрологической терминологии.\n"
        "Разбей текст на разделы:\n"
        "1) Общая характеристика личности\n"
        "2) Особенности характера и таланты\n"
        "3) Сфера отношений и партнерство\n"
        "4) Карьера и профессиональное развитие"
    )
    user_prompt = f"Дата рождения и время: {datetime_str}\nМесто рождения: {city}, {country}\nОпиши натальную карту."

    interpretation = await openai_request(system_prompt, user_prompt, max_tokens=3000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=60, rightMargin=60,
                            topMargin=50, bottomMargin=50)
    story = [
        Paragraph("НАТАЛЬНАЯ КАРТА", styles["TitleRu"]),
        Paragraph(f"Дата и время рождения: {datetime_str}", styles["IntroRu"]),
        Paragraph(f"Место рождения: {city}, {country}", styles["IntroRu"]),
        Spacer(1, 14),
    ]
    story.extend(paragraphs_to_flowables(interpretation))
    doc.build(story)
    return buf.getvalue()

# Хорарный вопрос — краткий ответ “Да/Нет”, пункты и совет, уточняющий вопрос
async def build_pdf_horary(datetime_str: str, city: str, country: str, question: str) -> bytes:
    system_prompt = (
        "Ты опытный астролог. Дай чёткий ответ в формате:\n"
        "1) Краткий ответ: «Да», «Нет» или «Скорее да/нет»\n"
        "2) 2-3 пункта пояснения\n"
        "3) Краткий совет\n"
        "Закончи одним конкретным уточняющим вопросом на тему, начиная словом «Хотите узнать:».\n"
        "Используй простой и понятный язык без терминов."
    )
    user_prompt = (
        f"Дата и время вопроса: {datetime_str}\n"
        f"Место: {city}, {country}\n"
        f"Вопрос: {question}"
    )
    response = await openai_request(system_prompt, user_prompt, max_tokens=1000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=60, rightMargin=60,
                            topMargin=50, bottomMargin=50)
    story = [
        Paragraph("ХОРАРНЫЙ ВОПРОС", styles["TitleRu"]),
        Paragraph(f"Дата и время: {datetime_str}", styles["IntroRu"]),
        Paragraph(f"Место: {city}, {country}", styles["IntroRu"]),
        Spacer(1, 14),
        Paragraph("Ответ:", styles["SectionRu"]),
    ]
    story.extend(paragraphs_to_flowables(response))
    doc.build(story)
    return buf.getvalue()

# Синастрия — взгляд на совместимость с сильными сторонами, проблемами и советами
async def build_pdf_synastry(dt_a: str, city_a: str, country_a: str,
                             dt_b: str, city_b: str, country_b: str) -> bytes:
    system_prompt = (
        "Ты профессиональный астролог. Сделай подробный анализ совместимости пары.\n"
        "Обязательно расскажи:\n"
        "1) Сильные стороны отношений и что их объединяет\n"
        "2) Возможные проблемы и трудности\n"
        "3) Советы и рекомендации для гармонии и роста отношений\n"
        "Пиши простым и понятным языком без терминов."
    )
    user_prompt = (
        f"Человек A: дата и время рождения {dt_a}, место {city_a}, {country_a}\n"
        f"Человек B: дата и время рождения {dt_b}, место {city_b}, {country_b}\n"
        "Выполни подробный разбор синастрии."
    )
    interpretation = await openai_request(system_prompt, user_prompt, max_tokens=3000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=60, rightMargin=60,
                            topMargin=50, bottomMargin=50)
    story = [
        Paragraph("СИНАСТРИЯ — АНАЛИЗ СОВМЕСТИМОСТИ", styles["TitleRu"]),
        Spacer(1, 14)
    ]
    story.extend(paragraphs_to_flowables(interpretation))
    doc.build(story)
    return buf.getvalue()

def parse_date_place(arg: str):
    parts = [p.strip() for p in arg.split(",")]
    if len(parts) < 4:
        raise ValueError("Ожидается формат: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    dd, mm, yyyy = parts[0].split(".")
    dt_iso = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{parts[1]}"
    city = parts[2]
    country = ",".join(parts[3:]).strip()
    return dt_iso, city, country

def parse_synastry(text: str):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    a_line = next((l for l in lines if l.upper().startswith("A:")), None)
    b_line = next((l for l in lines if l.upper().startswith("B:")), None)
    if not a_line or not b_line:
        raise ValueError("Должны быть строки с 'A:' и 'B:' для синастрии")
    dt_a, city_a, country_a = parse_date_place(a_line[2:].strip())
    dt_b, city_b, country_b = parse_date_place(b_line[2:].strip())
    return dt_a, city_a, country_a, dt_b, city_b, country_b

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def capture_user_question(message: types.Message):
    user_questions[message.chat.id] = message.text.strip()
    await message.answer(
        "✅ Вопрос принят! Теперь используйте команду с датой и местом для анализа.\n\n"
        "Примеры для использования команд:\n"
        "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна — хорарный вопрос\n"
        "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна — натальная карта\n"
        "/synastry\nA: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\nB: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна — синастрия\n\n"
        "Примеры хорарных вопросов:\n"
        "- Вернется ли ко мне Вася?\n"
        "- Удастся ли получить повышение?\n"
        "- Сложатся ли отношения с этим человеком?"
    )

@dp.message(Command("horary"))
async def horary_handler(message: types.Message):
    if message.chat.id not in user_questions:
        await message.answer("⚠️ Сначала отправьте в чат ваш вопрос текстом.")
        return
    try:
        arg = message.text.split(" ", 1)[1]
        dt, city, country = parse_date_place(arg)
    except Exception:
        await message.answer("❌ Неверный формат. Пример:\n/horary 08.11.2025, 14:30, Москва, Россия")
        return
    from datetime import datetime
    dt_str = datetime.fromisoformat(dt).strftime("%H:%M, %d.%m.%Y")
    await message.answer("⏳ Формирую ответ, подождите немного...")
    pdf = await build_pdf_horary(dt_str, city, country, user_questions[message.chat.id])
    await bot.send_document(message.chat.id, types.InputFile(io.BytesIO(pdf), "horary_answer.pdf"), caption="Ваш хорарный ответ")
    user_questions.pop(message.chat.id)

@dp.message(Command("natal"))
async def natal_handler(message: types.Message):
    try:
        arg = message.text.split(" ", 1)[1]
        dt, city, country = parse_date_place(arg)
    except Exception:
        await message.answer("❌ Неверный формат. Пример:\n/natal 17.08.2002, 15:20, Кострома, Россия")
        return
    from datetime import datetime
    dt_str = datetime.fromisoformat(dt).strftime("%H:%M, %d.%m.%Y")
    await message.answer("⏳ Формирую натальную карту, подождите...")
    pdf = await build_pdf_natal(dt_str, city, country)
    await bot.send_document(message.chat.id, types.InputFile(io.BytesIO(pdf), "natal_chart.pdf"))

@dp.message(Command("synastry"))
async def synastry_handler(message: types.Message):
    try:
        payload = message.text.partition("\n")[2]
        dt_a, city_a, country_a, dt_b, city_b, country_b = parse_synastry(payload)
    except Exception:
        await message.answer(
            "❌ Неверный формат команды.\n"
            "Используйте:\n"
            "/synastry\n"
            "A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
        )
        return
    await message.answer("⏳ Формирую синастрию, подождите...")
    pdf = await build_pdf_synastry(dt_a, city_a, country_a, dt_b, city_b, country_b)
    await bot.send_document(message.chat.id, types.InputFile(io.BytesIO(pdf), "synastry.pdf"))

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🔮 Хорарный вопрос (100₽)", callback_data="info_horary")],
        [InlineKeyboardButton("⭐ Натальная карта (300₽)", callback_data="info_natal")],
        [InlineKeyboardButton("💑 Синастрия (300₽)", callback_data="info_synastry")],
    ])
    await message.answer(
        "Привет! Отправьте мне ваш вопрос обычным сообщением.\n"
        "Затем используйте одну из команд с датой и временем, чтобы получить детальный анализ.\n\n"
        "Примеры вопросов хорарной астрологии:\n"
        "- Вернется ли ко мне Вася?\n"
        "- Удастся ли получить повышение?\n"
        "- Будут ли деньги с нового проекта?\n\n"
        "Или выберите услугу кнопкой ниже:",
        reply_markup=keyboard,
    )

@dp.callback_query(lambda c: c.data.startswith("info_"))
async def info_callback(callback: types.CallbackQuery):
    service = callback.data.replace("info_", "")
    info_texts = {
        "horary": (
            "🔮 <b>Хорарный вопрос (100₽)</b>\n\n"
            "Это быстрый и точный ответ на ваш конкретный вопрос в формате «Да/Нет» с пояснениями.\n\n"
            "Примеры вопросов:\n"
            "- Вернется ли ко мне Вася?\n"
            "- Удастся ли получить повышение?\n"
            "- Будут ли деньги с проекта?\n\n"
            "Действия:\n"
            "1) Отправьте ваш вопрос простым сообщением.\n"
            "2) Затем используйте команду:\n"
            "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "Пример:\n"
            "/horary 08.11.2025, 14:30, Москва, Россия"
        ),
        "natal": (
            "⭐ <b>Натальная карта (300₽)</b>\n\n"
            "Подробный разбор вашей личности на 5+ страниц.\n\n"
            "Команда:\n"
            "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "Пример:\n"
            "/natal 17.08.2002, 15:20, Кострома, Россия"
        ),
        "synastry": (
            "💑 <b>Синастрия (300₽)</b>\n\n"
            "Анализ совместимости пары на 3+ страницах.\n\n"
            "Команда:\n"
            "/synastry\n"
            "A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
            "Пример:\n"
            "/synastry\n"
            "A: 17.08.2002, 15:20, Кострома, Россия\n"
            "B: 04.07.1995, 12:00, Москва, Россия"
        )
    }
    await callback.message.answer(info_texts.get(service, "Информация отсутствует."))
    await callback.answer()

async def main():
    print("Бот успешно запущен. Работаю в режиме long polling...")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
