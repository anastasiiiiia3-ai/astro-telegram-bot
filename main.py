import os
import io
import asyncio
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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
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

# Регистрация шрифта
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
except Exception as err:
    print(f"⚠️ Шрифт не найден: {err}")

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")  # Токен оплаты (ЮKassa, Stripe и т.д.)

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("❌ Установите TELEGRAM_TOKEN и OPENAI_API_KEY!")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
client = httpx.AsyncClient(timeout=180)

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

# Хранилище вопросов и данных
user_data: Dict[int, dict] = {}

# Цены услуг (в рублях, умножить на 100 для копеек)
PRICES = {
    "horary": {"amount": 10000, "title": "Хорарный вопрос", "description": "Быстрый ответ Да/Нет"},
    "natal": {"amount": 30000, "title": "Натальная карта", "description": "Полный разбор личности"},
    "synastry": {"amount": 30000, "title": "Синастрия", "description": "Анализ совместимости"}
}

async def openai_request(system_prompt: str, user_prompt: str, max_tokens: int = 3000) -> str:
    """Запрос к OpenAI API"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
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
    except Exception as e:
        print(f"❌ OpenAI error: {e}")
        return "⚠️ Не удалось получить ответ от AI. Попробуйте позже."

def format_chart_data(chart: dict) -> str:
    """Форматирование астрологических данных для GPT"""
    planets_text = "\n".join([
        f"{p['name']}: {p['sign']} {round(p['lon'] % 30, 1)}° {'(R)' if p['retro'] else ''}"
        for p in chart['planets']
    ])
    return f"""
Дата: {chart['datetime_local']}
Широта: {chart['lat']:.2f}, Долгота: {chart['lon']:.2f}
Асцендент: {chart['asc']}
MC (Середина неба): {chart['mc']}

Планеты:
{planets_text}
"""

async def build_pdf_natal(chart_data: dict, interpretation: str) -> bytes:
    """Создание PDF натальной карты"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=50, rightMargin=50, 
                           topMargin=40, bottomMargin=40)
    
    story = [
        Paragraph("НАТАЛЬНАЯ КАРТА", styles["TitleRu"]),
        Paragraph(f"Дата: {chart_data['datetime_local']}", styles["IntroRu"]),
        Spacer(1, 20),
    ]
    
    # Интерпретация без таблицы
    for para in interpretation.split("\n\n"):
        if para.strip():
            story.append(Paragraph(para.strip(), styles["TextRu"]))
    
    doc.build(story)
    return buf.getvalue()

async def build_pdf_horary(chart_data: dict, question: str, answer: str) -> bytes:
    """PDF хорарного вопроса"""
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

async def build_pdf_synastry(chart_a: dict, chart_b: dict, analysis: str) -> bytes:
    """PDF синастрии"""
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

