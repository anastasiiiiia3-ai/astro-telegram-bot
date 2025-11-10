import os
import io
import asyncio
from typing import Dict, Optional
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import httpx
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

# Импортируем наш астрологический модуль
from astro_calc import get_location, calculate_chart, calculate_horary, calculate_synastry

# ============= НАСТРОЙКИ =============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")  # Токен оплаты от @BotFather

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Необходимо задать TELEGRAM_TOKEN и OPENAI_API_KEY!")

# Регистрация шрифта
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
except Exception as err:
    print(f"⚠️ Ошибка регистрации шрифта: {err}")

# ============= ИНИЦИАЛИЗАЦИЯ =============
storage = MemoryStorage()
bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=storage)
http_client = httpx.AsyncClient(timeout=180)

# ============= FSM STATES =============
class UserStates(StatesGroup):
    waiting_question = State()
    waiting_natal_data = State()
    waiting_synastry_a = State()
    waiting_synastry_b = State()

# ============= ЦЕНЫ =============
PRICES = {
    "horary": {"amount": 10000, "title": "Хорарный вопрос", "label": "100₽"},
    "natal": {"amount": 30000, "title": "Натальная карта", "label": "300₽"},
    "synastry": {"amount": 30000, "title": "Синастрия", "label": "300₽"},
    "horary_extra": {"amount": 10000, "title": "Дополнительный хорарный вопрос", "label": "100₽"}
}

# ============= ХРАНИЛИЩЕ =============
user_data: Dict[int, dict] = {}

