import os
import re
import uuid
from pathlib import Path
from typing import Dict, Any, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import Update, FSInputFile, BotCommand
import httpx

# ===================== ENV =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL     = os.getenv("PUBLIC_URL")  # например: https://astro-telegram-bot-xxxx.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "/tg/webhook")
ASTRO_API      = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")
HTTP_TIMEOUT   = 30

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

# ===================== TG CORE =================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ===================== HELPERS =================
DATE_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4}),\s*(\d{1,2}):(\d{2}),\s*(.+?),\s*(.+?)\s*$"
)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы",
         "Скорпион","Стрелец","Козерог","Водолей","Рыбы"]

def deg_to_sign(lon: float) -> str:
    sign = SIGNS[int((lon % 360)//30)]
    return f"{lon:.2f}° {sign}"

def parse_line(s: str):
    """Парсим: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна -> dict | None"""
    m = DATE_RE.match(s or "")
    if not m:
        return None
    d, mo, y, hh, mm, city, country = m.groups()
    iso = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{int(hh):02d}:{int(mm):02d}"
    return {"datetime_local": iso, "city": city.strip(), "country": country.strip()}

def usage() -> str:
    return (
        "Привет! Я астробот на точных эфемеридах.\n\n"
        "Команды:\n"
        "• /natal  — `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
        "• /horary — `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
        "• /synastry — отправь две строки подряд после команды:\n"
        "  A: `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
        "  B: `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
    )

async def api_post(path: str, json: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cl:
        r = await cl.post(f"{ASTRO_API}{path}", json=json)
        r.raise_for_status()
        return r.json()

async def resolve_place(city: str, country: str) -> Dict[str, Any]:
    return await api_post("/api/resolve", {"city": city, "country": country})

# ===================== PDF (ReportLab) =================
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_FONTS_READY = False
def ensure_fonts():
    """Пытаемся подключить DejaVuSans для кириллицы. Если файла нет — используем Helvetica."""
    global _FONTS_READY
    if _FONTS_READY:
        return
    try:
        font_path = Path("fonts/DejaVuSans.ttf")
        if font_path.exists():
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(font_path)))
            _FONTS_READY = True
        else:
            _FONTS_READY = False
    except Exception:
        _FONTS_READY = False

def style(name: str, size=11, leading=15, bold=False):
    ensure_fonts()
    base = "DejaVuSans" if _FONTS_READY else "Helvetica"
    return ParagraphStyle(
        name=name,
        fontName=base,
        fontSize=size,
        leading=leading,
        spaceAfter=6,
    )

def table(data: List[List[str]], widths=None):
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle([
        ("FONT", (0,0), (-1,-1), "DejaVuSans" if _FONTS_READY else "Helvetica", 10),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f2f4f7")),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#d1d5db")),
        ("LEFTPADDING",(0,0),(-1,-1),6), ("RIGHTPADDING",(0,0),(-1,-1),6),
        ("TOPPADDING",(0,0),(-1,-1),4), ("BOTTOMPADDING",(0,0),(-1,-1),4),
    ]))
    return t

