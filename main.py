import os
import re
import uuid
import json
import asyncio
from typing import Any, Dict, List, Optional
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import Update, FSInputFile, BotCommand

# ===================== ENV =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL     = os.getenv("PUBLIC_URL")  # например: https://astro-telegram-bot-1.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "/webhook/astro")  # начинается со слэша
ASTRO_API      = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # для интерпретаций

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is not set")

# ===================== TG CORE =================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ===================== PARSE & TEXT =================
DATE_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4}),\s*(\d{1,2}):(\d{2}),\s*(.+?),\s*(.+?)\s*$"
)

def parse_line(s: str) -> Optional[Dict[str,str]]:
    m = DATE_RE.match(s or "")
    if not m:
        return None
    d, mo, y, hh, mm, city, country = m.groups()
    return {
        "datetime_local": f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{int(hh):02d}:{int(mm):02d}",
        "city": city.strip(),
        "country": country.strip()
    }

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

def warm_intro() -> str:
    return ("Ниже — коротко и по делу, без перегруза. "
            "Цель — дать ясность и поддержать твои решения.")

# ===================== HTTP к astro-ephemeris (прогрев + ретраи) =================
HTTP_TIMEOUT = 60
WARMUP_URL   = f"{ASTRO_API}/health"

async def warmup_backend():
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            await cl.get(WARMUP_URL)
    except Exception:
        pass

async def api_post(path: str, payload: Dict[str,Any]) -> Dict[str,Any]:
    """Устойчивый POST к твоему astro-ephemeris: 4 попытки, экспоненциальная пауза."""
    url = f"{ASTRO_API}{path}"
    await warmup_backend()
    last_err = None
    for attempt in range(4):  # 0,1,2,3 -> 1s,2s,4s
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cl:
                r = await cl.post(url, json=payload)
                r.raise_for_status()
                return r.json()
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError) as e:
            last_err = e
            # 4xx — не ретраим
            if isinstance(e, httpx.HTTPStatusError) and (400 <= e.response.status_code < 500):
                break
            await asyncio.sleep(2 ** attempt)
    raise HTTPException(status_code=502, detail=f"backend error: {repr(last_err)}")

async def resolve_place(city: str, country: str) -> Dict[str,Any]:
    return await api_post("/api/resolve", {"city": city, "country": country})

# ===================== PDF (простая верстка) =================
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors

def pstyle(size=11, leading=15, bold=False):
    return ParagraphStyle(
        name="P",
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size,
        leading=leading,
        spaceAfter=6,
    )

def mk_pdf(title: str, rows: List[List[str]], interp: str, fname: str) -> Path:
    fpath = Path("/tmp")/fname
    doc = SimpleDocTemplate(str(fpath), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    flow = [
        Paragraph("Astro Report", pstyle(16, 20, bold=True)),
        Paragraph(title, pstyle(12, 16)),
        Spacer(1, 8),
    ]
    if rows:
        t = Table(rows, colWidths=[180, 300])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#f2f4f7")),
            ("GRID",(0,0),(-1,-1), 0.25, colors.HexColor("#d1d5db")),
            ("FONT",(0,0),(-1,-1),"Helvetica",10),
            ("LEFTPADDING",(0,0),(-1,-1),6),
            ("RIGHTPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),4),
            ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ]))
        flow += [t, Spacer(1,8)]
    flow += [
        Paragraph("Краткая интерпретация", pstyle(13, 16, bold=True)),
        Paragraph(interp, pstyle(11, 16)),
    ]
    doc.build(flow)
    return fpath

# ===================== OpenAI интерпретации =================
def gpt_interpret(kind: str, payload: Dict[str,Any]) -> str:
    """
    kind: 'natal' | 'horary' | 'synastry'
    payload: ответ astro-ephemeris (строго печатаем то, что пришло)
    """
    if not OPENAI_API_KEY:
        return ("ℹ️ Интерпретация отключена (нет OPENAI_API_KEY). "
                "Контрольные данные см. в таблице выше.")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        system = (
            "Ты астролог-интерпретатор. Пиши тёплым, поддерживающим, но конкретным тоном. "
            "Коротко (5–10 предложений), без эзотерики и пафоса. Не выдумывай градусы — опирайся только на JSON."
        )
        user = f"""
Вид чтения: {kind}
JSON от эфемерид (используй только это):
{json.dumps(payload, ensure_ascii=False)}

Сформируй понятный вывод:
- natal: 2–3 сильные стороны, 1–2 зоны роста, общий вектор.
- horary: по Лилли — сигнификаторы, рецепции, ближайший применяющийся аспект Луны, VOC, итог Да/Нет/При условии.
- synastry: 5–8 тезисов про динамику пары (притяжение, напряжения, что помогает).
Стиль: человеческий, без жаргона. Конкретнее, меньше абстракций.
"""
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0.6,
            max_output_tokens=600,
        )
        return resp.output_text.strip()
    except Exception as e:
        return f"⚠️ Не удалось получить интерпретацию от GPT ({e}). Данные из эфемерид выведены корректно."

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
    src = m.text.replace("/natal","",1).strip()
    parsed = parse_line(src)
    if not parsed:
        return await m.answer("Так: `/natal 17.08.2002, 15:20, Кострома, Россия`", parse_mode="Markdown")
    loc = await resolve_place(parsed["city"], parsed["country"])
    req = {
        "datetime_local": parsed["datetime_local"],
        "lat": loc["lat"], "lon": loc["lon"], "iana_tz": loc["iana_tz"],
        "house_system": "Placidus"
    }
    data = await api_post("/api/chart", req)
    chart = data.get("chart", data)
    # Таблица контроля (ASC/MC + 7 классических)
    rows = [["Точка","Положение"]]
    houses = chart.get("houses", {})
    if "asc" in houses: rows.append(["ASC", f"{houses['asc']:.2f}"])
    if "mc"  in houses: rows.append(["MC",  f"{houses['mc']:.2f}"])
    plist = {p["name"]: p for p in chart.get("planets", [])}
    for k, label in [("Sun","Солнце ☉"),("Moon","Луна ☽"),("Mercury","Меркурий ☿"),
                     ("Venus","Венера ♀"),("Mars","Марс ♂"),("Jupiter","Юпитер ♃"),
                     ("Saturn","Сатурн ♄")]:
        if k in plist:
            rows.append([label, f"{plist[k]['lon']:.2f}° {plist[k].get('sign','')}"])
    interp = gpt_interpret("natal", chart)
    pdf = mk_pdf("Натальная карта (Placidus)", rows, interp, f"astro_natal_{uuid.uuid4().hex[:8]}.pdf")
    await m.answer(warm_intro() + "\n\n" + "Контрольные данные рассчитаны. См. PDF.")
    await m.answer_document(FSInputFile(str(pdf)), caption="📄 Натальная карта — PDF")

