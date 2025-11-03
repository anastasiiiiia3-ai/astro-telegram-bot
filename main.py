import os
import re
import uuid
import asyncio
from typing import Dict, Any, Optional, List

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import Update, FSInputFile, BotCommand

# ===================== ENV =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL     = os.getenv("PUBLIC_URL")  # например: https://astro-telegram-bot-1.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "/webhook/astro")  # должен начинаться со слэша
ASTRO_API      = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is not set")

# ===================== TG CORE =================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ===================== HELPERS =================
DATE_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4}),\s*(\d{1,2}):(\d{2}),\s*(.+?),\s*(.+?)\s*$"
)
def parse_line(s: str):
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
        "• /synastry — две строки после команды:\n"
        "  A: `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
        "  B: `ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна`\n"
    )

# ===================== HTTP к astro-ephemeris (прогрев + ретраи) =================
HTTP_TIMEOUT = 60
WARMUP_URL   = f"{ASTRO_API}/health"

async def warmup_backend():
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            await cl.get(WARMUP_URL)
    except Exception:
        pass

async def api_post(path: str, json: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{ASTRO_API}{path}"
    await warmup_backend()
    last_err = None
    for attempt in range(4):  # 1s, 2s, 4s
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cl:
                r = await cl.post(url, json=json)
                r.raise_for_status()
                return r.json()
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError) as e:
            last_err = e
            if isinstance(e, httpx.HTTPStatusError) and (400 <= e.response.status_code < 500):
                break
            await asyncio.sleep(2 ** attempt)
    raise HTTPException(status_code=502, detail=f"backend error: {repr(last_err)}")

async def resolve_place(city: str, country: str) -> Dict[str, Any]:
    return await api_post("/api/resolve", {"city": city, "country": country})

# ===================== (опционально) PDF =================
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

def style(font="Helvetica", size=11, leading=15):
    return ParagraphStyle(name="P", fontName=font, fontSize=size, leading=leading, spaceAfter=6)

def mk_pdf(text: str, fname: str) -> Path:
    fpath = Path("/tmp")/fname
    doc = SimpleDocTemplate(str(fpath), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    flow = [Paragraph("Astro Report", style(size=16, leading=20)), Spacer(1,8), Paragraph(text, style())]
    doc.build(flow)
    return fpath

# ===================== TEXT TONE =================
def warm_intro() -> str:
    return (
        "Ниже — коротко и по делу, без перегруза терминами. "
        "Смысл — дать ясность и поддержать твои решения."
    )

# ===================== COMMANDS =================
@router.message(F.text.startswith("/start"))
async def cmd_start(m: types.Message):
    await bot.set_my_commands([
        BotCommand(command="start", description="Как пользоваться"),
        BotCommand(command="help", description="Подсказка по формату"),
        BotCommand(command="natal", description="Натальная карта"),
        BotCommand(command="horary", description="Хорарный вопрос"),
        BotCommand(command="synastry", description="Совместимость (2 строки)")
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
        return await m.answer("Так: `/natal 17.08.2002, 15:20, Кострома, Россия`", parse_mode="Markdown")
    loc = await resolve_place(parsed["city"], parsed["country"])
    body = {
        "datetime_local": parsed["datetime_local"],
        "lat": loc["lat"], "lon": loc["lon"], "iana_tz": loc["iana_tz"],
        "house_system": "Placidus"
    }
    data = await api_post("/api/chart", body)
    # краткий ответ
    txt = warm_intro() + "\n\n" + "Контрольные данные получены. Карта рассчитана корректно."
    pdf = mk_pdf(txt, f"astro_natal_{uuid.uuid4().hex[:8]}.pdf")
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
    data = await api_post("/api/horary", body)
    txt = warm_intro() + "\n\n" + "Хорарная сетка и Луна рассчитаны. Можно интерпретировать по Лилли."
    pdf = mk_pdf(txt, f"astro_horary_{uuid.uuid4().hex[:8]}.pdf")
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
    data = await api_post("/api/synastry", body)
    txt  = warm_intro() + "\n\n" + "Синастрические аспекты получены. Сводка по ТОП-аспектам готова."
    pdf  = mk_pdf(txt, f"astro_synastry_{uuid.uuid4().hex[:8]}.pdf")
    await m.answer(txt)
    await m.answer_document(FSInputFile(str(pdf)), caption="📄 Синастрия — PDF")

@router.message(F.text.regexp(r"^/"))
async def unknown_cmd(m: types.Message):
    await m.answer("Команда не распознана. Нажми /help — там примеры.")

# ===================== FASTAPI (uvicorn) =================
app = FastAPI(title="Astro Telegram Bot")

@app.get("/", response_class=PlainTextResponse)
def root():
    return "ok"

@app.get("/health")
def health():
    return {"ok": True}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(update: Dict[str, Any]):
    # максимально терпим к формату апдейта
    try:
        await dp.feed_update(bot, Update(**update))
    except Exception:
        try:
            upd = Update.model_validate(update)
            await dp.feed_update(bot, upd)
        except Exception as e:
            print("WEBHOOK ERROR:", repr(e))
    return JSONResponse({"ok": True})

@app.get("/setup", response_class=PlainTextResponse)
async def setup_webhook():
    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    ok = await bot.set_webhook(url, drop_pending_updates=True)
    if not ok:
        raise HTTPException(500, "set_webhook failed")
    return f"webhook set to {url}"
