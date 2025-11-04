import os
import io
import asyncio
from typing import Any, Dict, List, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode

# ====== ENV ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ASTRO_API = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

bot = Bot(TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
app = FastAPI()

# ====== HTTP CLIENT ======
client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0, read=60.0))

class EphemerisTemporaryError(Exception):
    pass

@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    retry=retry_if_exception_type((httpx.TimeoutException, EphemerisTemporaryError)),
)
async def astro_post(path: str, json: dict):
    url = f"{ASTRO_API}{path}"
    try:
        r = await client.post(url, json=json)
    except httpx.TimeoutException:
        raise
    if r.status_code >= 500:
        raise EphemerisTemporaryError(f"{r.status_code} on {url}")
    r.raise_for_status()
    return r.json()

async def astro_health() -> bool:
    try:
        r = await client.get(f"{ASTRO_API}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False

# ====== PDF ======
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors

try:
    pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
except Exception:
    pass

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="TitleRu", fontName="DejaVu", fontSize=18, leading=22, alignment=TA_CENTER, spaceAfter=12))
styles.add(ParagraphStyle(name="HeadRu", fontName="DejaVu", fontSize=12, leading=16, alignment=TA_LEFT, spaceBefore=8, spaceAfter=6))
styles.add(ParagraphStyle(name="TextRu", fontName="DejaVu", fontSize=11, leading=16, alignment=TA_LEFT, spaceAfter=6))

def _table(data: List[List[str]]) -> Table:
    t = Table(data, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), "DejaVu"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.whitesmoke),
        ("ALIGN", (0,0), (-1,0), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    return t

def build_pdf_natal(payload: Dict[str, Any]) -> bytes:
    chart = payload["chart"]
    planets = chart.get("planets", [])
    dt_loc = chart.get("datetime_local", "—")
    tz = chart.get("iana_tz", "—")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story: List[Any] = []

    story += [Paragraph("Натальная карта (Placidus)", styles["TitleRu"]),
              Paragraph(f"Дата и время: {dt_loc} ({tz})", styles["TextRu"]), Spacer(1, 8)]
    story += [_table([["Элемент","Значение"],["ASC",chart.get("asc","—")],["MC",chart.get("mc","—")]]), PageBreak()]

    rows = [["Планета","Долгота","Знак","R"]]
    for p in planets:
        rows.append([p["name"], f"{round(p['lon'],2)}°", p.get("sign","—"), "R" if p.get("retro") else ""])
    story += [Paragraph("Планеты", styles["HeadRu"]), _table(rows), PageBreak()]

    for i in range(3):
        story += [Paragraph(f"Раздел {i+1}", styles["HeadRu"]),
                  Paragraph("Тёплая, понятная интерпретация без перегруза терминами. "
                            "Фокус на поддержке и ясности.", styles["TextRu"]),
                  PageBreak()]
    story += [Paragraph("Резюме", styles["HeadRu"]),
              Paragraph("Опирайся на устойчивые качества и бережно расширяй зону контроля.", styles["TextRu"])]
    doc.build(story)
    return buf.getvalue()

def build_pdf_horary(payload: Dict[str, Any]) -> bytes:
    chart = payload["chart"]
    dt_loc = chart.get("datetime_local", "—")
    tz = chart.get("iana_tz", "—")
    buf = io.BytesIO(); doc = SimpleDocTemplate(buf, pagesize=A4); story=[]
    story += [Paragraph("Хорар (Regiomontanus)", styles["TitleRu"]),
              Paragraph(f"Момент: {dt_loc} ({tz})", styles["TextRu"]), Spacer(1,8),
              _table([["ASC", chart.get("asc","—")], ["MC", chart.get("mc","—")]]),
              Paragraph("Краткий вывод: Луна и ближайший аспект подсказывают динамику. "
                        "Гармоника — ближе к «да», напряжение — «при условии/нет».", styles["TextRu"])]
    doc.build(story); return buf.getvalue()

def build_pdf_synastry(payload: Dict[str, Any]) -> bytes:
    buf = io.BytesIO(); doc = SimpleDocTemplate(buf, pagesize=A4); story=[]
    story += [Paragraph("Синастрия", styles["TitleRu"]),
              Paragraph("Совместимость и общая динамика", styles["HeadRu"]),
              Paragraph("Где тянет друг к другу, где нужны договорённости. "
                        "Тон — поддерживающий и практичный.", styles["TextRu"]), PageBreak()]
    for i in range(2):
        story += [Paragraph(f"Раздел {i+1}", styles["HeadRu"]),
                  Paragraph("Описание ключевых зон притяжения и напряжения; рабочие советы.", styles["TextRu"]),
                  PageBreak()]
    story += [Paragraph("Итог", styles["HeadRu"]),
              Paragraph("Согласовывайте темп, признавайте различия — так совместимость раскрывается.", styles["TextRu"])]
    doc.build(story); return buf.getvalue()

# ====== LOGIC ======
async def resolve_place(city: str, country: str) -> Tuple[float, float, str]:
    data = await astro_post("/api/resolve", {"city": city, "country": country})
    return float(data["lat"]), float(data["lon"]), str(data["iana_tz"])

async def build_and_send_pdf(chat_id: int, kind: str, args: Dict[str, Any]):
    try:
        await astro_health()  # прогрев
        if kind == "natal":
            lat, lon, tz = await resolve_place(args["city"], args["country"])
            data = await astro_post("/api/chart", {
                "datetime_local": args["dt"], "lat": lat, "lon": lon,
                "iana_tz": tz, "house_system": "Placidus"
            })
            pdf = build_pdf_natal(data)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "natal.pdf"), caption="Натальная карта — PDF")
        elif kind == "horary":
            lat, lon, tz = await resolve_place(args["city"], args["country"])
            data = await astro_post("/api/horary", {
                "datetime_local": args["dt"], "lat": lat, "lon": lon,
                "iana_tz": tz, "house_system": "Regiomontanus"
            })
            pdf = build_pdf_horary(data)
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "horary.pdf"), caption="Хорар — PDF")
        else:
            a, b = args["a"], args["b"]
            lat_a, lon_a, tz_a = await resolve_place(a["city"], a["country"])
            lat_b, lon_b, tz_b = await resolve_place(b["city"], b["country"])
            da = await astro_post("/api/chart", {"datetime_local": a["dt"], "lat": lat_a, "lon": lon_a, "iana_tz": tz_a, "house_system": "Placidus"})
            db = await astro_post("/api/chart", {"datetime_local": b["dt"], "lat": lat_b, "lon": lon_b, "iana_tz": tz_b, "house_system": "Placidus"})
            pdf = build_pdf_synastry({"a": da, "b": db})
            await bot.send_document(chat_id, types.BufferedInputFile(pdf, "synastry.pdf"), caption="Синастрия — PDF")
    except Exception:
        await bot.send_message(chat_id, "⚠️ Сервис эфемерид сейчас недоступен. Я пришлю PDF, как только досчитаю.")

