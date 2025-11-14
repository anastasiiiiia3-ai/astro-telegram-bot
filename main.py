import os
import io
import asyncio
import logging
import sys
from typing import Dict
from datetime import datetime
from aiohttp import web

import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

# Импорт астрологических расчетов
from astro_calc import (
    get_location, 
    calculate_chart, 
    calculate_horary,
    calculate_synastry
)

# Импорт эзотерических расчётов  
try:
    from esoteric_calc import calculate_esoteric_points, format_esoteric_data
    ESOTERIC_AVAILABLE = True
except ImportError:
    ESOTERIC_AVAILABLE = False
    logger.warning("⚠️ Эзотерические расчёты недоступны")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Регистрация шрифта
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
    logger.info("✅ Шрифт DejaVuSans зарегистрирован")
except Exception as err:
    logger.error(f"⚠️ Ошибка регистрации шрифта: {err}")

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN не установлен!")
    sys.exit(1)
if not OPENAI_API_KEY:
    logger.error("❌ OPENAI_API_KEY не установлен!")
    sys.exit(1)

logger.info("✅ Все переменные окружения загружены")

# Инициализация бота с правильными параметрами
from aiogram.client.default import DefaultBotProperties

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
client = httpx.AsyncClient(timeout=180)

# Флаги состояния
bot_is_running = False
last_activity = datetime.now()

# Стили PDF
styles = getSampleStyleSheet()
styles.add(ParagraphStyle(
    "TitleRu", fontName="DejaVuSans", fontSize=20, 
    alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#2c3e50")
))
styles.add(ParagraphStyle(
    "SectionRu", fontName="DejaVuSans", fontSize=14, 
    alignment=TA_LEFT, spaceBefore=16, spaceAfter=10, 
    textColor=colors.HexColor("#34495e"), fontWeight='bold'
))
styles.add(ParagraphStyle(
    "TextRu", fontName="DejaVuSans", fontSize=11, 
    leading=16, alignment=TA_JUSTIFY, spaceAfter=10
))
styles.add(ParagraphStyle(
    "IntroRu", fontName="DejaVuSans", fontSize=11, 
    alignment=TA_CENTER, spaceAfter=15, textColor=colors.gray
))

# FSM States
class UserStates(StatesGroup):
    waiting_horary_question = State()
    waiting_natal_data = State()
    waiting_synastry_data = State()

# Хранилище данных
user_data: Dict[int, dict] = {}

# Цены услуг
PRICES = {
    "horary": {"amount": 10000, "title": "Хорарный вопрос", "description": "Быстрый ответ Да/Нет"},
    "natal": {"amount": 30000, "title": "Натальная карта", "description": "Полный разбор личности"},
    "synastry": {"amount": 30000, "title": "Синастрия", "description": "Анализ совместимости"},
    "esoteric": {"amount": 30000, "title": "Эзотерическая карта", "description": "Кармическое предназначение"}
}

async def openai_request(system_prompt: str, user_prompt: str, max_tokens: int = 3000) -> str:
    """Запрос к OpenAI с обработкой ошибок"""
    try:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.4,
        }
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return "⚠️ Временная ошибка сервиса. Попробуйте через минуту."