def mk_pdf(mode: str, payload: Dict[str, Any], text: str, fname: str) -> Path:
    fpath = Path("/tmp")/fname
    doc = SimpleDocTemplate(str(fpath), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    flow = []
    flow += [Paragraph("Astro Report", style("H1", 16, 20)), Spacer(1, 6),
             Paragraph(f"Режим: {mode.upper()}", style("P", 10, 14)),
             Spacer(1, 8)]

    # ——— Общая часть
    if mode in ("natal","horary"):
        chart = payload["chart"] if mode == "horary" else payload
        rows = [["Точка","Положение"]]
        planets = {p["name"]: p for p in chart["planets"]}
        for key, label in [("Sun","Солнце ☉"), ("Moon","Луна ☽"), ("Mercury","Меркурий ☿"),
                           ("Venus","Венера ♀"), ("Mars","Марс ♂"), ("Jupiter","Юпитер ♃"), ("Saturn","Сатурн ♄")]:
            if key in planets:
                rows.append([label, deg_to_sign(planets[key]["lon"])])
        rows += [["ASC", deg_to_sign(chart["houses"]["asc"])],
                 ["MC",  deg_to_sign(chart["houses"]["mc"])]]
        flow += [Paragraph("Контрольные позиции", style("H2", 13, 16)), table(rows, [150, 290]), Spacer(1, 8)]

    if mode == "horary":
        moon = payload.get("moon", {})
        voc = "VOC (без курса)" if moon.get("voc") else "Есть применяющийся аспект"
        rows = [["Параметр", "Значение"],
                ["Положение Луны", deg_to_sign(moon.get("lon", 0.0))],
                ["Статус", voc],
                ["Ближайший применяющийся аспект", moon.get("next_applying","—")]]
        flow += [Paragraph("Луна — хорарный контур", style("H2", 13, 16)), table(rows, [220, 220]), Spacer(1, 8)]

    if mode == "synastry":
        aspects = payload.get("aspects", [])[:10]
        rows = [["Планета A","Аспект","Планета B","Орб"]]
        for a in aspects:
            rows.append([a["p1"], a["aspect"], a["p2"], f'{a["orb"]:.2f}°'])
        flow += [Paragraph("Синастрия — ТОП-10 аспектов", style("H2", 13, 16)), table(rows, [120,110,120,60]), Spacer(1, 8)]

    flow += [Paragraph("Краткая интерпретация", style("H2", 13, 16)),
             Paragraph(text or "—", style("P", 11, 16))]

    doc.build(flow)
    return fpath

# ===================== TEXT TONES =================
def warm_intro() -> str:
    return (
        "Ниже — краткая выжимка без перегруза терминами. "
        "Смысл — помочь тебе лучше чувствовать свои процессы и принять ясные решения."
    )

def natal_text(chart: Dict[str, Any]) -> str:
    """Мини-интерпретация без поэзии: тёпло, поддерживающе, конкретно."""
    planets = {p["name"]: p for p in chart["planets"]}
    sun, moon = planets.get("Sun"), planets.get("Moon")
    asc = chart["houses"]["asc"]; mc = chart["houses"]["mc"]
    lines = [warm_intro()]
    if sun:  lines.append(f"☉ Солнце — {deg_to_sign(sun['lon'])}: основной вектор воли и жизненной энергии.")
    if moon: lines.append(f"☽ Луна — {deg_to_sign(moon['lon'])}: способы заботы о себе и эмоциональные ритмы.")
    lines.append(f"ASC — {deg_to_sign(asc)}: как тебя считывают с первого взгляда.")
    lines.append(f"MC  — {deg_to_sign(mc)}: траектория развития и тема признания.")
    return " ".join(lines)

def horary_text(payload: Dict[str, Any]) -> str:
    m = payload.get("moon", {})
    status = "луна без курса — ситуация тянется" if m.get("voc") else "луна идёт к аспекту — событие развивается"
    asp = m.get("next_applying", "аспект не выявлен")
    return (
        f"{warm_intro()} В хораре главное — сигнификаторы и Луна. "
        f"По Луне: {status}; ближайший применяющийся аспект — {asp}. "
        "Финальный ответ формулируем как Да/Нет/При условии после сопоставления сигнификаторов."
    )

def synastry_text(payload: Dict[str, Any]) -> str:
    return (
        f"{warm_intro()} В синастрии смотрим сочетание ☉/☽/ASC и личных планет. "
        "Гармоничные трины/секстили — зоны притяжения и лёгкости; квадраты/оппозиции — точки роста, "
        "где важны договорённости и регулярная обратная связь."
    )

# ===================== COMMANDS =================
@router.message(F.text.startswith("/start"))
async def cmd_start(m: types.Message):
    await bot.set_my_commands([
        BotCommand(command="start", description="Как пользоваться"),
        BotCommand(command="help", description="Подсказка по формату"),
        BotCommand(command="natal", description="Натальная карта"),
        BotCommand(command="horary", description="Хорарный вопрос"),
        BotCommand(command="synastry", description="Совместимость (2 строки)"),
    ])
    await m.answer(usage(), parse_mode="Markdown")

@router.message(F.text.startswith("/help"))
async def cmd_help(m: types.Message):
    await m.answer(usage(), parse_mode="Markdown")

@router.message(F.text.regexp(r"^/natal($|\s)"))
async def cmd_natal(m: types.Message):
    src = m.text.replace("/natal", "", 1).strip()
    parsed = parse_line(src)
    if not parsed:
        return await m.answer("Пожалуйста так: `/natal 17.08.2002, 15:20, Кострома, Россия`", parse_mode="Markdown")

    # 1) геокод
    loc = await resolve_place(parsed["city"], parsed["country"])
    body = {
        "datetime_local": parsed["datetime_local"],
        "lat": loc["lat"], "lon": loc["lon"], "iana_tz": loc["iana_tz"],
        "house_system": "Placidus"
    }
    # 2) карта
    data = await api_post("/api/chart", body)
    chart = data["chart"]

    # ответ тёплым тоном
    txt = natal_text(chart)
    # pdf
    pdf = mk_pdf("natal", chart, txt, f"astro_natal_{uuid.uuid4().hex[:8]}.pdf")
    await m.answer(txt)
    await m.answer_document(FSInputFile(str(pdf)), caption="📄 Натальная карта — PDF")

@router.message(F.text.regexp(r"^/horary($|\s)"))
async def cmd_horary(m: types.Message):
    src = m.text.replace("/horary", "", 1).strip()
    parsed = parse_line(src)
    if not parsed:
        return await m.answer("Так: `/horary 04.07.2025, 22:17, Москва, Россия`", parse_mode="Markdown")

    loc = await resolve_place(parsed["city"], parsed["country"])
    body = {
        "datetime_local": parsed["datetime_local"],
        "lat": loc["lat"], "lon": loc["lon"], "iana_tz": loc["iana_tz"],
        "house_system": "Regiomontanus"
    }
    data = await api_post("/api/horary", body)  # {chart:{...}, moon:{...}}
    txt  = horary_text(data)
    pdf  = mk_pdf("horary", data, txt, f"astro_horary_{uuid.uuid4().hex[:8]}.pdf")
    await m.answer(txt)
    await m.answer_document(FSInputFile(str(pdf)), caption="📄 Хорар — PDF")

@router.message(F.text.regexp(r"^/synastry($|\s)"))
async def cmd_synastry(m: types.Message):
    rest = m.text.replace("/synastry", "", 1).strip()
    lines = [ln.strip() for ln in rest.split("\n") if ln.strip()]
    if len(lines) < 2:
        return await m.answer(
            "Отправь двумя строками после команды:\n"
            "`/synastry`\n"
            "`17.08.2002, 15:20, Кострома, Россия`\n"
            "`04.07.1995, 12:00, Москва, Россия`",
            parse_mode="Markdown"
        )
    a = parse_line(lines[0]); b = parse_line(lines[1])
    if not a or not b:
        return await m.answer("Проверь формат двух строк. Должно быть как в примере.", parse_mode="Markdown")

    la = await resolve_place(a["city"], a["country"])
    lb = await resolve_place(b["city"], b["country"])
    body = {
        "a": {"datetime_local": a["datetime_local"], "lat": la["lat"], "lon": la["lon"], "iana_tz": la["iana_tz"], "house_system": "Placidus"},
        "b": {"datetime_local": b["datetime_local"], "lat": lb["lat"], "lon": lb["lon"], "iana_tz": lb["iana_tz"], "house_system": "Placidus"},
    }
    data = await api_post("/api/synastry", body)  # {a:{chart}, b:{chart}, aspects:[...]}
    txt  = synastry_text(data)
    pdf  = mk_pdf("synastry", data, txt, f"astro_synastry_{uuid.uuid4().hex[:8]}.pdf")
    await m.answer(txt)
    await m.answer_document(FSInputFile(str(pdf)), caption="📄 Синастрия — PDF")

# Фолбэк на всё остальное — дружелюбно подсказываем формат
@router.message(F.text.regexp(r"^/"))
async def unknown_cmd(m: types.Message):
    await m.answer("Команда не распознана. Нажми /help — там формат и примеры.")

# ===================== FASTAPI =================
app = FastAPI(title="Astro Telegram Bot")

@app.get("/health")
def health():
    return {"ok": True}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(update: Dict[str, Any]):
    """Принимаем апдейты напрямую (без специальных интеграций aiogram)"""
    await dp.feed_update(bot, Update.model_validate(update))
    return JSONResponse({"ok": True})

@app.get("/setup", response_class=PlainTextResponse)
async def setup_webhook():
    if not PUBLIC_URL:
        raise HTTPException(400, "PUBLIC_URL is not set")
    ok = await bot.set_webhook(f"{PUBLIC_URL}{WEBHOOK_PATH}")
    return "webhook set" if ok else "failed"