# ============= PDF СТИЛИ =============
styles = getSampleStyleSheet()
styles.add(ParagraphStyle("TitleRu", fontName="DejaVuSans", fontSize=20, alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#2c3e50")))
styles.add(ParagraphStyle("SectionRu", fontName="DejaVuSans", fontSize=14, alignment=TA_LEFT, spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#34495e")))
styles.add(ParagraphStyle("TextRu", fontName="DejaVuSans", fontSize=11, leading=16, alignment=TA_JUSTIFY, spaceAfter=10))
styles.add(ParagraphStyle("IntroRu", fontName="DejaVuSans", fontSize=11, alignment=TA_CENTER, spaceAfter=15, textColor=colors.gray))

# ============= OPENAI ЗАПРОСЫ =============
async def openai_request(system_prompt: str, user_prompt: str, max_tokens: int = 3000) -> str:
    """Запрос к GPT для форматирования астрологической информации"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    try:
        resp = await http_client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"❌ OpenAI ошибка: {e}")
        return "⚠️ Не удалось получить ответ от AI сервиса."

# ============= ГЕНЕРАЦИЯ PDF =============
def create_pdf(title: str, content: str, metadata: Optional[dict] = None) -> bytes:
    """Универсальная функция создания PDF"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=60, rightMargin=60, topMargin=50, bottomMargin=50)
    
    story = [Paragraph(title, styles["TitleRu"])]
    
    if metadata:
        for key, value in metadata.items():
            story.append(Paragraph(f"{key}: {value}", styles["IntroRu"]))
        story.append(Spacer(1, 14))
    
    # Разбиваем контент на параграфы
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    for p in paragraphs:
        story.append(Paragraph(p, styles["TextRu"]))
    
    doc.build(story)
    return buf.getvalue()

# ============= ОБРАБОТЧИКИ ОПЛАТЫ =============
async def create_invoice(chat_id: int, service_type: str, description: str):
    """Создание инвойса для оплаты"""
    price_info = PRICES[service_type]
    
    if not PAYMENT_TOKEN:
        await bot.send_message(chat_id, "⚠️ Оплата временно недоступна. Используйте тестовый режим.")
        return False
    
    prices = [LabeledPrice(label=price_info["title"], amount=price_info["amount"])]
    
    await bot.send_invoice(
        chat_id=chat_id,
        title=price_info["title"],
        description=description,
        payload=f"{service_type}_{chat_id}_{asyncio.get_event_loop().time()}",
        provider_token=PAYMENT_TOKEN,
        currency="RUB",
        prices=prices,
        start_parameter=f"pay_{service_type}"
    )
    return True

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    """Подтверждение оплаты"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Обработка успешной оплаты"""
    payment = message.successful_payment
    user_id = message.from_user.id
    
    # Определяем тип услуги из payload
    service_type = payment.invoice_payload.split("_")[0]
    
    await message.answer(
        f"✅ Оплата на сумму {payment.total_amount // 100}₽ прошла успешно!\n\n"
        f"Теперь отправьте необходимые данные для расчёта."
    )
    
    # Инициализируем данные пользователя
    if user_id not in user_data:
        user_data[user_id] = {}
    user_data[user_id]["paid_service"] = service_type
    user_data[user_id]["payment_amount"] = payment.total_amount

# ============= КОМАНДЫ БОТА =============
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    """Стартовое сообщение"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Хорарный вопрос (100₽)", callback_data="select_horary")],
        [InlineKeyboardButton(text="⭐ Натальная карта (300₽)", callback_data="select_natal")],
        [InlineKeyboardButton(text="💑 Синастрия (300₽)", callback_data="select_synastry")],
    ])
    
    await message.answer(
        "🌟 <b>Добро пожаловать в Астрологический бот!</b>\n\n"
        "Я помогу вам получить:\n"
        "• Точные астрологические расчёты\n"
        "• Понятные интерпретации от AI\n"
        "• Профессиональные PDF-отчёты\n\n"
        "Выберите услугу:",
        reply_markup=keyboard
    )

# ============= CALLBACK ОБРАБОТЧИКИ =============
@dp.callback_query(F.data.startswith("select_"))
async def service_selection(callback: types.CallbackQuery, state: FSMContext):
    """Выбор услуги"""
    service = callback.data.replace("select_", "")
    user_id = callback.from_user.id
    
    descriptions = {
        "horary": (
            "🔮 <b>Хорарный вопрос</b>\n\n"
            "Получите точный ответ на конкретный вопрос с астрологическим расчётом.\n\n"
            "Примеры:\n"
            "• Вернётся ли ко мне партнёр?\n"
            "• Получу ли я повышение?\n"
            "• Стоит ли покупать эту недвижимость?\n\n"
            "Формат: сначала отправьте вопрос, затем дату/время/место"
        ),
        "natal": (
            "⭐ <b>Натальная карта</b>\n\n"
            "Подробный анализ вашей личности с точными астрологическими расчётами:\n"
            "• Характер и таланты\n"
            "• Отношения и любовь\n"
            "• Карьера и призвание\n\n"
            "Формат: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
        ),
        "synastry": (
            "💑 <b>Синастрия</b>\n\n"
            "Анализ совместимости двух людей:\n"
            "• Сильные стороны отношений\n"
            "• Зоны роста\n"
            "• Рекомендации для гармонии\n\n"
            "Нужны данные обоих партнёров"
        )
    }
    
    await callback.message.answer(descriptions[service])
    
    # Создаём инвойс
    await create_invoice(user_id, service, PRICES[service]["title"])
    
    # Устанавливаем состояние
    if service == "horary":
        await state.set_state(UserStates.waiting_question)
    elif service == "natal":
        await state.set_state(UserStates.waiting_natal_data)
    elif service == "synastry":
        await state.set_state(UserStates.waiting_synastry_a)
    
    await callback.answer()

@dp.callback_query(F.data == "buy_horary_extra")
async def buy_extra_horary(callback: types.CallbackQuery, state: FSMContext):
    """Докупка дополнительного хорарного вопроса"""
    user_id = callback.from_user.id
    
    await callback.message.answer(
        "💬 <b>Дополнительный вопрос</b>\n\n"
        "Задайте новый вопрос, и я дам вам развёрнутый ответ с расчётами."
    )
    
    # Создаём инвойс
    await create_invoice(user_id, "horary_extra", "Дополнительный хорарный вопрос")
    await state.set_state(UserStates.waiting_question)
    await callback.answer()

# ============= ОБРАБОТЧИКИ ДАННЫХ =============
@dp.message(UserStates.waiting_question)
async def receive_question(message: types.Message, state: FSMContext):
    """Получение хорарного вопроса"""
    await state.update_data(question=message.text)
    await message.answer(
        "✅ Вопрос принят!\n\n"
        "Теперь отправьте дату, время и место вопроса в формате:\n"
        "<code>ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна</code>\n\n"
        "Пример: <code>10.11.2025, 14:30, Москва, Россия</code>"
    )

@dp.message(F.text.regexp(r"\d{2}\.\d{2}\.\d{4}"))
async def process_datetime_input(message: types.Message, state: FSMContext):
    """Обработка даты/времени/места для хорарного вопроса"""
    current_state = await state.get_state()
    
    if current_state != UserStates.waiting_question:
        return
    
    try:
        # Парсим данные
        parts = [p.strip() for p in message.text.split(",")]
        if len(parts) < 4:
            raise ValueError("Недостаточно данных")
        
        date_str = parts[0]
        time_str = parts[1]
        city = parts[2]
        country = ",".join(parts[3:])
        
        # Формируем ISO datetime
        dd, mm, yyyy = date_str.split(".")
        dt_iso = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{time_str}"
        
        # Получаем вопрос из состояния
        data = await state.get_data()
        question = data.get("question", "Нет вопроса")
        
        await message.answer("⏳ Выполняю хорарный расчёт, подождите...")
        
        # Получаем координаты
        lat, lon, tz_name = await get_location(city, country)
        
        # Рассчитываем хорарную карту
        chart = calculate_horary(dt_iso, lat, lon, tz_name)
        
        # Форматируем данные для GPT
        chart_text = f"""
Вопрос: {question}
Дата: {date_str}, Время: {time_str}
Место: {city}, {country}

Асцендент: {chart['asc']}
МС: {chart['mc']}

Планеты:
{chr(10).join([f"{p['name']}: {p['sign']} {round(p['lon'] % 30, 1)}°" for p in chart['planets']])}
        """
        
        # Запрос к GPT с инструкцией о follow-up вопросах
        system_prompt = (
            "Ты опытный хорарный астролог. Дай ответ в формате:\n\n"
            "1) **Краткий ответ**: Да/Нет/Скорее да/Скорее нет\n"
            "2) **Пояснение** (2-3 пункта почему так)\n"
            "3) **Совет** (что делать)\n"
            "4) **Дополнительный вопрос**: В конце ОБЯЗАТЕЛЬНО предложи один конкретный уточняющий вопрос, "
            "который поможет человеку глубже разобраться в ситуации. Начни с: "
            "\"💡 Хотите узнать: [конкретный вопрос]?\"\n\n"
            "Пиши простым языком, тепло и по делу."
        )
        
        interpretation = await openai_request(
            system_prompt,
            chart_text,
            max_tokens=1500
        )
        
        # Создаём PDF
        pdf_bytes = create_pdf(
            "ХОРАРНЫЙ ВОПРОС",
            interpretation,
            {
                "Вопрос": question,
                "Дата": f"{date_str}, {time_str}",
                "Место": f"{city}, {country}"
            }
        )
        
        # Отправляем с кнопкой для докупки
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Задать ещё вопрос (100₽)", callback_data="buy_horary_extra")]
        ])
        
        await bot.send_document(
            message.chat.id,
            types.BufferedInputFile(pdf_bytes, filename="horary_answer.pdf"),
            caption="✨ Ваш хорарный ответ готов!",
            reply_markup=keyboard
        )
        
        await state.clear()
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\n\nПроверьте формат данных.")

@dp.message(UserStates.waiting_natal_data)
async def receive_natal_data(message: types.Message, state: FSMContext):
    """Получение данных для натальной карты"""
    try:
        # Парсим данные
        parts = [p.strip() for p in message.text.split(",")]
        if len(parts) < 4:
            raise ValueError("Недостаточно данных")
        
        date_str = parts[0]
        time_str = parts[1]
        city = parts[2]
        country = ",".join(parts[3:])
        
        # Формируем ISO datetime
        dd, mm, yyyy = date_str.split(".")
        dt_iso = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{time_str}"
        
        await message.answer("⏳ Выполняю расчёты, это займёт 1-2 минуты...")
        
        # Получаем координаты
        lat, lon, tz_name = await get_location(city, country)
        
        # Рассчитываем натальную карту
        chart = calculate_chart(dt_iso, lat, lon, tz_name)
        
        # Форматируем данные для GPT
        chart_text = f"""
Асцендент: {chart['asc']}
МС (Середина неба): {chart['mc']}

Положения планет:
{chr(10).join([f"{p['name']}: {p['sign']} {round(p['lon'] % 30, 1)}° {'(ретроградная)' if p['retro'] else ''}" for p in chart['planets']])}
        """
        
        # Запрос к GPT
        system_prompt = (
            "Ты профессиональный астролог с 15-летним опытом. "
            "Создай подробную интерпретацию натальной карты простым языком без терминов. "
            "Структура:\n"
            "1) Общий портрет личности\n"
            "2) Характер и таланты\n"
            "3) Отношения и любовь\n"
            "4) Карьера и призвание\n\n"
            "Пиши тепло, поддерживающе и вдохновляюще."
        )
        
        interpretation = await openai_request(
            system_prompt,
            f"Данные натальной карты:\n{chart_text}\n\nДата: {date_str}, Время: {time_str}, Место: {city}, {country}",
            max_tokens=3000
        )
        
        # Создаём PDF
        pdf_bytes = create_pdf(
            "НАТАЛЬНАЯ КАРТА",
            interpretation,
            {
                "Дата рождения": f"{date_str}, {time_str}",
                "Место рождения": f"{city}, {country}",
                "Координаты": f"{round(lat, 2)}°, {round(lon, 2)}°"
            }
        )
        
        # Отправляем
        await bot.send_document(
            message.chat.id,
            types.BufferedInputFile(pdf_bytes, filename="natal_chart.pdf"),
            caption="✨ Ваша натальная карта готова!"
        )
        
        await state.clear()
        
    except Exception as e:
        await message.answer(f"❌ Ошибка обработки данных: {str(e)}\n\nПроверьте формат и попробуйте снова.")

@dp.message(UserStates.waiting_synastry_a)
async def receive_synastry_person_a(message: types.Message, state: FSMContext):
    """Получение данных первого человека для синастрии"""
    try:
        # Сохраняем данные первого человека
        await state.update_data(person_a=message.text)
        await message.answer(
            "✅ Данные первого человека приняты!\n\n"
            "Теперь отправьте данные второго человека в том же формате:\n"
            "<code>ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна</code>"
        )
        await state.set_state(UserStates.waiting_synastry_b)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(UserStates.waiting_synastry_b)
async def receive_synastry_person_b(message: types.Message, state: FSMContext):
    """Получение данных второго человека и расчёт синастрии"""
    try:
        # Получаем данные первого человека
        data = await state.get_data()
        person_a_text = data.get("person_a", "")
        person_b_text = message.text
        
        # Парсим оба набора данных
        def parse_input(text: str):
            parts = [p.strip() for p in text.split(",")]
            if len(parts) < 4:
                raise ValueError("Недостаточно данных")
            date_str = parts[0]
            time_str = parts[1]
            city = parts[2]
            country = ",".join(parts[3:])
            dd, mm, yyyy = date_str.split(".")
            dt_iso = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}T{time_str}"
            return dt_iso, date_str, time_str, city, country
        
        dt_a, date_a, time_a, city_a, country_a = parse_input(person_a_text)
        dt_b, date_b, time_b, city_b, country_b = parse_input(person_b_text)
        
        await message.answer("⏳ Рассчитываю синастрию, это займёт 1-2 минуты...")
        
        # Получаем координаты для обоих
        lat_a, lon_a, tz_a = await get_location(city_a, country_a)
        lat_b, lon_b, tz_b = await get_location(city_b, country_b)
        
        # Рассчитываем синастрию
        synastry = calculate_synastry(dt_a, lat_a, lon_a, tz_a, dt_b, lat_b, lon_b, tz_b)
        
        # Форматируем для GPT
        chart_a = synastry["chart_a"]
        chart_b = synastry["chart_b"]
        
        synastry_text = f"""
ЧЕЛОВЕК A:
Дата: {date_a}, {time_a}
Место: {city_a}, {country_a}
Асцендент: {chart_a['asc']}
Планеты: {', '.join([f"{p['name']} в {p['sign']}" for p in chart_a['planets'][:5]])}

ЧЕЛОВЕК B:
Дата: {date_b}, {time_b}
Место: {city_b}, {country_b}
Асцендент: {chart_b['asc']}
Планеты: {', '.join([f"{p['name']} в {p['sign']}" for p in chart_b['planets'][:5]])}
        """
        
        system_prompt = (
            "Ты профессиональный астролог. Создай подробный анализ совместимости пары.\n"
            "Структура:\n"
            "1) Общая характеристика союза\n"
            "2) Сильные стороны отношений\n"
            "3) Возможные сложности\n"
            "4) Рекомендации для гармонии\n\n"
            "Пиши тепло, поддерживающе и конструктивно. Без терминов."
        )
        
        interpretation = await openai_request(
            system_prompt,
            f"Данные синастрии:\n{synastry_text}",
            max_tokens=3000
        )
        
        # Создаём PDF
        pdf_bytes = create_pdf(
            "СИНАСТРИЯ — АНАЛИЗ СОВМЕСТИМОСТИ",
            interpretation,
            {
                "Человек A": f"{date_a}, {time_a} — {city_a}, {country_a}",
                "Человек B": f"{date_b}, {time_b} — {city_b}, {country_b}"
            }
        )
        
        await bot.send_document(
            message.chat.id,
            types.BufferedInputFile(pdf_bytes, filename="synastry.pdf"),
            caption="✨ Анализ совместимости готов!"
        )
        
        await state.clear()
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}\n\nПроверьте формат данных.")

# ============= ЗАПУСК БОТА =============
async def main():
    print("🤖 Бот запущен и готов к работе!")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
