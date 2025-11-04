import os
import io
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "/webhook/telegram")
ASTRO_API      = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

# ========= BOT/DP =========
bot = Bot(TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# ========= FASTAPI APP =========
app = FastAPI()

# ========= HTTP CLIENT =========
# Таймауты побольше, чтобы пережить холодный старт Render у сервиса эфемерид
client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=30.0, read=30.0, write=30.0))

class UpstreamError(Exception):
    pass

def _is_retryable(e: Exception) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        # 5xx/502 — ретраим
        return 500 <= e.response.status_code < 600
    return isinstance(e, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError, UpstreamError))

@retry(
    retry=retry_if_exception_type((httpx.HTTPError, UpstreamError)),
    wait=wait_exponential(multiplier=1.5, min=2, max=12),
    stop=stop_after_attempt(6),
    reraise=True,
)
async def call_api(path: str, json: Dict[str, Any]) -> Dict[str, Any]:
    """Вызов эндпоинта эфемерид с ретраями (переживём 502/вялый старт)."""
    url = f"{ASTRO_API}{path}"
    resp = await client.post(url, json=json)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Пробрасываем, чтобы tenacity сделал ретрай
        raise e
    data = resp.json()
    if not isinstance(data, dict):
        raise UpstreamError("Bad upstream payload")
    return data

