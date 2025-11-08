import os
import io
import asyncio
from typing import Dict, Any

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

# Регистрация шрифта
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
except Exception as e:
    print(f"Ошибка регистрации шрифта: {e}")
    raise

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("TELEGRAM_TOKEN и OPENAI_API_KEY необходимы")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
client = httpx.AsyncClient(timeout=120)

styles = getSampleStyleSheet()
styles.add(ParagraphStyle("TitleRu", fontName="DejaVuSans", fontSize=20, alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#2c3e50")))
styles.add(ParagraphStyle("SectionRu", fontName="DejaVuSans", fontSize=14, alignment=TA_LEFT, spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#34495e")))
styles.add(ParagraphStyle("TextRu", fontName="DejaVuSans", fontSize=11, leading=16, alignment=TA_JUSTIFY, spaceAfter=10))
styles.add(ParagraphStyle("IntroRu", fontName="DejaVuSans", fontSize=11, alignment=TA_CENTER, spaceAfter=15, textColor=colors.gray))

user_questions: Dict[int, str] = {}

# Вспомогательная функция для разделения текста на параграфы
def paragraphs_to_flowables(text: str):
    paras = [p.strip() for p in text.split('\n\n') if p.strip()]
    return [Paragraph(p, styles["TextRu"]) for p in paras]

# Простой вызов OpenAI для интерпретаций с заданным system prompt
async def openai_request(system_prompt: str, user_prompt: str, max_tokens: int = 3000) -> str:
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    try:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=body
        )
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"OpenAI request failed: {e}")
        return "⚠️ Ошибка при получении интерпретации. Попробуйте позже."

# Функция построения PDF для натальной карты
async def build_pdf_natal(datetime_str: str, city: str, country: str) -> bytes:
    system_prompt = (
        "Ты профессиональный астролог с 15-летним опытом. "
        "Опиши характеристику человека по дате и месту рождения простым языком, без сложной астрологической терминологии."
    )
    user_prompt = f"Дата рождения: {datetime_str}, Место: {city}, {country}.\nДай подробный разбор личности."
    interpretation = await openai_request(system_prompt, user_prompt, max_tokens=3000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=60, rightMargin=60, topMargin=50, bottomMargin=50)
    story = [
        Paragraph("НАТАЛЬНАЯ КАРТА", styles["TitleRu"]),
        Paragraph(f"Дата рождения и время: {datetime_str}", styles["IntroRu"]),
        Paragraph(f"Место рождения: {city}, {country}", styles["IntroRu"]),
        Spacer(1, 14),
    ]
    story.extend(paragraphs_to_flowables(interpretation))
    doc.build(story)
    return buf.getvalue()

# Функция построения PDF для хорарного вопроса
async def build_pdf_horary(datetime_str: str, city: str, country: str, question: str) -> bytes:
    system_prompt = (
        "Ты опытный астролог. Ответь четко и коротко: да/нет/скорее да или нет.\n"
        "Разъясни 2-3 пункта, затем дай краткий совет. Без терминов.\n"
        "В конце предложи 1 уточняющий вопрос по теме, который пользователь мог бы задать."
    )
    user_prompt = (
        f"Дата вопроса: {datetime_str}, Место: {city}, {country}.\n"
        f"Вопрос: {question}\n"
        "Ответь и предложи уточняющий вопрос."
    )
    response = await openai_request(system_prompt, user_prompt, max_tokens=1000)
    # Ожидаем, что модель вернёт ответ + уточняющий вопрос (можно разделять по разделителю, но для простоты выводим всё в PDF)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=60, rightMargin=60, topMargin=50, bottomMargin=50)
    story = [
        Paragraph("ХОРАРНЫЙ ВОПРОС", styles["TitleRu"]),
        Paragraph(f"Дата и время вопроса: {datetime_str}", styles["IntroRu"]),
        Paragraph(f"Место: {city}, {country}", styles["IntroRu"]),
        Spacer(1, 14),
        Paragraph("Ответ:", styles["SectionRu"]),
    ]
    story.extend(paragraphs_to_flowables(response))
    doc.build(story)
    return buf.getvalue()

# Функция построения PDF для синастрии
async def build_pdf_synastry(datetime_a: str, city_a: str, country_a: str,
                             datetime_b: str, city_b: str, country_b: str) -> bytes:
    system_prompt = (
        "Ты опытный астролог. Опиши совместимость пары, их сильные и слабые стороны, возможные сложности и советы для гармоничных отношений.\n"
        "Пиши простым, понятным языком."
    )
    user_prompt = (
        f"Человек A: дата рождения и время {datetime_a}, место {city_a}, {country_a}.\n"
        f"Человек B: дата рождения и время {datetime_b}, место {city_b}, {country_b}.\n"
        "Дай подробный разбор совместимости."
    )
    interpretation = await openai_request(system_prompt, user_prompt, max_tokens=3000)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=60, rightMargin=60, topMargin=50, bottomMargin=50)
    story = [
        Paragraph("СИНАСТРИЯ — АНАЛИЗ СОВМЕСТИМОСТИ", styles["TitleRu"]),
        Spacer(1, 14),
    ]
    story.extend(paragraphs_to_flowables(interpretation))
    doc.build(story)
    return buf.getvalue()

def parse_date_place(arg: str):
    parts = [p.strip() for p in arg.split(",")]
    if len(parts) < 4:
        raise ValueError("Неверный формат. Требуется: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    dd, mm, yyyy = parts[0].split(".")
    dt = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{parts[1]}"
    city = parts[2]
    country = ",".join(parts[3:]).strip()
    return dt, city, country

def parse_synastry(text: str):
    lines = [line.strip() for line in text.strip().splitlines()]
    a_line = next((l for l in lines if l.startswith("A:")), None)
    b_line = next((l for l in lines if l.startswith("B:")), None)
    if not a_line or not b_line:
        raise ValueError("Для синастрии нужны строки с A: и B:")
    a_data = a_line[2:].strip()
    b_data = b_line[2:].strip()
    dt_a, city_a, country_a = parse_date_place(a_data)
    dt_b, city_b, country_b = parse_date_place(b_data)
    return dt_a, city_a, country_a, dt_b, city_b, country_b

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def store_user_question(message: types.Message):
    user_questions[message.chat.id] = message.text.strip()
    await message.answer(
        "Вопрос сохранён.\n"
        "Теперь используйте одну из команд:\n"
        "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна — хорарный вопрос\n"
        "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна — натальная карта\n"
        "/synastry\nA: дата, время, город, страна\nB: дата, время, город, страна — синастрия\n\n"
        "Примеры хорарных вопросов:\n"
        "- Вернется ли ко мне Вася?\n"
        "- Будет ли повышение?\n"
        "- Удастся ли продать квартиру?\n"
    )

@dp.message(Command("horary"))
async def cmd_horary(message: types.Message):
    if message.chat.id not in user_questions:
        await message.answer("Сначала отправьте ваш вопрос сообщением.")
        return
    try:
        arg = message.text.split(" ", 1)[1]
        dt, city, country = parse_date_place(arg)
    except Exception:
        await message.answer("Неверный формат команды. Используйте:\n/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
        return
    dt_str = datetime.fromisoformat(dt).strftime("%H:%M, %d.%m.%Y")
    await message.answer("Готовлю ответ. Пожалуйста, подождите.")
    pdf = await build_pdf_horary(dt_str, city, country, user_questions[message.chat.id])
    await bot.send_document(message.chat.id, types.InputFile(io.BytesIO(pdf), "horary_answer.pdf"))
    user_questions.pop(message.chat.id)

@dp.message(Command("natal"))
async def cmd_natal(message: types.Message):
    try:
        arg = message.text.split(" ", 1)[1]
        dt, city, country = parse_date_place(arg)
    except Exception:
        await message.answer("Неверный формат команды. Используйте:\n/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
        return
    dt_str = datetime.fromisoformat(dt).strftime("%H:%M, %d.%m.%Y")
    await message.answer("Готовлю натальную карту. Пожалуйста, подождите.")
    pdf = await build_pdf_natal(dt_str, city, country)
    await bot.send_document(message.chat.id, types.InputFile(io.BytesIO(pdf), "natal_chart.pdf"))

@dp.message(Command("synastry"))
async def cmd_synastry(message: types.Message):
    try:
        payload = message.text.partition("\n")[2]
        dt_a, city_a, country_a, dt_b, city_b, country_b = parse_synastry(payload)
    except Exception:
        await message.answer(
            "Неверный формат команды.\n"
            "Используйте:\n"
            "/synastry\n"
            "A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
            "B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
        )
        return
    await message.answer("Готовлю анализ совместимости. Пожалуйста, подождите.")
    pdf = await build_pdf_synastry(dt_a, city_a, country_a, dt_b, city_b, country_b)
    await bot.send_document(message.chat.id, types.InputFile(io.BytesIO(pdf), "synastry.pdf"))

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🔮 Хорарный вопрос (100₽)", callback_data="info_horary")],
        [InlineKeyboardButton("⭐ Натальная карта (300₽)", callback_data="info_natal")],
        [InlineKeyboardButton("💑 Синастрия (300₽)", callback_data="info_synastry")]
    ])
    await message.answer(
        "Привет! Отправьте ваш вопрос обычным сообщением,\n"
        "затем используйте одну из команд:\n"
        "• /horary — для хорарного вопроса\n"
        "• /natal — для натальной карты\n"
        "• /synastry — для анализа совместимости пары\n\n"
        "Примеры хорарных вопросов:\n"
        "- Вернется ли ко мне Вася?\n"
        "- Будет ли повышение на работе?\n"
        "- Сложатся ли отношения с этим человеком?\n\n"
        "Выберите услугу ниже:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("info_"))
async def callback_info(c: types.CallbackQuery):
    service = c.data.replace("info_", "")
    texts = {
        "horary": (
            "🔮 <b>Хорарный вопрос</b>\n\n"
            "Задайте конкретный вопрос, например:\n"
            "- Вернется ли ко мне Вася?\n"
            "- Удастся ли получить повышение?\n"
            "- Будут ли деньги с проекта?\n\n"
            "После отправки вопроса используйте команду:\n"
            "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
        ),
        "natal": (
            "⭐ <b>Натальная карта</b>\n\n"
            "Детальный анализ личности.\n"
            "Команда:\n"
            "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
        ),
        "synastry": (
            "💑 <b>Синастрия</b>\n\n"
            "Анализ совместимости пары.\n"
            "Команда:\n"
            "/synastry\nA: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\nB: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
        )
    }
    await c.message.answer(texts.get(service, "Информация отсутствует."))
    await c.answer()

async def main():
    print("Бот запускается в polling режиме...")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
