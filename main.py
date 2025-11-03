import os
import json
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, Message
from aiogram.filters import CommandStart, Command

# ============ конфиг ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PUBLIC_URL     = os.environ.get("PUBLIC_URL")          # https://...onrender.com
ASTRO_API      = os.environ.get("ASTRO_API", "https://astro-ephemeris.onrender.com")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

app = FastAPI()


# ---------- утилиты ----------
async def astro_call(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Вызов твоего astro-ephemeris API с таймаутами и понятными ошибками."""
    url = f"{ASTRO_API}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()

def fmt_err(msg: str) -> str:
    return f"⚠️ {msg}\nПопробуйте ещё раз через минуту."

# ---------- OpenAI: интерпретации ----------
def openai_interpret(kind: str, data: Dict[str, Any]) -> str:
    """
    kind: 'natal' | 'horary' | 'synastry'
    data: JSON от astro-ephemeris
    Пишем тёплым, поддерживающим, но конкретным тоном.
    """
    if not OPENAI_API_KEY:
        # Если ключ не задан — даём сухой вывод без интерпретации
        return "ℹ️ Интерпретация отключена (нет OPENAI_API_KEY). Контрольные данные показаны выше."

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    system = (
        "Ты астролог-интерпретатор. Тон — тёплый, поддерживающий, без поэзии и мистики. "
        "Пиши конкретно, для обычного читателя. Кратко (5–10 предложений). "
        "Никаких придуманных градусов: опирайся только на JSON, который я даю."
    )

    user = f"""
Вид чтения: {kind}
JSON данных от эфемерид (используй только это, ничего не выдумывай):
{json.dumps(data, ensure_ascii=False, indent=2)}

Сформируй понятный вывод:
- для natаl: краткая характеристика, 2–3 сильные стороны, 1–2 зоны роста;
- для horary: логика по Лилли (сигнификаторы, рецепции, ближайший применяющийся аспект Луны, статус VOC), итог Да/Нет/При условии;
- для synastry: 5–8 тезисов о динамике пары (притяжение, напряжения, что помогает).
Стиль: человеческий, без жаргона и эзотерики.
"""

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        temperature=0.7,
        max_output_tokens=600,
    )
    return resp.output_text.strip()


# ---------- парсинг ввода ----------
def parse_single(text: str) -> Optional[Dict[str, str]]:
    # формат: "ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
    try:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 4:
            return None
        date = parts[0]
        time = parts[1]
        city = parts[2]
        country = ",".join(parts[3:]).strip()
        return {"date": date, "time": time, "city": city, "country": country}
    except Exception:
        return None

def to_chart_payload(parsed: Dict[str, str], house_system: str) -> Dict[str, Any]:
    # сначала резолвим город → lat/lon/iana_tz
    return {
        "resolve": {"city": parsed["city"], "country": parsed["country"]},
        "datetime_local": f"{parsed['date']} {parsed['time']}",
        "house_system": house_system
    }


# ---------- handlers ----------
@dp.message(CommandStart())
async def start(m: Message):
    txt = (
        "Привет! Я астробот на точных эфемеридах.\n\n"
        "Команды:\n"
        "• /natal — ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "• /horary — ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "• /synastry — две строки подряд после команды:\n"
        "  A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "  B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
    )
    await m.answer(txt)

@dp.message(Command("natal"))
async def natal(m: Message):
    payload = parse_single(m.text.replace("/natal", "", 1).strip())
    if not payload:
        return await m.answer("Пожалуйста так:\n/natal 17.08.2002, 15:20, Кострома, Россия")
    try:
        # 1) resolve
        res = await astro_call("/api/resolve", {"city": payload["city"], "country": payload["country"]})
        # 2) chart (Placidus)
        chart_req = {
            "datetime_local": f"{payload['date']}T{payload['time']}",
            "lat": res["lat"], "lon": res["lon"], "iana_tz": res["iana_tz"],
            "house_system": "Placidus"
        }
        chart = await astro_call("/api/chart", chart_req)

        # контрольные цифры (ASC/MC/планеты) из API
        lines = []
        if "houses" in chart and chart["houses"].get("ASC") and chart["houses"].get("MC"):
            lines.append(f"ASC: {chart['houses']['ASC']}")
            lines.append(f"MC: {chart['houses']['MC']}")
        for p in chart.get("planets", []):
            if p.get("name") in ["Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn"]:
                lines.append(f"{p['name']}: {round(p['lon'],5)}° {p.get('sign','')}")

        header = "📌 Контрольные данные (из эфемерид):\n" + "\n".join(lines)
        interp = openai_interpret("natal", chart)
        await m.answer(f"{header}\n\n{interp}")

    except httpx.HTTPError as e:
        await m.answer(fmt_err(f"Сервис эфемерид недоступен ({e})"))

@dp.message(Command("horary"))
async def horary(m: Message):
    payload = parse_single(m.text.replace("/horary", "", 1).strip())
    if not payload:
        return await m.answer("Пожалуйста так:\n/horary 04.07.2025, 17:00, Москва, Россия")
    try:
        res = await astro_call("/api/resolve", {"city": payload["city"], "country": payload["country"]})
        req = {
            "datetime_local": f"{payload['date']}T{payload['time']}",
            "lat": res["lat"], "lon": res["lon"], "iana_tz": res["iana_tz"],
            "house_system": "Regiomontanus"
        }
        h = await astro_call("/api/horary", req)

        lines = []
        if "houses" in h and h["houses"].get("ASC") and h["houses"].get("MC"):
            lines.append(f"ASC: {h['houses']['ASC']}")
            lines.append(f"MC: {h['houses']['MC']}")
        for p in h.get("planets", []):
            if p.get("name") in ["Sun","Moon"]:
                lines.append(f"{p['name']}: {round(p['lon'],5)}° {p.get('sign','')}")

        header = "📌 Контрольные данные (из эфемерид):\n" + "\n".join(lines)
        interp = openai_interpret("horary", h)
        await m.answer(f"{header}\n\n{interp}")

    except httpx.HTTPError as e:
        await m.answer(fmt_err(f"Сервис эфемерид недоступен ({e})"))

@dp.message(Command("synastry"))
async def synastry(m: Message):
    # ждём формат из двух строк: A: ... / B: ...
    text = m.text.replace("/synastry", "", 1).strip()
    parts = [s.strip() for s in text.split("\n") if s.strip()]
    if len(parts) < 2 or not parts[0].startswith("A:") or not parts[1].startswith("B:"):
        return await m.answer("Формат:\n/synastry\nA: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\nB: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")

    pa = parse_single(parts[0].replace("A:", "", 1).strip())
    pb = parse_single(parts[1].replace("B:", "", 1).strip())
    if not pa or not pb:
        return await m.answer("Не смог разобрать даты. Проверь формат, как в подсказке выше.")

    try:
        ra = await astro_call("/api/resolve", {"city": pa["city"], "country": pa["country"]})
        rb = await astro_call("/api/resolve", {"city": pb["city"], "country": pb["country"]})

        a = {"datetime_local": f"{pa['date']}T{pa['time']}",
             "lat": ra["lat"], "lon": ra["lon"], "iana_tz": ra["iana_tz"], "house_system": "Placidus"}
        b = {"datetime_local": f"{pb['date']}T{pb['time']}",
             "lat": rb["lat"], "lon": rb["lon"], "iana_tz": rb["iana_tz"], "house_system": "Placidus"}

        syn = await astro_call("/api/synastry", {"a": a, "b": b})

        # ТОП-аспекты (если backend вернёт) — иначе только интерпретация
        aspects_txt = ""
        if syn.get("top_aspects"):
            rows = []
            for asp in syn["top_aspects"][:10]:
                rows.append(f"{asp['a']} — {asp['aspect']} — {asp['b']} — орб {asp['orb']}°")
            aspects_txt = "🧭 Топ аспектов:\n" + "\n".join(rows) + "\n\n"

        interp = openai_interpret("synastry", syn)
        await m.answer(f"{aspects_txt}{interp}")

    except httpx.HTTPError as e:
        await m.answer(fmt_err(f"Сервис эфемерид недоступен ({e})"))


# ---------- FastAPI endpoints ----------
@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/")
async def telegram_webhook(request: Request):
    data = await request.json()
    await dp.feed_update(bot, Update.model_validate(data))
    return JSONResponse({"ok": True})

@app.get("/setup")
async def setup():
    if not PUBLIC_URL:
        raise HTTPException(400, "PUBLIC_URL is not set")
    url = f"{PUBLIC_URL}/"
    await bot.set_webhook(url)
    return {"detail": f"Webhook set to {url}"}