@retry(
    retry=retry_if_exception_type((httpx.HTTPError, UpstreamError)),
    wait=wait_exponential(multiplier=1.5, min=2, max=10),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def resolve_place(city: str, country: str) -> Tuple[float, float, str]:
    payload = {"city": city, "country": country}
    data = await call_api("/api/resolve", payload)
    try:
        lat = float(data["lat"]); lon = float(data["lon"]); tz = str(data["iana_tz"])
    except Exception:
        raise UpstreamError("resolve returned malformed json")
    return lat, lon, tz

# ========= PDF (ReportLab) =========
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors

# Регистрируем шрифт с кириллицей
# На Render шрифта может не быть — грузим из стандартного пакета reportlab (DejaVuSans)
# Если у тебя есть свой .ttf, можешь положить рядом и заменить путь/имя.
try:
    pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
except Exception:
    # fallback: встроенный Helvetica (без кириллицы) — но попробуем всё-таки DejaVu, он обычно есть
    pass

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVu", fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=12))
styles.add(ParagraphStyle(name="HeadRu",  fontName="DejaVu", fontSize=12, leading=16, alignment=TA_LEFT, spaceBefore=8, spaceAfter=6))
styles.add(ParagraphStyle(name="TextRu",  fontName="DejaVu", fontSize=11, leading=16, alignment=TA_LEFT, spaceAfter=6))
styles.add(ParagraphStyle(name="SmallRu", fontName="DejaVu", fontSize=9, leading=12, alignment=TA_LEFT, spaceAfter=4))

def _table(data: List[List[str]]) -> Table:
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), "DejaVu"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.whitesmoke),
        ("ALIGN", (0,0), (-1,0), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    return t

def _friendly_dt(dt_local: str, tz: str) -> str:
    return f"{dt_local} • {tz}"

def build_pdf_natal(payload: Dict[str, Any]) -> bytes:
    """
    Делаем ≥5 страниц:
    1) Титул + контрольные цифры
    2) Планеты по знакам/домам (таблица)
    3) Интерпретационные блоки (тёплые и конкретные, без «воды»)
    4) Ещё интерпретации (характер/работа/отношения)
    5) Резюме + рекомендации
    """
    chart = payload["chart"]
    planets = chart.get("planets", [])
    houses  = chart.get("houses", {})
    dt_loc  = chart.get("datetime_local", "—")
    tz      = chart.get("iana_tz", "—")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story: List[Any] = []

    # 1) Титул
    story += [
        Paragraph("Натальная карта (Placidus)", styles["TitleRu"]),
        Paragraph(f"Дата/время: { _friendly_dt(dt_loc, tz) }", styles["TextRu"]),
        Spacer(1, 8),
    ]

    # Контрольные цифры (ASC/MC/☉/☽ + четыре классики для проверки)
    ctrl = [
        ["Элемент", "Значение"],
        ["ASC", f"{chart.get('asc', '—')}"],
        ["MC",  f"{chart.get('mc',  '—')}"],
        ["Солнце",  _fmt_planet(planets, "Sun")],
        ["Луна",    _fmt_planet(planets, "Moon")],
        ["Меркурий",_fmt_planet(planets, "Mercury")],
        ["Венера",  _fmt_planet(planets, "Venus")],
        ["Марс",    _fmt_planet(planets, "Mars")],
    ]
    story += [_table(ctrl), Spacer(1, 12), PageBreak()]

    # 2) Планеты: таблица знаков/долгот/ретроградности
    tbl = [["Планета", "Долгота", "Знак", "R"]]
    for p in planets:
        tbl.append([p["name"], f"{round(p['lon'],2)}°", p.get("sign","—"), "R" if p.get("retro") else ""])
    story += [
        Paragraph("Планеты — позиции", styles["HeadRu"]),
        _table(tbl),
        Spacer(1, 12),
        Paragraph("Дома (сводка)", styles["HeadRu"]),
        _table([["Система домов", houses.get("house_system","Placidus")]]),
        PageBreak()
    ]

    # 3) Интерпретационный блок — тёплый, конкретный
    story += [
        Paragraph("Как читать эту карту", styles["HeadRu"]),
        Paragraph(
            "Ниже — краткая, понятная и приземлённая интерпретация. "
            "Цель — дать ясность и поддержать твои решения, без перегруза терминами.", styles["TextRu"]),
        Spacer(1, 6),
        Paragraph(_warm_block_core(planets, houses), styles["TextRu"]),
        PageBreak()
    ]

    # 4) Блоки по сферам
    story += [
        Paragraph("Характер и базовые паттерны", styles["HeadRu"]),
        Paragraph(_sphere_character(planets), styles["TextRu"]),
        Spacer(1, 8),
        Paragraph("Работа/реализация", styles["HeadRu"]),
        Paragraph(_sphere_work(planets), styles["TextRu"]),
        Spacer(1, 8),
        Paragraph("Отношения/близость", styles["HeadRu"]),
        Paragraph(_sphere_relations(planets), styles["TextRu"]),
        PageBreak()
    ]

    # 5) Резюме
    story += [
        Paragraph("Что важно сейчас", styles["HeadRu"]),
        Paragraph(_final_advice(planets), styles["TextRu"]),
        Spacer(1, 8),
        Paragraph("Это краткий отчёт. Для детальной проработки я смогу дополнить карту прогностиками. "
                  "Если захочешь — просто напиши.", styles["SmallRu"])
    ]

    doc.build(story)
    return buf.getvalue()

def _fmt_planet(planets: List[Dict[str, Any]], name: str) -> str:
    for p in planets:
        if p.get("name") == name:
            sign = p.get("sign","")
            return f"{round(p['lon'],2)}° {sign}"
    return "—"

# ——— Мини-логика интерпретаций без ИИ (тёплые, конкретные) ———

def _warm_block_core(planets, houses) -> str:
    sun_sign = _find_sign(planets, "Sun")
    moon_sign = _find_sign(planets, "Moon")
    asc = houses.get("asc_sign") or houses.get("asc") or "ASC"

    return (
        f"Солнце в {sun_sign} — твоя энергия раскрывается, когда есть ощущение смысла и собственных правил. "
        f"Луна в {moon_sign} — эмоциональная регуляция через знакомые привычки и надёжные связи. "
        f"Асцендент ({asc}) окрашивает стиль взаимодействия — люди считывают тебя именно так с первых минут. "
        "В этой карте важно не «идеально соответствовать» архетипу, а замечать, где уже есть ресурс, и на него опираться."
    )

def _sphere_character(planets) -> str:
    mars = _find_sign(planets, "Mars")
    merc = _find_sign(planets, "Mercury")
    return (f"Марс в {mars} — реакция на вызовы достаточно прямая, но лучше срабатывает стратегия «короткими рывками». "
            f"Меркурий в {merc} — сильная сторона коммуникации: структурировать мысли и переводить сложное в простое.")

def _sphere_work(planets) -> str:
    venus = _find_sign(planets, "Venus")
    jup = _find_sign(planets, "Jupiter")
    return (f"Венера в {venus} — устойчивый вкус к качеству и эстетике, что хорошо ложится на продукт/контент. "
            f"Юпитер в {jup} — развитие через расширение контекста: обучение, публикации, международка.")

def _sphere_relations(planets) -> str:
    moon = _find_sign(planets, "Moon")
    return (f"Луна в {moon} подсказывает: эмоциональная безопасность — первична. "
            "В отношениях выигрывает спокойная ясность границ и ритуалы заботы, которые повторяются изо дня в день.")

def _final_advice(planets) -> str:
    sat = _find_sign(planets, "Saturn")
    return (f"Сатурн в {sat} напоминает: чтобы росло важное, нужно делать маленькие, но регулярные шаги. "
            "Выбери 1–2 фокуса на месяц, закрепляй их в расписании и измеряй прогресс. Остальное подтянется.")

def _find_sign(planets, name) -> str:
    for p in planets:
        if p.get("name") == name:
            return p.get("sign","знаке")
    return "знаке"

def build_pdf_horary(payload: Dict[str, Any]) -> bytes:
    chart = payload["chart"]
    planets = chart.get("planets", [])
    houses  = chart.get("houses", {})
    dt_loc  = chart.get("datetime_local", "—")
    tz      = chart.get("iana_tz", "—")

    moon = next((p for p in planets if p["name"]=="Moon"), None)
    moon_next = (moon or {}).get("next_applying")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story: List[Any] = []

    story += [
        Paragraph("Хорар: краткий ответ (Regiomontanus)", styles["TitleRu"]),
        Paragraph(f"Момент вопроса: { _friendly_dt(dt_loc, tz) }", styles["TextRu"]),
        Spacer(1, 10),
        Paragraph("Контрольные цифры", styles["HeadRu"]),
        _table([
            ["Элемент","Значение"],
            ["ASC", f"{chart.get('asc','—')}"],
            ["MC",  f"{chart.get('mc','—')}"],
            ["Солнце",  _fmt_planet(planets, "Sun")],
            ["Луна",    _fmt_planet(planets, "Moon")],
        ]),
        Spacer(1, 8),
        Paragraph("Логика по Лилли (упрощённо)", styles["HeadRu"]),
        Paragraph(_horary_text(moon_next), styles["TextRu"]),
    ]

    doc.build(story)
    return buf.getvalue()

def _horary_text(moon_next) -> str:
    if moon_next:
        asp = moon_next.get("aspect","")
        to  = moon_next.get("to","")
        return (f"Ближайший применяющийся аспект Луны — {asp} к {to}. "
                "Если аспекты поддерживающие — ответ ближе к «да»; напряжённые — «нет» или «при условии». "
                "Уточни условия и сроки, если есть зависимость от третьих факторов.")
    return "Луна без курса — чаще «нет» или «неопределённо сейчас». Переформулируй вопрос/сроки."

def build_pdf_synastry(payload: Dict[str, Any]) -> bytes:
    a = payload["a"]; b = payload["b"]
    dt_a = a["chart"].get("datetime_local","—"); tz_a = a["chart"].get("iana_tz","—")
    dt_b = b["chart"].get("datetime_local","—"); tz_b = b["chart"].get("iana_tz","—")

    # ТОП-аспекты по орбу, если API вернёт (если нет — краткая общая динамика)
    top = payload.get("top_aspects") or []

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story: List[Any] = []

    story += [
        Paragraph("Синастрия: краткий отчёт", styles["TitleRu"]),
        Paragraph(f"A: {dt_a} • {tz_a}", styles["SmallRu"]),
        Paragraph(f"B: {dt_b} • {tz_b}", styles["SmallRu"]),
        Spacer(1, 8)
    ]

    if top:
        rows = [["A — аспект — B", "Орб"]]
        for t in top[:10]:
            rows.append([f"{t['a']} {t['aspect']} {t['b']}", f"{t['orb']:.2f}°"])
        story += [Paragraph("ТОП-10 аспектов (меньший орб — выше):", styles["HeadRu"]), _table(rows), Spacer(1, 8)]

    story += [
        Paragraph("Динамика связи (в целом)", styles["HeadRu"]),
        Paragraph(_synastry_dynamics(), styles["TextRu"]),
        PageBreak()
    ]

    # Ещё 2 страницы с разбором эмоциональной/бытовой совместимости
    story += [
        Paragraph("Эмоциональная совместимость", styles["HeadRu"]),
        Paragraph(_synastry_emotional(), styles["TextRu"]),
        PageBreak(),
        Paragraph("Быт/ритм/ценности", styles["HeadRu"]),
        Paragraph(_synastry_life(), styles["TextRu"]),
    ]

    doc.build(story)
    return buf.getvalue()

def _synastry_dynamics() -> str:
    return ("Зоны притяжения проявляются там, где лёгкие аспекты (трины/секстили) связывают личные планеты — "
            "там проще договариваться и вдохновлять друг друга. Напряжение обычно локализуется в квадратах/оппозициях — "
            "это точки роста, где помогает проговаривание правил и регулярные «сверки карт».")

def _synastry_emotional() -> str:
    return ("Стабильность растёт, если базовые эмоциональные стратегии совпадают: как каждый успокаивается, "
            "как просит о поддержке, как выходит из конфликта. Поддерживающие ритуалы (общие завтраки, прогулки, "
            "созвон по пятницам) работают лучше великих обещаний.")

def _synastry_life() -> str:
    return ("В быту важны темп и роли: кто берёт на себя организацию, кто отвечает за деньги, кто инициирует отдых. "
            "Если есть расхождения, решает не компромисс «по чуть-чуть», а ясное разделение: «ты — здесь капитан, "
            "я — здесь», с правом вето на перегрузы.")

# ========= ФОН РАСЧЁТОВ =========

async def build_and_send_pdf(chat_id: int, kind: str, args: Dict[str, Any]):
    """
    kind: 'natal' | 'horary' | 'synastry'
    args:
      natal/horary: {dt: "YYYY-MM-DDTHH:MM", city, country, house_system?}
      synastry: {a:{dt, city, country}, b:{...}}
    """
    try:
        # небольшой прогрев апстрима (запрос к /health у твоего API можно добавить при желании)
        # — опущу для краткости, ретраи выше всё равно есть.

        if kind == "natal":
            lat, lon, tz = await resolve_place(args["city"], args["country"])
            payload = {
                "datetime_local": args["dt"],
                "lat": lat, "lon": lon, "iana_tz": tz,
                "house_system": "Placidus"
            }
            data = await call_api("/api/chart", payload)
            pdf_bytes = build_pdf_natal(data)
            caption = "Натальная карта — PDF"
            filename = "natal.pdf"

        elif kind == "horary":
            lat, lon, tz = await resolve_place(args["city"], args["country"])
            payload = {
                "datetime_local": args["dt"],
                "lat": lat, "lon": lon, "iana_tz": tz,
                "house_system": "Regiomontanus"
            }
            data = await call_api("/api/horary", payload)
            pdf_bytes = build_pdf_horary(data)
            caption = "Хорар — краткий ответ (PDF)"
            filename = "horary.pdf"

        else:  # synastry
            lat_a, lon_a, tz_a = await resolve_place(args["a"]["city"], args["a"]["country"])
            lat_b, lon_b, tz_b = await resolve_place(args["b"]["city"], args["b"]["country"])
            pa = {"datetime_local": args["a"]["dt"], "lat": lat_a, "lon": lon_a, "iana_tz": tz_a, "house_system": "Placidus"}
            pb = {"datetime_local": args["b"]["dt"], "lat": lat_b, "lon": lon_b, "iana_tz": tz_b, "house_system": "Placidus"}
            data_a = await call_api("/api/chart", pa)
            data_b = await call_api("/api/chart", pb)
            # Если у твоего API есть отдельный /api/synastry — можно позвать его.
            payload = {"a": data_a, "b": data_b, "top_aspects": []}
            pdf_bytes = build_pdf_synastry(payload)
            caption = "Синастрия — PDF"
            filename = "synastry.pdf"

        file = types.BufferedInputFile(pdf_bytes, filename=filename)
        await bot.send_document(chat_id, document=file, caption=caption)

    except Exception as e:
        # честно сообщаем об ошибке
        text = ("⚠️ Сервис эфемерид сейчас недоступен (502/таймаут) или данные не получены. "
                "Попробуй ещё раз через несколько минут.")
        try:
            await bot.send_message(chat_id, text)
        except:
            pass

# ========= ПАРСИНГ КОМАНД =========

def _parse_one_line(s: str) -> Tuple[str, str, str]:
    # "ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 4:
        raise ValueError("Нужен формат: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    dt = _to_iso(parts[0], parts[1])
    city = parts[2]
    country = ",".join(parts[3:]).strip()
    return dt, city, country

def _to_iso(d: str, t: str) -> str:
    # "17.08.2002", "15:20" -> "2002-08-17T15:20"
    dd, mm, yy = d.split(".")
    return f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}T{t.zfill(5)}"

# ========= TELEGRAM HANDLERS =========

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    text = (
        "Привет 🙂\n\n"
        "Доступные команды:\n"
        "• <b>/natal</b> ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "• <b>/horary</b> ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "• <b>/synastry</b> две строки сразу после команды:\n"
        "  A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "  B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n\n"
        "Я сразу подтвержу приём и пришлю PDF как только досчитаю."
    )
    await m.answer(text)

@dp.message(F.text.regexp(r"^/natal\s+(.+)$"))
async def on_natal(m: types.Message, regexp: types.MessageEntity):
    try:
        arg = m.text.split(" ", 1)[1].strip()
        dt, city, country = _parse_one_line(arg)
    except Exception:
        return await m.answer("Формат: /natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")

    await m.answer("Приняла, считаю натал… пришлю PDF.")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "natal", {"dt": dt, "city": city, "country": country}))