def parse_date_place(text: str):
    """Парсинг даты и места: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 4:
        raise ValueError("Неверный формат")
    
    date_part, time_part = parts[0], parts[1]
    dd, mm, yyyy = date_part.split(".")
    dt_iso = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{time_part}"
    city = parts[2]
    country = ",".join(parts[3:]).strip()
    return dt_iso, city, country

# ===== КОМАНДЫ =====

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Хорарный вопрос (100₽)", callback_data="service_horary")],
        [InlineKeyboardButton(text="⭐ Натальная карта (300₽)", callback_data="service_natal")],
        [InlineKeyboardButton(text="💑 Синастрия (300₽)", callback_data="service_synastry")],
    ])
    await message.answer(
        "👋 <b>Добро пожаловать в астробот!</b>\n\n"
        "Я сочетаю искусственный интеллект и профессиональные астрологические расчёты Swiss Ephemeris, "
        "что делает мои анализы максимально точными и понятными.\n\n"
        "Я помогу вам:\n"
        "• Получить точный ответ на ваш вопрос (хорар)\n"
        "• Узнать свою натальную карту\n"
        "• Проверить совместимость (синастрия)\n\n"
        "Выберите услугу:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("service_"))
async def service_selection(callback: types.CallbackQuery, state: FSMContext):
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

# ===== ОБРАБОТКА ДАННЫХ =====

@dp.message(UserStates.waiting_horary_question)
async def horary_question_handler(message: types.Message, state: FSMContext):
    user_data[message.from_user.id]["question"] = message.text.strip()
    await state.clear()
    await message.answer(
        "Отлично! Теперь отправьте дату и время вопроса:\n"
        "<code>ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна</code>\n\n"
        "Пример:\n<code>10.11.2025, 14:30, Москва, Россия</code>"
    )
    await state.set_state(UserStates.waiting_natal_data)

@dp.message(UserStates.waiting_natal_data)
async def natal_data_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    try:
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
        await message.answer(f"❌ Ошибка: {e}\nПроверьте формат данных.")

@dp.message(UserStates.waiting_synastry_data)
async def synastry_data_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    try:
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
        await message.answer(f"❌ Ошибка: {e}")

# ===== ПЛАТЕЖИ =====

@dp.callback_query(F.data.startswith("pay_"))
async def payment_handler(callback: types.CallbackQuery):
    service = callback.data.split("_")[1]
    price_info = PRICES[service]
    
    if not PAYMENT_TOKEN:
        await callback.answer("⚠️ Оплата отключена, обработка бесплатно...")
        await process_service(callback.from_user.id, callback.message)
        return
    
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

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    await message.answer("✅ Оплата прошла успешно! Готовлю ваш анализ...")
    await process_service(message.from_user.id, message)

# ===== ОБРАБОТКА УСЛУГ =====

async def process_service(user_id: int, message: types.Message):
    data = user_data.get(user_id, {})
    service = data.get("service")
    
    try:
        if service == "horary":
            await process_horary(user_id, message)
        elif service == "natal":
            await process_natal(user_id, message)
        elif service == "synastry":
            await process_synastry(user_id, message)
    except Exception as e:
        await message.answer(f"❌ Ошибка обработки: {e}")

async def process_horary(user_id: int, message: types.Message):
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
        "Дай 3-4 пункта объяснения почему именно такой ответ. "
        "Каждый пункт с новой строки, простым языком.\n\n"
        "3. СОВЕТ\n"
        "Конкретная рекомендация что делать в этой ситуации.\n\n"
        "4. УТОЧНЯЮЩИЙ ВОПРОС\n"
        "Закончи одним вопросом начиная со слов: 'Хотите узнать: ...?'\n\n"
        "ВАЖНО:\n"
        "- Используй простой понятный язык БЕЗ астрологических терминов\n"
        "- НЕ используй символы ###, **, звёздочки - только чистый текст\n"
        "- Пиши конкретно и по делу"
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

async def process_natal(user_id: int, message: types.Message):
    data = user_data[user_id]
    
    lat, lon, tz = await get_location(data["city"], data["country"])
    chart = calculate_chart(data["datetime"], lat, lon, tz)
    
    # Формируем детальное описание планет для GPT
    planets_list = "\n".join([
        f"- {p['name']} в {p['sign']} ({round(p['lon'] % 30, 1)}°){'- Ретроградна' if p['retro'] else ''}"
        for p in chart['planets']
    ])
    
    system_prompt = (
        "Ты профессиональный астролог с 20-летним опытом. "
        "Твоя задача - создать МАКСИМАЛЬНО ПОДРОБНУЮ натальную карту на 5-6 страниц текста.\n\n"
        "СТРОГАЯ СТРУКТУРА (каждый раздел начинай с заголовка):\n\n"
        "=== 1. ОБЩАЯ ХАРАКТЕРИСТИКА ЛИЧНОСТИ ===\n"
        "Опиши основные черты характера, темперамент, энергетику человека, жизненные приоритеты. "
        "Как человек воспринимает мир и себя в нём. (минимум 1 страница)\n\n"
        
        "=== 2. ДОМА И ЗНАКИ В НИХ ===\n"
        "Пройдись ПО КАЖДОМУ дому (с 1-го по 12-й) и объясни значение знака в этом доме:\n"
        "• 1-й дом (Асцендент, личность, внешность) - какой знак и что это значит\n"
        "• 2-й дом (деньги, ценности, ресурсы)\n"
        "• 3-й дом (общение, обучение, ближайшее окружение)\n"
        "• 4-й дом (семья, дом, корни, эмоциональная база)\n"
        "• 5-й дом (творчество, любовь, дети, хобби)\n"
        "• 6-й дом (работа, здоровье, повседневные дела)\n"
        "• 7-й дом (партнёрство, брак, серьёзные отношения)\n"
        "• 8-й дом (трансформации, кризисы, глубинная психология)\n"
        "• 9-й дом (философия, путешествия, высшее образование)\n"
        "• 10-й дом (карьера, призвание, социальный статус)\n"
        "• 11-й дом (дружба, сообщества, мечты и цели)\n"
        "• 12-й дом (подсознание, духовность, уединение)\n"
        "Для каждого дома напиши 2-3 предложения! (минимум 1.5 страницы)\n\n"
        
        "=== 3. ТАЛАНТЫ И ПРИРОДНЫЕ СПОСОБНОСТИ ===\n"
        "Какие врождённые дары есть у человека. В чём он может легко преуспеть. "
        "Какие сферы даются естественно. (минимум полстраницы)\n\n"
        
        "=== 4. ДЕНЬГИ, КАРЬЕРА И ПРИЗВАНИЕ ===\n"
        "Очень подробно раскрой:\n"
        "• Какие профессии и сферы деятельности подходят (примеры конкретных профессий)\n"
        "• В какой атмосфере комфортно работать: в команде или в одиночку, в офисе или удалённо, "
        "со структурой или свободным графиком\n"
        "• Как человек относится к деньгам (транжира/накопитель/инвестор)\n"
        "• Лучшие способы заработка и монетизации талантов\n"
        "• Что мотивирует в работе\n"
        "(минимум 1 страница)\n\n"
        
        "=== 5. ЛЮБОВЬ, ОТНОШЕНИЯ И ПАРТНЁРСТВО ===\n"
        "Максимально подробно:\n"
        "• Какой партнёр идеально подходит (характер, темперамент, ценности)\n"
        "• Как человек проявляет свою любовь и привязанность (слова, поступки, подарки)\n"
        "• Что важно в отношениях (свобода/стабильность, страсть/дружба)\n"
        "• Венера в знаке: стиль любви, романтика, что привлекает\n"
        "• Марс в знаке: сексуальность, страсть, как действует в отношениях\n"
        "• Потенциальные сложности в отношениях и как их избежать\n"
        "(минимум 1 страница)\n\n"
        
        "=== 6. АСЦЕНДЕНТ — ПЕРВОЕ ВПЕЧАТЛЕНИЕ ===\n"
        "Как человека воспринимают при первой встрече. Какую энергию он излучает. "
        "Что видят в нём незнакомцы. Маска личности. (минимум полстраницы)\n\n"
        
        "=== 7. ВНЕШНОСТЬ, КРАСОТА И СТИЛЬ ===\n"
        "На основе Венеры и Асцендента опиши:\n"
        "• Внешние данные: черты лица, фигура, общий образ\n"
        "• Природная притягательность и обаяние\n"
        "• Стиль одежды который идёт (классика/бохо/спорт/романтика)\n"
        "• Как подчеркнуть свою красоту\n"
        "(минимум полстраницы)\n\n"
        
        "ВАЖНО:\n"
        "- Пиши простым, понятным языком БЕЗ астрологических терминов\n"
        "- НЕ используй символы ###, **, звёздочки - только чистый текст\n"
        "- Каждый раздел начинай с ЗАГОЛОВКА В ВЕРХНЕМ РЕГИСТРЕ\n"
        "- Пиши максимально подробно - это должно быть 5-6 страниц!\n"
        "- Давай конкретные примеры и рекомендации\n"
    )
    
    user_prompt = (
        f"Натальная карта:\n"
        f"Дата: {chart['datetime_local']}\n"
        f"Асцендент: {chart['asc']}\n"
        f"MC: {chart['mc']}\n\n"
        f"Планеты:\n{planets_list}\n\n"
        f"Сделай МАКСИМАЛЬНО ПОДРОБНЫЙ анализ по всем разделам!"
    )
    
    interpretation = await openai_request(system_prompt, user_prompt, max_tokens=6000)
    
    pdf = await build_pdf_natal(chart, interpretation)
    
    await bot.send_document(
        user_id,
        types.BufferedInputFile(pdf, "natal_chart.pdf"),
        caption="⭐ Ваша натальная карта готова!"
    )

async def process_synastry(user_id: int, message: types.Message):
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
        "Ты профессиональный астролог по синастрии. Проанализируй совместимость пары на 3-4 страницы.\n\n"
        "СТРУКТУРА АНАЛИЗА (каждый раздел с заголовком):\n\n"
        "СИЛЬНЫЕ СТОРОНЫ ОТНОШЕНИЙ\n"
        "Подробно опиши что объединяет партнёров:\n"
        "• Какие качества друг друга они ценят\n"
        "• В чём их естественная гармония\n"
        "• Что делает отношения крепкими\n"
        "• Общие интересы и ценности\n"
        "• Сексуальная совместимость\n"
        "(минимум 1 страница)\n\n"
        
        "ВОЗМОЖНЫЕ ТРУДНОСТИ И КОНФЛИКТЫ\n"
        "Честно расскажи о потенциальных проблемах:\n"
        "• В чём партнёры могут не понимать друг друга\n"
        "• Какие различия в характерах и потребностях\n"
        "• Где возможны конфликты и непонимание\n"
        "• Что может раздражать друг друга\n"
        "• Сложности в бытовом плане\n"
        "(минимум 1 страница)\n\n"
        
        "СОВЕТЫ ДЛЯ ГАРМОНИИ И РАЗВИТИЯ\n"
        "Дай конкретные практические рекомендации:\n"
        "• Как избегать конфликтов и недопониманий\n"
        "• На что обращать внимание в отношениях\n"
        "• Как проявлять любовь друг к другу\n"
        "• Что укрепит связь\n"
        "• Как поддерживать баланс и уважение\n"
        "(минимум 1 страница)\n\n"
        
        "ВАЖНО:\n"
        "- Пиши простым понятным языком БЕЗ астрологических терминов\n"
        "- НЕ используй символы решётки ###, звёздочки **, жирный шрифт - ТОЛЬКО ЧИСТЫЙ ТЕКСТ\n"
        "- Заголовки пиши ЗАГЛАВНЫМИ БУКВАМИ на отдельной строке\n"
        "- Будь честным и объективным, но тактичным\n"
        "- Давай конкретные примеры и рекомендации\n"
    )
    
    user_prompt = (
        f"ЧЕЛОВЕК A:\n"
        f"Дата: {synastry['chart_a']['datetime_local']}\n"
        f"Асцендент: {synastry['chart_a']['asc']}\n"
        f"Планеты:\n{planets_a}\n\n"
        f"ЧЕЛОВЕК B:\n"
        f"Дата: {synastry['chart_b']['datetime_local']}\n"
        f"Асцендент: {synastry['chart_b']['asc']}\n"
        f"Планеты:\n{planets_b}\n\n"
        f"Проанализируй совместимость этой пары МАКСИМАЛЬНО ПОДРОБНО!"
    )
    
    analysis = await openai_request(system_prompt, user_prompt, max_tokens=5000)
    
    pdf = await build_pdf_synastry(synastry["chart_a"], synastry["chart_b"], analysis)
    
    await bot.send_document(
        user_id,
        types.BufferedInputFile(pdf, "synastry.pdf"),
        caption="💑 Анализ совместимости готов!"
    )

# ===== ВЕБ-СЕРВЕР ДЛЯ RENDER =====

async def health_check(request):
    """Health check endpoint для Render"""
    return web.Response(text="Bot is running!")

async def start_web_server():
    """Запуск веб-сервера для Render"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    port = int(os.getenv('PORT', 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web server started on port {port}")

async def main():
    # Принудительно удаляем webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook удален")
    except Exception as e:
        print(f"⚠️ Ошибка удаления webhook: {e}")
    
    # Проверяем, что бот работает
    try:
        me = await bot.get_me()
        print(f"✅ Бот подключен: @{me.username} (ID: {me.id})")
    except Exception as e:
        print(f"❌ Не удалось подключиться к боту: {e}")
        return
    
    print("🚀 Запускаю веб-сервер и polling...")
    
    # Запускаем веб-сервер и бота параллельно
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot, skip_updates=True, allowed_updates=dp.resolve_used_update_types())
    )

if __name__ == "__main__":
    asyncio.run(main())