# ====== PARSE ======
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
    await m.answer(
        "Привет 🙂\n\n"
        "Доступные команды:\n"
        "• /natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "• /horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "• /synastry (две строки: A: … и B: …)\n\n"
        "Я сразу подтвержу приём и пришлю PDF, как только всё досчитаю."
    )

@dp.message(lambda m: m.text and m.text.startswith("/natal"))
async def natal(m: types.Message):
    try:
        arg = m.text.split(" ",1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer("Формат: /natal 17.08.2002, 15:20, Кострома, Россия")
    await m.answer("Приняла ✅ Считаю натал… пришлю PDF.")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "natal", {"dt": dt, "city": city, "country": country}))

@dp.message(lambda m: m.text and m.text.startswith("/horary"))
async def horary(m: types.Message):
    try:
        arg = m.text.split(" ",1)[1]
        dt, city, country = _parse_line(arg)
    except Exception:
        return await m.answer("Формат: /horary 03.11.2025, 19:05, Москва, Россия")
    await m.answer("Приняла ✅ Считаю хорар… пришлю PDF.")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "horary", {"dt": dt, "city": city, "country": country}))

@dp.message(lambda m: m.text and m.text.startswith("/synastry"))
async def synastry(m: types.Message):
    lines = m.text.splitlines()
    if len(lines) < 3:
        return await m.answer("После /synastry пришли две строки:\nA: ...\nB: ...")
    try:
        a_str = lines[1].split(":",1)[-1].strip()
        b_str = lines[2].split(":",1)[-1].strip()
        dt_a, city_a, country_a = _parse_line(a_str)
        dt_b, city_b, country_b = _parse_line(b_str)
    except Exception:
        return await m.answer("Пример:\nA: 17.08.2002, 15:20, Кострома, Россия\nB: 04.07.1995, 12:00, Москва, Россия")
    await m.answer("Приняла ✅ Считаю синастрию… пришлю PDF.")
    asyncio.create_task(build_and_send_pdf(m.chat.id, "synastry", {
        "a": {"dt": dt_a, "city": city_a, "country": country_a},
        "b": {"dt": dt_b, "city": city_b, "country": country_b}
    }))

# ====== FASTAPI ======
@app.get("/")
async def root(): return PlainTextResponse("ok")

@app.get("/health")
async def health(): return {"ok": True}

# ====== Render: запускаем polling вместе с приложением ======
@app.on_event("startup")
async def on_startup():
    # стартуем Telegram polling фоном
    asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def on_shutdown():
    await client.aclose()
    try:
        await bot.session.close()
    except Exception:
        pass