async def build_pdf_natal(chart_data: dict, interpretation: str) -> bytes:
    """Создание PDF натальной карты"""
    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=50, rightMargin=50, 
                               topMargin=40, bottomMargin=40)
        
        story = [
            Paragraph("НАТАЛЬНАЯ КАРТА", styles["TitleRu"]),
            Paragraph(f"Дата: {chart_data['datetime_local']}", styles["IntroRu"]),
            Spacer(1, 20),
        ]
        
        for para in interpretation.split("\n\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), styles["TextRu"]))
        
        doc.build(story)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        raise

async def build_pdf_horary(chart_data: dict, question: str, answer: str) -> bytes:
    """PDF хорарного вопроса"""
    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=50, rightMargin=50)
        
        story = [
            Paragraph("ХОРАРНЫЙ ВОПРОС", styles["TitleRu"]),
            Paragraph(f"Дата: {chart_data['datetime_local']}", styles["IntroRu"]),
            Spacer(1, 20),
            Paragraph(f"<b>Вопрос:</b> {question}", styles["TextRu"]),
            Spacer(1, 10),
            Paragraph("<b>Ответ:</b>", styles["SectionRu"]),
        ]
        
        for para in answer.split("\n\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), styles["TextRu"]))
        
        doc.build(story)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        raise

async def build_pdf_synastry(chart_a: dict, chart_b: dict, analysis: str) -> bytes:
    """PDF синастрии"""
    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=50, rightMargin=50)
        
        story = [
            Paragraph("СИНАСТРИЯ — АНАЛИЗ СОВМЕСТИМОСТИ", styles["TitleRu"]),
            Spacer(1, 20),
        ]
        
        for para in analysis.split("\n\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), styles["TextRu"]))
        
        doc.build(story)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        raise

def parse_date_place(text: str):
    """Парсинг даты и места"""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 4:
        raise ValueError("Неверный формат")
    
    date_part, time_part = parts[0], parts[1]
    dd, mm, yyyy = date_part.split(".")
    dt_iso = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{time_part}"
    city = parts[2]
    country = ",".join(parts[3:]).strip()
    return dt_iso, city, country

# ===== ОБРАБОТЧИКИ С ЗАЩИТОЙ ОТ ОШИБОК =====

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    try:
        global last_activity
        last_activity = datetime.now()
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔮 Хорарный вопрос (100₽)", callback_data="service_horary")],
            [InlineKeyboardButton(text="⭐ Натальная карта (300₽)", callback_data="service_natal")],
            [InlineKeyboardButton(text="🌟 Эзотерическая карта (300₽)", callback_data="service_esoteric")],
            [InlineKeyboardButton(text="💑 Синастрия (300₽)", callback_data="service_synastry")],
        ])
        await message.answer(
            "👋 <b>Добро пожаловать в астробот!</b>\n\n"
            "Я сочетаю искусственный интеллект и профессиональные астрологические расчёты Swiss Ephemeris, "
            "что делает мои анализы максимально точными и понятными.\n\n"
            "Я помогу вам:\n"
            "• Получить точный ответ на ваш вопрос (хорар)\n"
            "• Узнать свою натальную карту\n"
            "• Раскрыть кармическое предназначение (эзотерика)\n"
            "• Проверить совместимость (синастрия)\n\n"
            "Выберите услугу:",
            reply_markup=keyboard
        )
        logger.info(f"User {message.from_user.id} started bot")
    except Exception as e:
        logger.error(f"Error in start_handler: {e}")

@dp.callback_query(F.data.startswith("service_"))
async def service_selection(callback: types.CallbackQuery, state: FSMContext):
    try:
        global last_activity
        last_activity = datetime.now()
        
        service = callback.data.split("_")[1]
        user_data[callback.from_user.id] = {"service": service}
        
        if service == "horary":
            await state.set_state(UserStates.waiting_horary_question)
            await callback.message.answer(
                "🔮 <b>Хорарная астрология</b>\n\n"
                "Задайте ваш вопрос в формате:\n"
                "• Вернется ли ко мне Вася?\n"
                "• Получу ли я повышение?\n"
                "• Стоит ли покупать эту квартиру?\n\n"
                "Отправьте ваш вопрос:"
            )
        elif service == "natal":
            await state.set_state(UserStates.waiting_natal_data)
            await callback.message.answer(
                "⭐ <b>Натальная карта</b>\n\n"
                "Отправьте данные в формате:\n"
                "<code>ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна</code>\n\n"
                "Пример:\n"
                "<code>17.08.2002, 15:20, Кострома, Россия</code>"
            )
        elif service == "esoteric":
            await state.set_state(UserStates.waiting_natal_data)
            await callback.message.answer(
                "🌟 <b>Эзотерическая карта</b>\n\n"
                "Глубинный кармический анализ вашей души!\n\n"
                "Вы узнаете:\n"
                "⚡ Электрический и магнитный асцендент\n"
                "🤍 Белую Луну — ангельскую защиту\n"
                "⭐ Фиксированные звёзды\n"
                "💎 Парс Фортуны и Духа\n"
                "🌳 Родовую карму\n\n"
                "Отправьте данные рождения:\n"
                "<code>ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна</code>\n\n"
                "Пример:\n"
                "<code>17.08.2002, 15:20, Кострома, Россия</code>"
            )
        elif service == "synastry":
            await state.set_state(UserStates.waiting_synastry_data)
            await callback.message.answer(
                "💑 <b>Синастрия</b>\n\n"
                "Отправьте данные двух человек:\n"
                "<code>A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
                "B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна</code>\n\n"
                "Пример:\n"
                "<code>A: 17.08.2002, 15:20, Кострома, Россия\n"
                "B: 04.07.1995, 12:00, Москва, Россия</code>"
            )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in service_selection: {e}")
        await callback.answer("⚠️ Произошла ошибка, попробуйте снова")