@dp.message(F.text.regexp(r"^/horary\s+(.+)$"))
async def on_horary(m: types.Message, regexp: types.MessageEntity):
    try:
        arg = m.text.split(" ", 1)[1].strip()
        dt, city, country = _parse_one_line(arg)
    except Exception:
        return await m.answer("Формат: /horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")

    await m.answer("Приняла, считаю хорар… пришлю PDF.")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "horary", {"dt": dt, "city": city, "country": country}))

@dp.message(F.text.regexp(r"^/synastry(\s|\n)+(.+)$"))
async def on_synastry(m: types.Message):
    # Ожидаем две строки сразу после /synastry
    lines = m.text.splitlines()
    if len(lines) < 3:
        return await m.answer("После /synastry пришли ДВЕ строки:\nA: дата, время, город, страна\nB: дата, время, город, страна")
    try:
        a_str = lines[1].split(":",1)[-1].strip()
        b_str = lines[2].split(":",1)[-1].strip()
        dt_a, city_a, country_a = _parse_one_line(a_str)
        dt_b, city_b, country_b = _parse_one_line(b_str)
    except Exception:
        return await m.answer("Формат строк A/B неверный. Пример:\nA: 17.08.2002, 15:20, Кострома, Россия\nB: 04.07.1995, 12:00, Москва, Россия")

    await m.answer("Приняла, считаю синастрию… пришлю PDF.")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "synastry", {
        "a": {"dt": dt_a, "city": city_a, "country": country_a},
        "b": {"dt": dt_b, "city": city_b, "country": country_b},
    }))

# ========= FASTAPI ROUTES =========

@app.get("/")
async def root():
    return PlainTextResponse("ok")

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/setup")
async def setup_webhook():
    if not PUBLIC_URL:
        raise HTTPException(400, detail="PUBLIC_URL is not set")
    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(url, drop_pending_updates=True)
    return {"ok": True, "webhook": url}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        update = types.Update.model_validate(await request.json())
    except Exception:
        raise HTTPException(400, detail="invalid update")
    await dp.feed_update(bot, update)
    return JSONResponse({"ok": True})

# ========= UVICORN ENTRY =========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
