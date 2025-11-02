import os, re, uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.webhook.integrations.fastapi import FastAPIWebhookRequestHandler
import httpx

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL     = os.getenv("PUBLIC_URL")  # https://<имя-сервиса>.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "/tg/webhook")
ASTRO_API      = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")
TIMEOUT        = 30

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN env var")

# ====== TELEGRAM ======
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== HELPERS ======
DATE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4}),\s*(\d{1,2}):(\d{2}),\s*(.+?),\s*(.+?)\s*$")

async def astro_run(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(f"{ASTRO_API}/api/run", json=payload)
        r.raise_for_status()
        return r.json()

def parse_datetime_city_country(text: str):
    """
    Формат: 'ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна'
    Возврат dict: datetime_local ISO + city, country
    """
    m = DATE_RE.match(text or "")
    if not m:
        return None
    d, mth, y, hh, mm, city, country = m.groups()
    iso = f"{int(y):04d}-{int(mth):02d}-{int(d):02d}T{int(hh):02d}:{int(mm):02d}"
    return {"datetime_local": iso, "city": city.strip(), "country": country.strip()}

def fmt_usage() -> str:
    return (
        "Привет! Я астробот на Swiss Ephemeris.\n\n"
        "Форматы команд:\n"
        "• /natal  — `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
        "• /horary — `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
        "• /synastry — две строки подряд после команды:\n"
        "  A: `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
        "  B: `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
    )

# ====== PDF (минимальный отчёт, кириллица без доп. шрифтов может выглядеть проще) ======
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]
def deg_sign(x: float) -> str:
    sign = SIGNS[int((x % 360)//30)]
    return f"{x:.2f}° {sign}"

STYLES = None
def init_styles():
    global STYLES
    if STYLES: return
    styles = getSampleStyleSheet()
    # Без внешних ttf: Helvetica (может быть без кириллицы на некоторых платформах).
    styles.add(ParagraphStyle(name="H1", fontName="Helvetica", fontSize=16, leading=20, spaceAfter=8))
    styles.add(ParagraphStyle(name="H2", fontName="Helvetica", fontSize=13, leading=16, spaceAfter=6))
    styles.add(ParagraphStyle(name="P",  fontName="Helvetica", fontSize=10, leading=14))
    STYLES = styles

def mk_table(data, colWidths=None):
    t = Table(data, colWidths=colWidths)
    t.setStyle(TableStyle([
        ("FONT", (0,0), (-1,-1), "Helvetica", 10),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f0f2f5")),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#d1d5db")),
        ("LEFTPADDING",(0,0),(-1,-1),6), ("RIGHTPADDING",(0,0),(-1,-1),6),
        ("TOPPADDING",(0,0),(-1,-1),4), ("BOTTOMPADDING",(0,0),(-1,-1),4),
    ]))
    return t

def build_pdf(mode: str, payload: dict, out_path: Path) -> Path:
    init_styles()
    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    flow = []
    flow += [Paragraph("Astro Report", STYLES["H1"]), Spacer(1, 6), Paragraph(f"Режим: {mode.upper()}", STYLES["P"])]

    if mode in ("natal","horary"):
        chart = payload["chart"] if mode == "horary" else payload
        dt = chart["datetime_local"]; lat = chart["lat"]; lon = chart["lon"]; tz = chart["iana_tz"]
        asc = chart["houses"]["asc"]; mc = chart["houses"]["mc"]
        planets = {p["name"]: p for p in chart["planets"]}

        flow += [Spacer(1,8), Paragraph("Контрольные позиции", STYLES["H2"])]
        rows = [["Точка","Положение"]]
        for key, label in [("Sun","Солнце ☉"), ("Moon","Луна ☽"), ("Mercury","Меркурий ☿"),
                           ("Venus","Венера ♀"), ("Mars","Марс ♂"), ("Jupiter","Юпитер ♃"), ("Saturn","Сатурн ♄")]:
            if key in planets:
                rows.append([label, deg_sign(planets[key]["lon"])])
        rows += [["ASC", deg_sign(asc)], ["MC", deg_sign(mc)]]
        flow.append(mk_table(rows, [140, 260]))

        flow += [Spacer(1,10), Paragraph(f"Дата/время: {dt}  |  Координаты: {lat:.4f}, {lon:.4f}  |  TZ: {tz}", STYLES["P"])]

        if mode == "horary":
            moon = payload["moon"]
            flow += [Spacer(1,8), Paragraph("Хорар — Луна", STYLES["H2"])]
            voc = "VOC (без курса)" if moon.get("voc") else "Есть применяющийся аспект"
            rows = [["Параметр","Значение"],
                    ["Положение Луны", deg_sign(moon["lon"])],
                    ["Статус", voc],
                    ["Ближ. применяющийся аспект", moon.get("next_applying","—")]]
            flow.append(mk_table(rows, [180, 220]))

    if mode == "synastry":
        aspects = payload.get("aspects", [])[:10]
        flow += [Spacer(1,8), Paragraph("Синастрия — ТОП-10 аспектов", STYLES["H2"])]
        rows = [["Планета A","Аспект","Планета B","Орб"]]
        for a in aspects:
            rows.append([a["p1"], a["aspect"], a["p2"], f'{a["orb"]:.2f}°'])
        flow.append(mk_table(rows, [120,110,120,60]))

    doc.build(flow)
    return out_path

