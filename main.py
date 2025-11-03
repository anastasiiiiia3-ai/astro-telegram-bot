import os, io, json, asyncio
from typing import List, Tuple, Dict, Any

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, Update

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib.units import cm

# ---------- ENV ----------
TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
PUBLIC_URL  = os.getenv("PUBLIC_URL", "")
WEBHOOK_PATH= os.getenv("WEBHOOK_PATH", "/webhook")
ASTRO_API   = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set")

USE_GPT = bool(OPENAI_API_KEY)

# ---------- GPT (опционально) ----------
if USE_GPT:
    from openai import OpenAI
    gpt = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "Ты астролог-консультант. Пиши по-русски, тепло и поддерживающе, но конкретно и понятно, без эзотерики."
)

async def gpt_interpret(section: str, data: dict, model="gpt-4o-mini") -> str:
    if not USE_GPT:
        return ""
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Сделай развёрнутую интерпретацию раздела «{section}» (2–4 абзаца)."},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)}
    ]
    for attempt in range(4):
        try:
            resp = await asyncio.to_thread(
                gpt.chat.completions.create,
                model=model, messages=msgs, temperature=0.7
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            await asyncio.sleep(2 ** attempt)
    return ""

# ---------- PDF ----------
pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
def P(size=11): return ParagraphStyle(name=f"P{size}", fontName="HYSMyeongJo-Medium",
                                      fontSize=size, leading=15, spaceAfter=6)
H1 = ParagraphStyle(name="H1", fontName="HYSMyeongJo-Medium", fontSize=18, leading=22, spaceAfter=10)
H2 = ParagraphStyle(name="H2", fontName="HYSMyeongJo-Medium", fontSize=14, leading=18, spaceAfter=8)

def make_pdf(title: str, blocks: List[Tuple[str, str]]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    story = [Paragraph(title, H1), Spacer(1, 8)]
    for head, text in blocks:
        if not text: 
            continue
        story.append(Paragraph(head, H2))
        for para in (text or "").split("\n"):
            para = para.strip()
            if para:
                story.append(Paragraph(para, P()))
        story.append(Spacer(1, 6))
        # вставляем переносы страниц, чтобы гарантировать объём
        if len(story) % 12 == 0:
            story.append(PageBreak())
    # если совсем мало — добьём «тихими» пустыми абзацами
    while len(story) < 80:
        story.append(Paragraph("&nbsp;", P()))
    doc.build(story)
    buf.seek(0)
    return buf.read()

# ---------- HTTP клиент + «будилка» ----------
HTTP_TIMEOUT = httpx.Timeout(60.0, read=60.0, connect=15.0)
CLIENT = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

async def wake_ephemeris() -> None:
    """Разбудить Render-инстанс: несколько GET на /health и /docs."""
    urls = [f"{ASTRO_API}/health", f"{ASTRO_API}/docs"]
    for _ in range(6):
        for u in urls:
            try:
                r = await CLIENT.get(u)
                if r.status_code < 400:
                    return
            except Exception:
                pass
        await asyncio.sleep(3)

async def api_post(path: str, payload: dict) -> dict:
    """POST с повторами на 502/503/504, таймаут и сетевые ошибки."""
    url = f"{ASTRO_API}{path}"
    for attempt in range(6):
        try:
            r = await CLIENT.post(url, json=payload)
            if r.status_code in (502, 503, 504):
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            await asyncio.sleep(2 ** attempt)
    raise HTTPException(502, detail=f"Ephemeris API not responding: {url}")

# ---------- Обёртки к эпимерису ----------
async def resolve_place(city: str, country: str) -> dict:
    return await api_post("/api/resolve", {"city": city, "country": country})

async def get_chart(dt_local: str, lat: float, lon: float, iana_tz: str, house="Placidus") -> dict:
    return await api_post("/api/chart", {
        "datetime_local": dt_local, "lat": lat, "lon": lon, "iana_tz": iana_tz, "house_system": house
    })

async def get_horary(dt_local: str, lat: float, lon: float, iana_tz: str, house="Regiomontanus") -> dict:
    return await api_post("/api/horary", {
        "datetime_local": dt_local, "lat": lat, "lon": lon, "iana_tz": iana_tz, "house_system": house
    })

async def get_synastry(a: dict, b: dict) -> dict:
    return await api_post("/api/synastry", {"a": a, "b": b})

# ---------- Telegram ----------
bot = Bot(TOKEN)
dp  = Dispatcher()
app = FastAPI()

def parse_args(text: str) -> List[str]:
    parts = (text or "").split(maxsplit=1)
    if len(parts) < 2: return []
    return [x.strip() for x in parts[1].split(",")]

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет 🙂\n\n"
        "Доступные команды:\n"
        "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "/synastry две строки подряд A и B."
    )

@dp.message(Command("natal"))
async def cmd_natal(m: Message):
    args = parse_args(m.text)
    if len(args) < 4:
        await m.answer("Формат: /natal 17.08.2002, 15:20, Кострома, Россия")
        return
    date, time, city, country = args[0], args[1], args[2], ",".join(args[3:])
    dt = f"{date} {time}"
    try:
        await wake_ephemeris()
        place = await resolve_place(city, country)
        chart = await get_chart(dt, place["lat"], place["lon"], place["iana_tz"])
        blocks = []
        for sec in ["Общий портрет", "Стихии", "Психология", "Отношения", "Профессия", "Советы"]:
            blocks.append((sec, await gpt_interpret(sec, chart)))
        pdf = make_pdf("Натальная карта", blocks)  # объём добиваем внутр. логикой
        await bot.send_document(m.chat.id, document=("natal.pdf", pdf))
    except HTTPException as e:
        await m.answer("⚠️ Сервис эфемерид временно недоступен (502). Попробуй ещё раз через минуту.")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@dp.message(Command("horary"))
async def cmd_horary(m: Message):
    args = parse_args(m.text)
    if len(args) < 4:
        await m.answer("Формат: /horary 03.11.2025, 18:45, Москва, Россия")
        return
    date, time, city, country = args[0], args[1], args[2], ",".join(args[3:])
    dt = f"{date} {time}"
    try:
        await wake_ephemeris()
        place = await resolve_place(city, country)
        data = await get_horary(dt, place["lat"], place["lon"], place["iana_tz"])
        txt = await gpt_interpret("Хорарный вопрос", data) or "Краткий анализ выполнен."
        pdf = make_pdf("Хорар", [("Разбор", txt)])  # ~1 страница
        await bot.send_document(m.chat.id, document=("horary.pdf", pdf))
    except HTTPException:
        await m.answer("⚠️ Сервис эфемерид временно недоступен (502). Попробуй ещё раз через минуту.")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@dp.message(Command("synastry"))
async def cmd_synastry(m: Message):
    await m.answer("Отправь двумя сообщениями:\nA: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\nB: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")

# ---------- FastAPI ----------
@app.get("/health")
async def health(): return PlainTextResponse("ok")

@app.get("/setup")
async def setup():
    if not PUBLIC_URL:
        raise HTTPException(status_code=400, detail="PUBLIC_URL not set")
    url = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as cl:
        r = await cl.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook", params={"url": url})
        return JSONResponse(r.json())

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return JSONResponse({"ok": True})
