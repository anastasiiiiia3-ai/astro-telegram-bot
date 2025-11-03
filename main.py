import os
import io
import json
import math
import asyncio
from typing import List, Dict, Any

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, Update
from dateutil import parser as dtparser

# PDF и шрифты
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib.units import cm

pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))

# ---------------- Настройки окружения ----------------
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
ASTRO_API = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set")

bot = Bot(TOKEN)
dp = Dispatcher()
app = FastAPI()
USE_GPT = bool(OPENAI_API_KEY)

if USE_GPT:
    from openai import OpenAI
    gpt_client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- Базовые PDF стили ----------------
def P(size=11): 
    return ParagraphStyle(name=f"P{size}", fontName="HYSMyeongJo-Medium", fontSize=size, leading=15, spaceAfter=6)
H1 = ParagraphStyle(name="H1", fontName="HYSMyeongJo-Medium", fontSize=18, leading=22, spaceAfter=10)
H2 = ParagraphStyle(name="H2", fontName="HYSMyeongJo-Medium", fontSize=14, leading=18, spaceAfter=8)

# ---------------- Сетевые запросы ----------------
SESSION = httpx.AsyncClient(timeout=40)

async def resolve_place(city: str, country: str) -> dict:
    r = await SESSION.post(f"{ASTRO_API}/api/resolve", json={"city": city, "country": country})
    r.raise_for_status()
    return r.json()

async def get_chart(datetime_local, lat, lon, iana_tz, house_system="Placidus"):
    r = await SESSION.post(f"{ASTRO_API}/api/chart", json={
        "datetime_local": datetime_local, "lat": lat, "lon": lon, "iana_tz": iana_tz, "house_system": house_system
    })
    r.raise_for_status()
    return r.json()

async def get_horary(datetime_local, lat, lon, iana_tz, house_system="Regiomontanus"):
    r = await SESSION.post(f"{ASTRO_API}/api/horary", json={
        "datetime_local": datetime_local, "lat": lat, "lon": lon, "iana_tz": iana_tz, "house_system": house_system
    })
    r.raise_for_status()
    return r.json()

async def get_synastry(a: dict, b: dict):
    r = await SESSION.post(f"{ASTRO_API}/api/synastry", json={"a": a, "b": b})
    r.raise_for_status()
    return r.json()

# ---------------- Вспомогательные функции ----------------
def _deg(x):
    x = (x + 360) % 360
    d = int(x)
    m = int((x - d) * 60)
    return f"{d}°{m:02d}"

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
def _sign(lon): return SIGNS[int(((lon % 360)//30)%12)]

def _table(header, rows, widths):
    data = [header] + rows
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle([
        ("FONTNAME",(0,0),(-1,-1),"HYSMyeongJo-Medium"),
        ("FONTSIZE",(0,0),(-1,-1),10),
        ("GRID",(0,0),(-1,-1),0.25,colors.grey),
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#f1f1f1")),
    ]))
    return t

# ---------------- GPT-интерпретация ----------------
SYSTEM_PROMPT = (
    "Ты астролог-консультант. Пиши по-русски, тёпло и поддерживающе, но конкретно и прагматично. "
    "Избегай эзотерических фраз, метафор и пафоса. Делай отчёты как для обычного человека: "
    "ясно, дружелюбно, с практическими выводами."
)

async def gpt_interpret(section: str, data: dict, model="gpt-4o-mini") -> str:
    if not USE_GPT:
        return ""
    msgs = [
        {"role":"system","content":SYSTEM_PROMPT},
        {"role":"user","content":f"Сделай подробную интерпретацию для раздела {section}. Дай 2–4 абзаца текста."},
        {"role":"user","content":json.dumps(data, ensure_ascii=False)},
    ]
    for i in range(3):
        try:
            resp = await asyncio.to_thread(
                gpt_client.chat.completions.create,
                model=model, messages=msgs, temperature=0.7
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            await asyncio.sleep(2**i)
    return ""

# ---------------- Создание PDF ----------------
def make_pdf(title: str, blocks: List[tuple]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    story = [Paragraph(title, H1), Spacer(1,8)]
    for head, text in blocks:
        if not text: continue
        story.append(Paragraph(head, H2))
        for p in text.split("\n"):
            p = p.strip()
            if p:
                story.append(Paragraph(p, P()))
        story.append(Spacer(1,6))
        if len(story) % 6 == 0:
            story.append(PageBreak())
    doc.build(story)
    buf.seek(0)
    return buf.read()

# ---------------- Telegram команды ----------------
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer("Привет 🌞\n\nДоступные команды:\n"
                   "/natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
                   "/horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
                   "/synastry две строки подряд A и B.")

def parse_args(text: str) -> List[str]:
    parts = text.split(maxsplit=1)
    if len(parts) < 2: return []
    return [x.strip() for x in parts[1].split(",")]

@dp.message(Command("natal"))
async def natal(m: Message):
    args = parse_args(m.text or "")
    if len(args) < 4:
        await m.answer("Формат: /natal 17.08.2002, 15:20, Кострома, Россия")
        return
    date, time, city, country = args[0], args[1], args[2], ",".join(args[3:])
    dt = f"{date} {time}"
    try:
        place = await resolve_place(city, country)
        chart = await get_chart(dt, place["lat"], place["lon"], place["iana_tz"])
        parts = []
        for sec in ["Общий портрет","Стихии","Психология","Отношения","Профессия","Советы"]:
            text = await gpt_interpret(sec, chart)
            parts.append((sec, text))
        pdf = make_pdf("Натальная карта", parts)
        await bot.send_document(m.chat.id, document=("natal.pdf", pdf))
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@dp.message(Command("horary"))
async def horary(m: Message):
    args = parse_args(m.text or "")
    if len(args) < 4:
        await m.answer("Формат: /horary 03.11.2025, 18:45, Москва, Россия")
        return
    date, time, city, country = args[0], args[1], args[2], ",".join(args[3:])
    dt = f"{date} {time}"
    try:
        place = await resolve_place(city, country)
        data = await get_horary(dt, place["lat"], place["lon"], place["iana_tz"])
        text = await gpt_interpret("Хорарный вопрос", data)
        pdf = make_pdf("Хорар", [("Разбор", text or "Краткий анализ выполнен.")])
        await bot.send_document(m.chat.id, document=("horary.pdf", pdf))
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

@dp.message(Command("synastry"))
async def synastry(m: Message):
    await m.answer("Отправь две строки подряд:\nA: 17.08.2002, 15:20, Кострома, Россия\nB: 04.07.1995, 10:40, Москва, Россия")

# ---------------- FastAPI endpoints ----------------
@app.get("/health")
async def health():
    return PlainTextResponse("ok")

@app.get("/setup")
async def setup():
    if not PUBLIC_URL:
        raise HTTPException(status_code=400, detail="PUBLIC_URL not set")
    url = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH
    async with httpx.AsyncClient() as cl:
        r = await cl.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook", params={"url": url})
        return JSONResponse(r.json())

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return JSONResponse({"ok": True})