# ====== HANDLERS ======
@router.message(F.text == "/start")
async def start(m: types.Message):
    await m.answer(fmt_usage(), parse_mode="Markdown")

@router.message(F.text.regexp(r"^/natal($|\s)"))
async def cmd_natal(m: types.Message):
    payload = m.text.replace("/natal", "", 1).strip()
    parsed = parse_datetime_city_country(payload)
    if not parsed:
        await m.answer("Дай данные так:\n`/natal 17.08.2002, 15:20, Кострома, Россия`", parse_mode="Markdown"); return
    body = {"mode": "natal", **parsed, "house_system": "Placidus"}
    try:
        data = await astro_run(body)
        await m.answer(data.get("text", "Готово."))
        # PDF
        fname = f"astro_natal_{uuid.uuid4().hex[:8]}.pdf"
        pdf_path = build_pdf("natal", data.get("payload", {}), Path("/tmp")/fname)
        from aiogram.types import FSInputFile
        await m.answer_document(FSInputFile(str(pdf_path)), caption="📄 Натальная карта — PDF")
    except httpx.HTTPError as e:
        await m.answer(f"Ошибка расчёта: {e}")

@router.message(F.text.regexp(r"^/horary($|\s)"))
async def cmd_horary(m: types.Message):
    payload = m.text.replace("/horary", "", 1).strip()
    parsed = parse_datetime_city_country(payload)
    if not parsed:
        await m.answer("Дай данные так:\n`/horary 04.07.2025, 22:17, Москва, Россия`", parse_mode="Markdown"); return
    body = {"mode": "horary", **parsed, "house_system": "Regiomontanus"}
    try:
        data = await astro_run(body)
        await m.answer(data.get("text", "Готово."))
        fname = f"astro_horary_{uuid.uuid4().hex[:8]}.pdf"
        pdf_path = build_pdf("horary", data.get("payload", {}), Path("/tmp")/fname)
        from aiogram.types import FSInputFile
        await m.answer_document(FSInputFile(str(pdf_path)), caption="📄 Хорар — PDF")
    except httpx.HTTPError as e:
        await m.answer(f"Ошибка расчёта: {e}")

@router.message(F.text.regexp(r"^/synastry($|\s)"))
async def cmd_synastry(m: types.Message):
    rest = m.text.replace("/synastry", "", 1).strip()
    lines = [ln.strip() for ln in rest.split("\n") if ln.strip()]
    if len(lines) < 2:
        ex = "Пожалуйста двумя строками:\n`/synastry`\n`17.08.2002, 15:20, Кострома, Россия`\n`04.07.1995, 12:00, Москва, Россия`"
        await m.answer(ex, parse_mode="Markdown"); return
    pa = parse_datetime_city_country(lines[0]); pb = parse_datetime_city_country(lines[1])
    if not pa or not pb:
        await m.answer("Проверь формат строк A и B."); return
    body = {"mode": "synastry", "a": pa, "b": pb}
    try:
        data = await astro_run(body)
        await m.answer(data.get("text", "Готово."))
        fname = f"astro_synastry_{uuid.uuid4().hex[:8]}.pdf"
        pdf_path = build_pdf("synastry", data.get("payload", {}), Path("/tmp")/fname)
        from aiogram.types import FSInputFile
        await m.answer_document(FSInputFile(str(pdf_path)), caption="📄 Синастрия — PDF")
    except httpx.HTTPError as e:
        await m.answer(f"Ошибка расчёта: {e}")

# ====== FASTAPI app + webhook ======
app = FastAPI(title="Astro TG Bot")

@app.get("/health")
def health(): return {"ok": True}

# Webhook handler
handler = FastAPIWebhookRequestHandler(dispatcher=dp, bot=bot)
app.post(WEBHOOK_PATH)(handler.handle)

@app.get("/setup", response_class=PlainTextResponse)
async def setup_webhook():
    """Установить вебхук: PUBLIC_URL + WEBHOOK_PATH"""
    if not PUBLIC_URL:
        raise HTTPException(400, "Set PUBLIC_URL env var")
    ok = await bot.set_webhook(url=f"{PUBLIC_URL}{WEBHOOK_PATH}")
    return "webhook set" if ok else "failed"
