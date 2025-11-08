import os
import io
import asyncio
from typing import Dict, List

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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors

from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

# Регистрация шрифта DejaVuSans
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "/app/DejaVuSans.ttf"))
except Exception as e:
    print(f"Ошибка регистрации шрифта: {e}")
    raise

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = "/webhook/astrohorary"

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("TELEGRAM_TOKEN и OPENAI_API_KEY обязательны")

bot = Bot(TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
app = FastAPI()
client = httpx.AsyncClient(timeout=120)

# Здесь импортируйте свои astro_calc реализованные функции:
# from astro_calc import get_location, calculate_chart, calculate_horary, calculate_synastry


def split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def paragraphs_to_flowables(text: str) -> List[Paragraph]:
    return [Paragraph(p, styles["TextRu"]) for p in split_paragraphs(text)]


styles = getSampleStyleSheet()
styles.add(ParagraphStyle("TitleRu", fontName="DejaVuSans", fontSize=20, alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#2c3e50")))
styles.add(ParagraphStyle("SectionRu", fontName="DejaVuSans", fontSize=14, alignment=TA_LEFT, spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#34495e")))
styles.add(ParagraphStyle("TextRu", fontName="DejaVuSans", fontSize=11, leading=16, alignment=TA_JUSTIFY, spaceAfter=10))
styles.add(ParagraphStyle("IntroRu", fontName="DejaVuSans", fontSize=11, alignment=TA_CENTER, spaceAfter=15, textColor=colors.grey))


async def gpt_interpret(question: str, max_tokens=1000) -> str:
    system_msg = (
        "Ты профессиональный астролог с 15-летним опытом. "
        "Ответь четко и коротко.\n"
        "Формат: 1) краткий ответ Да/Нет или похожий вариант, 2) 2-3 пункта объяснения, 3) краткий совет.\n"
        "Не используй астрологические термины. Текст на русском."
    )
    try:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": question}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
        )
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return "⚠️ Не удалось получить ответ. Попробуйте позже."


async def gpt_followup_question(question: str) -> str:
    prompt = (
        f"Пользователь спросил: \"{question}\".\n"
        "Придумай один конкретный уточняющий вопрос, логично связанный и полезный для дальнейшего анализа.\n"
        "Ответь коротко, начинай с \"Хотите узнать:\" и сам вопрос."
    )
    try:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "Ты опытный астролог, который умеет задавать полезные уточняющие вопросы."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 60,
                "temperature": 0.7,
            },
        )
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return "Хотите узнать более подробную информацию по вашему вопросу?"


user_questions: Dict[int, str] = {}


@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def capture_question(message: types.Message):
    user_questions[message.chat.id] = message.text.strip()
    await message.answer(
        "✅ Вопрос принят!\n"
        "Теперь отправьте команду с датой, временем и местом, например:\n"
        "/horary 08.11.2025, 14:30, Москва, Россия"
    )


def parse_date_place(arg: str):
    parts = [p.strip() for p in arg.split(",")]
    if len(parts) < 4:
        raise ValueError("Неверный формат даты и места")
    dd, mm, yyyy = parts[0].split(".")
    dt = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{parts[1]}"
    city = parts[2]
    country = ",".join(parts[3:]).strip()
    return dt, city, country


async def build_pdf_horary(dt: str, city: str, country: str, question: str) -> bytes:
    from datetime import datetime

    try:
        dt_obj = datetime.fromisoformat(dt)
        dt_str = dt_obj.strftime("%H:%M, %d.%m.%Y")
    except Exception:
        dt_str = dt

    header = f"Дата и время вопроса: {dt_str}\nМесто: {city}, {country}"

    answer_text = await gpt_interpret(question)
    followup = await gpt_followup_question(question)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50,
                            leftMargin=60, rightMargin=60)
    story = [
        Paragraph("Хорарный вопрос", styles["TitleRu"]),
        Paragraph(header, styles["IntroRu"]),
        Spacer(1, 12),
        Paragraph("Ответ:", styles["SectionRu"]),
    ] + paragraphs_to_flowables(answer_text) + [
        Spacer(1, 20),
        Paragraph("Дополнительный вопрос:", styles["SectionRu"]),
        Paragraph(followup, styles["TextRu"]),
    ]
    doc.build(story)
    return buf.getvalue()


@dp.message(Command("horary"))
async def horary_command(message: types.Message):
    if message.chat.id not in user_questions:
        await message.answer(
            "❗ Сначала отправьте мне ваш вопрос обычным сообщением.\n"
            "Затем используйте команду /horary с датой, временем и местом.\n"
            "Пример:\n"
            "/horary 08.11.2025, 14:30, Москва, Россия"
        )
        return

    try:
        arg = message.text.split(" ", 1)[1]
        dt, city, country = parse_date_place(arg)
    except Exception:
        await message.answer("❌ Некорректный формат команды. Используйте:\n/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
        return

    await message.answer("⏳ Обработка вашего вопроса... Это может занять минуту.")
    pdf = await build_pdf_horary(dt, city, country, user_questions[message.chat.id])
    await bot.send_document(message.chat.id, types.BufferedInputFile(pdf, "horary_answer.pdf"), caption="Ответ на ваш вопрос")
    del user_questions[message.chat.id]


@dp.message(Command("start"))
async def start_handler(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🔮 Хорарный вопрос (100₽)", callback_data="info_horary")],
    ])
    await message.answer(
        "Привет! Отправьте ваш вопрос обычным сообщением, а затем используйте команду /horary с датой, временем и местом.\n\n"
        "Пример команды:\n/horary 08.11.2025, 14:30, Москва, Россия",
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data == "info_horary")
async def info_horary_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "🔮 Хорарный вопрос (100₽)\n\n"
        "Примеры вопросов:\n"
        "- Вернется ли ко мне Вася?\n"
        "- Удастся ли получить повышение?\n"
        "- Будут ли деньги в этом проекте?\n\n"
        "Сначала напишите свой вопрос простым сообщением, потом пришлите команду с датой, временем и местом:\n"
        "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
    )
    await callback.answer()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