@router.message(F.text.regexp(r"^/horary($|\s)"))
async def cmd_horary(m: types.Message):
    src = m.text.replace("/horary","",1).strip()
    parsed = parse_line(src)
    if not parsed:
        return await m.answer("Так: `/horary 04.07.2025, 22:17, Москва, Россия`", parse_mode="Markdown")
    loc = await resolve_place(parsed["city"], parsed["country"])
    req = {
        "datetime_local": parsed["datetime_local"],
        "lat": loc["lat"], "lon": loc["lon"], "iana_tz": loc["iana_tz"],
        "house_system": "Regiomontanus"
    }
    data = await api_post("/api/horary", req)
    chart = data.get("chart", data)
    rows = [["Параметр","Значение"]]
    houses = chart.get("houses", {})
    if "asc" in houses: rows.append(["ASC", f"{houses['asc']:.2f}"])
    if "mc"  in houses: rows.append(["MC",  f"{houses['mc']:.2f}"])
    moon = chart.get("moon") or {}
    if isinstance(moon, dict):
        if "lon" in moon: rows.append(["Луна ☽", f"{moon['lon']:.2f}° {moon.get('sign','')}"])
        if "next_applying" in moon: rows.append(["Ближ. применяющийся аспект Луны", str(moon["next_applying"])])
        if "voc" in moon: rows.append(["Луна без курса (VOC)", "да" if moon["voc"] else "нет"])
    interp = gpt_interpret("horary", data)
    pdf = mk_pdf("Хорар (Regiomontanus)", rows, interp, f"astro_horary_{uuid.uuid4().hex[:8]}.pdf")
    await m.answer(warm_intro() + "\n\n" + "Хорарная сетка рассчитана. См. PDF.")
    await m.answer_document(FSInputFile(str(pdf)), caption="📄 Хорар — PDF")

@router.message(F.text.regexp(r"^/synastry($|\s)"))
async def cmd_synastry(m: types.Message):
    rest = m.text.replace("/synastry","",1).strip()
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
    req = {
        "a": {"datetime_local": a["datetime_local"], "lat": la["lat"], "lon": la["lon"], "iana_tz": la["iana_tz"], "house_system": "Placidus"},
        "b": {"datetime_local": b["datetime_local"], "lat": lb["lat"], "lon": lb["lon"], "iana_tz": lb["iana_tz"], "house_system": "Placidus"},
    }
    data = await api_post("/api/synastry", req)
    aspects = data.get("aspects", [])[:10]
    rows = [["Планета A — аспект — Планета B","Орб (°)"]]
    for asp in aspects:
        left = f"{asp.get('p1','?')} — {asp.get('aspect','?')} — {asp.get('p2','?')}"
        rows.append([left, f"{abs(asp.get('orb',0.0)):.2f}"])
    interp = gpt_interpret("synastry", data)
    pdf = mk_pdf("Синастрия (ТОП-аспекты)", rows, interp, f"astro_synastry_{uuid.uuid4().hex[:8]}.pdf")
    await m.answer(warm_intro() + "\n\n" + "Синастрия рассчитана. См. PDF.")
    await m.answer_document(FSInputFile(str(pdf)), caption="📄 Синастрия — PDF")

@router.message(F.text.regexp(r"^/"))
async def unknown_cmd(m: types.Message):
    await m.answer("Команда не распознана. Нажми /help — там формат и примеры.")

# ===================== FASTAPI (uvicorn) =================
app = FastAPI(title="Astro Telegram Bot")

@app.get("/", response_class=PlainTextResponse)
def root():
    return "ok"

@app.get("/health")
def health():
    return {"ok": True}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(update: Dict[str,Any]):
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