@dp.message(UserStates.waiting_horary_question)
async def horary_question_handler(message: types.Message, state: FSMContext):
    try:
        user_data[message.from_user.id]["question"] = message.text.strip()
        await state.clear()
        await message.answer(
            "Отлично! Теперь отправьте дату и время вопроса:\n"
            "<code>ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна</code>\n\n"
            "Пример:\n<code>10.11.2025, 14:30, Москва, Россия</code>"
        )
        await state.set_state(UserStates.waiting_natal_data)
    except Exception as e:
        logger.error(f"Error in horary_question_handler: {e}")
        await message.answer("⚠️ Ошибка обработки. Попробуйте /start")

@dp.message(UserStates.waiting_natal_data)
async def natal_data_handler(message: types.Message, state: FSMContext):
    try:
        uid = message.from_user.id
        dt_iso, city, country = parse_date_place(message.text)
        user_data[uid]["datetime"] = dt_iso
        user_data[uid]["city"] = city
        user_data[uid]["country"] = country
        
        service_type = user_data[uid]["service"]
        price_info = PRICES.get(service_type, PRICES["horary"])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"Оплатить {price_info['amount']//100}₽",
                callback_data=f"pay_{service_type}"
            )
        ]])
        
        await message.answer(
            f"✅ Данные приняты!\n\n"
            f"<b>{price_info['title']}</b>\n"
            f"{price_info['description']}\n\n"
            f"Стоимость: {price_info['amount']//100}₽",
            reply_markup=keyboard
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error in natal_data_handler: {e}")
        await message.answer(f"❌ Ошибка: {e}\nПроверьте формат данных.")

@dp.message(UserStates.waiting_synastry_data)
async def synastry_data_handler(message: types.Message, state: FSMContext):
    try:
        uid = message.from_user.id
        lines = [l.strip() for l in message.text.strip().splitlines() if l.strip()]
        a_line = next((l for l in lines if l.upper().startswith("A:")), None)
        b_line = next((l for l in lines if l.upper().startswith("B:")), None)
        
        if not a_line or not b_line:
            raise ValueError("Нужны строки с 'A:' и 'B:'")
        
        dt_a, city_a, country_a = parse_date_place(a_line[2:].strip())
        dt_b, city_b, country_b = parse_date_place(b_line[2:].strip())
        
        user_data[uid].update({
            "dt_a": dt_a, "city_a": city_a, "country_a": country_a,
            "dt_b": dt_b, "city_b": city_b, "country_b": country_b
        })
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Оплатить 300₽", callback_data="pay_synastry")
        ]])
        
        await message.answer(
            "✅ Данные обоих партнеров приняты!\n\n"
            "<b>Синастрия</b>\nСтоимость: 300₽",
            reply_markup=keyboard
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error in synastry_data_handler: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(F.data.startswith("pay_"))
async def payment_handler(callback: types.CallbackQuery):
    try:
        service = callback.data.split("_")[1]
        
        if not PAYMENT_TOKEN:
            await callback.answer("⚠️ Обработка без оплаты...")
            await process_service(callback.from_user.id, callback.message)
            return
        
        price_info = PRICES[service]
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=price_info["title"],
            description=price_info["description"],
            payload=f"{service}_{callback.from_user.id}",
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label=price_info["title"], amount=price_info["amount"])],
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in payment_handler: {e}")
        await callback.answer("⚠️ Ошибка оплаты")

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    await message.answer("✅ Оплата прошла успешно! Готовлю ваш анализ...")
    await process_service(message.from_user.id, message)

async def process_service(user_id: int, message: types.Message):
    try:
        data = user_data.get(user_id, {})
        service = data.get("service")
        
        if service == "horary":
            await process_horary(user_id, message)
        elif service == "natal":
            await process_natal(user_id, message)
        elif service == "esoteric":
            await process_esoteric(user_id, message)
        elif service == "synastry":
            await process_synastry(user_id, message)
    except Exception as e:
        logger.error(f"Error in process_service: {e}")
        await message.answer(f"❌ Ошибка обработки: {e}")

async def process_horary(user_id: int, message: types.Message):
    try:
        data = user_data[user_id]
        lat, lon, tz = await get_location(data["city"], data["country"])
        chart = calculate_horary(data["datetime"], lat, lon, tz)
        
        planets_list = "\n".join([
            f"- {p['name']} в {p['sign']} ({round(p['lon'] % 30, 1)}°)"
            for p in chart['planets']
        ])
        
        system_prompt = (
            "Ты опытный хорарный астролог. Проанализируй карту и дай СТРУКТУРИРОВАННЫЙ ответ:\n\n"
            "1. КРАТКИЙ ОТВЕТ\n"
            "Напиши одним предложением: «Да», «Нет», «Скорее да» или «Скорее нет».\n\n"
            "2. ОБОСНОВАНИЕ\n"
            "Дай 3-4 пункта объяснения. Каждый пункт с новой строки.\n\n"
            "3. СОВЕТ\n"
            "Конкретная рекомендация.\n\n"
            "4. УТОЧНЯЮЩИЙ ВОПРОС\n"
            "Закончи вопросом: 'Хотите узнать: ...?'\n\n"
            "Используй простой язык БЕЗ терминов и символов ###, **"
        )
        
        user_prompt = (
            f"Хорарная карта:\n"
            f"Дата: {chart['datetime_local']}\n"
            f"Асцендент: {chart['asc']}\n\n"
            f"Планеты:\n{planets_list}\n\n"
            f"ВОПРОС: {data['question']}"
        )
        
        answer = await openai_request(system_prompt, user_prompt, max_tokens=1500)
        pdf = await build_pdf_horary(chart, data["question"], answer)
        
        await bot.send_document(
            user_id,
            types.BufferedInputFile(pdf, "horary.pdf"),
            caption="🔮 Ваш хорарный ответ готов!"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Задать еще вопрос 🔮", callback_data="service_horary")
        ]])
        await message.answer("Хотите задать еще один вопрос?", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error in process_horary: {e}")
        await message.answer("❌ Ошибка создания анализа. Попробуйте снова.")

async def process_natal(user_id: int, message: types.Message):
    try:
        data = user_data[user_id]
        lat, lon, tz = await get_location(data["city"], data["country"])
        chart = calculate_chart(data["datetime"], lat, lon, tz)
        
        planets_list = "\n".join([
            f"- {p['name']} в {p['sign']} ({round(p['lon'] % 30, 1)}°){'- Ретроградна' if p['retro'] else ''}"
            for p in chart['planets']
        ])
        
        system_prompt = (
            "Ты профессиональный астролог с 20-летним опытом. "
            "Создай МАКСИМАЛЬНО ПОДРОБНУЮ натальную карту на 5-6 страниц.\n\n"
            "СТРУКТУРА:\n\n"
            "=== 1. ОБЩАЯ ХАРАКТЕРИСТИКА ЛИЧНОСТИ ===\n"
            "Основные черты характера, темперамент, энергетика (1 страница)\n\n"
            "=== 2. ДОМА И ЗНАКИ ===\n"
            "Пройдись по КАЖДОМУ дому (1-12) и объясни значение знака (1.5 страницы)\n\n"
            "=== 3. ТАЛАНТЫ И СПОСОБНОСТИ ===\n"
            "Врождённые дары (0.5 страницы)\n\n"
            "=== 4. ДЕНЬГИ, КАРЬЕРА И ПРИЗВАНИЕ ===\n"
            "Профессии, атмосфера работы, отношение к деньгам, способы заработка (1 страница)\n\n"
            "=== 5. ЛЮБОВЬ И ОТНОШЕНИЯ ===\n"
            "Партнёр, проявление любви, Венера, Марс, сексуальность (1 страница)\n\n"
            "=== 6. АСЦЕНДЕНТ ===\n"
            "Первое впечатление (0.5 страницы)\n\n"
            "=== 7. ВНЕШНОСТЬ И КРАСОТА ===\n"
            "Венера + Асцендент, стиль (0.5 страницы)\n\n"
            "Пиши простым языком БЕЗ терминов и символов ###, **\n"
            "Заголовки ЗАГЛАВНЫМИ БУКВАМИ"
        )
        
        user_prompt = (
            f"Натальная карта:\n"
            f"Дата: {chart['datetime_local']}\n"
            f"Асцендент: {chart['asc']}\n"
            f"MC: {chart['mc']}\n\n"
            f"Планеты:\n{planets_list}"
        )
        
        interpretation = await openai_request(system_prompt, user_prompt, max_tokens=6000)
        pdf = await build_pdf_natal(chart, interpretation)
        
        await bot.send_document(
            user_id,
            types.BufferedInputFile(pdf, "natal_chart.pdf"),
            caption="⭐ Ваша натальная карта готова!"
        )
    except Exception as e:
        logger.error(f"Error in process_natal: {e}")
        await message.answer("❌ Ошибка создания анализа. Попробуйте снова.")

async def process_synastry(user_id: int, message: types.Message):
    try:
        data = user_data[user_id]
        lat_a, lon_a, tz_a = await get_location(data["city_a"], data["country_a"])
        lat_b, lon_b, tz_b = await get_location(data["city_b"], data["country_b"])
        
        synastry = calculate_synastry(
            data["dt_a"], lat_a, lon_a, tz_a,
            data["dt_b"], lat_b, lon_b, tz_b
        )
        
        planets_a = "\n".join([
            f"- {p['name']} в {p['sign']} ({round(p['lon'] % 30, 1)}°)"
            for p in synastry["chart_a"]['planets']
        ])
        
        planets_b = "\n".join([
            f"- {p['name']} в {p['sign']} ({round(p['lon'] % 30, 1)}°)"
            for p in synastry["chart_b"]['planets']
        ])
        
        system_prompt = (
            "Ты профессиональный астролог по синастрии. Проанализируй совместимость на 3-4 страницы.\n\n"
            "СТРУКТУРА:\n\n"
            "СИЛЬНЫЕ СТОРОНЫ ОТНОШЕНИЙ\n"
            "Что объединяет, гармония, совместимость, сексуальность (1 страница)\n\n"
            "ВОЗМОЖНЫЕ ТРУДНОСТИ И КОНФЛИКТЫ\n"
            "Непонимание, различия, конфликты (1 страница)\n\n"
            "СОВЕТЫ ДЛЯ ГАРМОНИИ\n"
            "Практические рекомендации (1 страница)\n\n"
            "Пиши простым языком БЕЗ символов ###, **\n"
            "Заголовки ЗАГЛАВНЫМИ БУКВАМИ"
        )
        
        user_prompt = (
            f"ЧЕЛОВЕК A:\n"
            f"Дата: {synastry['chart_a']['datetime_local']}\n"
            f"Планеты:\n{planets_a}\n\n"
            f"ЧЕЛОВЕК B:\n"
            f"Дата: {synastry['chart_b']['datetime_local']}\n"
            f"Планеты:\n{planets_b}"
        )
        
        analysis = await openai_request(system_prompt, user_prompt, max_tokens=5000)
        pdf = await build_pdf_synastry(synastry["chart_a"], synastry["chart_b"], analysis)
        
        await bot.send_document(
            user_id,
            types.BufferedInputFile(pdf, "synastry.pdf"),
            caption="💑 Анализ совместимости готов!"
        )
    except Exception as e:
        logger.error(f"Error in process_synastry: {e}")
        await message.answer("❌ Ошибка создания анализа. Попробуйте снова.")

async def process_esoteric(user_id: int, message: types.Message):
    try:
        data = user_data[user_id]
        lat, lon, tz = await get_location(data["city"], data["country"])
        
        # Импортируем функцию парсинга из astro_calc
        from astro_calc import parse_datetime
        jd = parse_datetime(data["datetime"], tz)
        
        # Получаем базовую карту
        from astro_calc import swe
        houses, ascmc = swe.houses(jd, lat, lon, b'P')
        asc = ascmc[0]
        mc = ascmc[1]
        
        # Получаем Солнце и Луну
        sun_pos = swe.calc_ut(jd, swe.SUN)[0][0]
        moon_pos = swe.calc_ut(jd, swe.MOON)[0][0]
        
        # Рассчитываем эзотерические точки
        esoteric = calculate_esoteric_points(jd, lat, lon, asc, mc, sun_pos, moon_pos)
        esoteric_text = format_esoteric_data(esoteric)
        
        system_prompt = (
            "Ты эзотерический астролог с глубоким знанием кармы и духовных практик. "
            "Создай МИСТИЧЕСКИЙ и ГЛУБОКИЙ анализ на 6-8 страниц.\n\n"
            "ОБЯЗАТЕЛЬНЫЕ РАЗДЕЛЫ:\n\n"
            "=== ЭЛЕКТРИЧЕСКИЙ АСЦЕНДЕНТ — ИСТИННАЯ СУЩНОСТЬ ===\n"
            "Объясни что такое электрический асцендент (духовная суть, высшее Я). "
            "Опиши как проявляется знак электрического асцендента. "
            "Какова истинная духовная природа человека. (1 страница)\n\n"
            
            "=== МАГНИТНЫЙ АСЦЕНДЕНТ — ЧТО ПРИТЯГИВАЕТ ===\n"
            "Объясни что такое магнитный асцендент (что притягивает в жизнь). "
            "Какие события, люди, ситуации магнетически притягиваются. "
            "Как использовать эту энергию. (1 страница)\n\n"
            
            "=== БЕЛАЯ ЛУНА (СЕЛЕНА) — АНГЕЛ-ХРАНИТЕЛЬ ===\n"
            "Опиши Белую Луну как источник божественной защиты. "
            "В каком знаке находится ангел-хранитель и как он проявляется. "
            "Какие дары и защиту даёт. Как обращаться за помощью. (1 страница)\n\n"
            
            "=== ФИКСИРОВАННЫЕ ЗВЁЗДЫ — КАРМИЧЕСКАЯ ИЗЮМИНКА ===\n"
            "Объясни влияние каждой звезды которая есть в карте. "
            "Какие кармические дары или испытания приносит. "
            "Связь с прошлыми воплощениями. (1 страница)\n\n"
            
            "=== ПАРС ФОРТУНЫ И ДУХА — ПУТЬ К СЧАСТЬЮ ===\n"
            "Парс Фортуны - где найти материальное счастье и удачу. "
            "Парс Духа - как реализовать духовное призвание. "
            "Практические советы по обеим точкам. (1 страница)\n\n"
            
            "=== РОДОВАЯ КАРМА — НАСЛЕДИЕ ПРЕДКОВ ===\n"
            "Анализ Северного и Южного Узлов. "
            "Что передалось от предков (таланты, программы, долги). "
            "Какие родовые программы нужно отработать. "
            "Как освободиться и какое наследие принять. (1.5 страницы)\n\n"
            
            "=== ДУХОВНОЕ ПРЕДНАЗНАЧЕНИЕ И ПУТЬ ===\n"
            "Синтез всех эзотерических точек. "
            "Главная кармическая задача в этой жизни. "
            "Духовные практики которые подходят. "
            "Конкретные шаги для реализации предназначения. (1.5 страницы)\n\n"
            
            "ВАЖНО:\n"
            "- Пиши мистическим, вдохновляющим языком\n"
            "- Используй слова: душа, карма, предназначение, энергия\n"
            "- Давай КОНКРЕТНЫЕ практические советы\n"
            "- БЕЗ символов ###, **\n"
            "- Заголовки ЗАГЛАВНЫМИ БУКВАМИ"
        )
        
        user_prompt = (
            f"Дата рождения: {data['datetime']}\n"
            f"Место: {data['city']}, {data['country']}\n\n"
            f"{esoteric_text}\n\n"
            f"Создай ГЛУБОКИЙ эзотерический анализ!"
        )
        
        interpretation = await openai_request(system_prompt, user_prompt, max_tokens=7000)
        pdf = await build_pdf_natal({"datetime_local": data["datetime"]}, interpretation)
        
        await bot.send_document(
            user_id,
            types.BufferedInputFile(pdf, "esoteric_chart.pdf"),
            caption="🌟 Ваша эзотерическая карта готова!"
        )
    except Exception as e:
        logger.error(f"Error in process_esoteric: {e}")
        await message.answer("❌ Ошибка создания анализа. Попробуйте снова.")

# ===== ВЕБ-СЕРВЕР =====

async def health_check(request):
    global bot_is_running, last_activity
    time_since = (datetime.now() - last_activity).total_seconds()
    
    if bot_is_running and time_since < 300:
        return web.Response(text=f"OK - {int(time_since)}s ago", status=200)
    else:
        return web.Response(text=f"DOWN - {int(time_since)}s ago", status=503)

async def start_web_server():
    global bot_is_running
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    port = int(os.getenv('PORT', 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    bot_is_running = True
    logger.info(f"🌐 Web server started on port {port}")

# ===== ГЛАВНАЯ ФУНКЦИЯ С АВТОПЕРЕЗАПУСКОМ =====

async def main():
    retry_count = 0
    max_retries = 10
    
    while retry_count < max_retries:
        try:
            logger.info(f"🔄 Попытка запуска {retry_count + 1}/{max_retries}")
            
            # Удаляем webhook
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                logger.info("✅ Webhook удален")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка удаления webhook: {e}")
            
            # Проверяем подключение
            try:
                me = await bot.get_me()
                logger.info(f"✅ Бот подключен: @{me.username} (ID: {me.id})")
            except Exception as e:
                logger.error(f"❌ Не удалось подключиться к боту: {e}")
                retry_count += 1
                await asyncio.sleep(5)
                continue
            
            logger.info("🚀 Запускаю веб-сервер и polling...")
            
            # Запускаем с обработкой ошибок
            await asyncio.gather(
                start_web_server(),
                dp.start_polling(
                    bot, 
                    skip_updates=True,
                    allowed_updates=dp.resolve_used_update_types(),
                    handle_as_tasks=True
                )
            )
            
            logger.info("✅ Polling запущен успешно")
            break  # Если всё прошло успешно
            
        except asyncio.CancelledError:
            logger.warning("⚠️ Получен сигнал остановки")
            break
        except Exception as e:
            retry_count += 1
            logger.error(f"❌ Критическая ошибка (попытка {retry_count}/{max_retries}): {e}")
            
            if retry_count < max_retries:
                wait_time = min(retry_count * 5, 30)  # Экспоненциальная задержка
                logger.info(f"⏳ Перезапуск через {wait_time} секунд...")
                await asyncio.sleep(wait_time)
            else:
                logger.critical("💀 Превышено количество попыток. Бот остановлен.")
                raise
    
    # Держим бота запущенным
    try:
        while True:
            await asyncio.sleep(3600)  # Проверяем каждый час
    except asyncio.CancelledError:
        logger.info("👋 Бот остановлен")

if __name__ == "__main__":
    try:
        logger.info("=" * 50)
        logger.info("🌟 ЗАПУСК АСТРОЛОГИЧЕСКОГО БОТА")
        logger.info("=" * 50)
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"💥 Фатальная ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
