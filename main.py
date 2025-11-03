import os
import asyncio
from typing import Dict, Any, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from timezonefinder import TimezoneFinder

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL     = os.getenv("PUBLIC_URL")             # https://your-bot.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "/webhook")
ASTRO_API      = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Env TELEGRAM_TOKEN is not set")
if not PUBLIC_URL:
    raise RuntimeError("Env PUBLIC_URL is not set")

bot = Bot(TELEGRAM_TOKEN, parse_mode="HTML")
dp  = Dispatcher()

# ---------- HTTP client with long timeout + retries ----------
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

# ---------- helpers ----------

class AstroError(Exception):
    pass

def warm_text() -> str:
    return "Приняла данные — запускаю точный расчёт. Это займёт несколько секунд…"

def err_text() -> str:
    return ("⚠️ Сервис эфемерид был недоступен на запросе. "
            "Я уже настроила повторы и попробую ещё раз через мгновение. "
            "Если не выйдет — пришлю понятное сообщение, а вы сможете повторить команду.")

def parse_one_line(s: str) -> Tuple[str, str, str, str]:
    # формат: "ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна"
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 4:
        raise ValueError("Ожидаю: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    date = parts[0]
    time = parts[1]
    city = parts[2]
    country = ",".join(parts[3:]).strip()
    return date, time, city, country

@retry(
    retry=retry_if_exception_type(AstroError),
    wait=wait_exponential(multiplier=0.8, min=1, max=6),
    stop=stop_after_attempt(4),
    reraise=True
)
async def call_astro(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{ASTRO_API}{path}"
    try:
        r = await client.post(url, json=payload)
    except httpx.RequestError as e:
        raise AstroError(f"network: {e}") from e
    if r.status_code >= 500:
        raise AstroError(f"server {r.status_code}")
    if r.status_code != 200:
        raise AstroError(f"http {r.status_code}: {r.text}")
    return r.json()

async def warmup_astro():
    try:
        await client.get(f"{ASTRO_API}/docs")
    except Exception:
        pass

# ---- fallback resolve (если /api/resolve вернул 5xx) ----
async def fallback_resolve(city: str, country: str) -> Tuple[float, float, str]:
    q = f"{city}, {country}"
    try:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "astro-bot/1.0"}
        )
        data = r.json()
        if not data:
            raise AstroError("geocode empty")
        lat = float(data[0]["lat"])
        lon = float(data[0]["lon"])
        tf = TimezoneFinder()
        tz = tf.timezone_at(lat=lat, lng=lon) or "UTC"
        return lat, lon, tz
    except Exception as e:
        raise AstroError(f"fallback resolve failed: {e}") from e

async def resolve_place(city: str, country: str) -> Tuple[float, float, str]:
    payload = {"city": city, "country": country}
    try:
        data = await call_astro("/api/resolve", payload)
        return float(data["lat"]), float(data["lon"]), str(data["iana_tz"])
    except Exception:
        # пробуем фолбэк
        return await fallback_resolve(city, country)

# -------- replies formatting --------

def fmt_ctrl_planets(chart: Dict[str, Any]) -> str:
    p = {pl["name"]: pl for pl in chart["planets"]}
    asc = chart["houses"]["ASC"]
    mc  = chart["houses"]["MC"]
    def one(name: str) -> str:
        d = p[name]
        return f"{name}: {d['lon']:.2f}° {d['sign']}"
    lines = [
        f"ASC: {asc['lon']:.2f}° {asc['sign']}",
        f"MC:  {mc['lon']:.2f}° {mc['sign']}",
        one("Sun"), one("Moon"), one("Mercury"), one("Venus"),
        one("Mars"), one("Jupiter"), one("Saturn"),
    ]
    return "\n".join(lines)

def fmt_ctrl_core(chart: Dict[str, Any]) -> str:
    p = {pl["name"]: pl for pl in chart["planets"]}
    asc = chart["houses"]["ASC"]
    mc  = chart["houses"]["MC"]
    lines = [
        f"ASC: {asc['lon']:.2f}° {asc['sign']}",
        f"MC:  {mc['lon']:.2f}° {mc['sign']}",
        f"☉: {p['Sun']['lon']:.2f}° {p['Sun']['sign']}",
        f"☽: {p['Moon']['lon']:.2f}° {p['Moon']['sign']}",
    ]
    return "\n".join(lines)

# ---------- commands ----------

@dp.message(CommandStart())
async def cmd_start(m: Message):
    text = (
        "Привет! Я астробот на точных эфемеридах.\n\n"
        "Команды:\n"
        "• <b>/natal</b> — ДД.ММ.ГГГГ, ЧЧ:ММ, Город,  Страна\n"
        "• <b>/horary</b> — ДД.ММ.ГГГГ, ЧЧ:ММ, Город,  Страна\n"
        "• <b>/synastry</b> — отправь две строки подряд после команды:\n"
        "  A: ДД.ММ.ГГГГ, ЧЧ:ММ, Город,  Страна\n"
        "  B: ДД.ММ.ГГГГ, ЧЧ:ММ, Город,  Страна"
    )
    await m.answer(text)

@dp.message(Command("natal"))
async def cmd_natal(m: Message):
    try:
        args = m.text.split(" ", 1)[1]
        date, time, city, country = parse_one_line(args)
    except Exception:
        return await m.reply("Формат: /natal ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    await m.answer(warm_text())
    try:
        lat, lon, tz = await resolve_place(city, country)
        payload = {
            "datetime_local": f"{date} {time}",
            "lat": lat, "lon": lon,
            "iana_tz": tz, "house_system": "Placidus"
        }
        data = await call_astro("/api/chart", payload)
        ctrl = fmt_ctrl_planets(data["chart"])
        # короткая человеческая часть (без воды)
        human = (
            "Картина характера — тёплая и практичная. "
            "Я отмечаю опорные точки (☉/☽/ASC/MC) и опишу это простым языком, "
            "без жаргона и метафор, чтобы было легко применить в жизни."
        )
        await m.answer(f"🔢 Контрольные цифры:\n{ctrl}\n\n📝 {human}")
    except Exception as e:
        await m.answer(f"⚠️ Экшен не вернул данные. {str(e)}\nПопробуй ещё раз через минуту.")

@dp.message(Command("horary"))
async def cmd_horary(m: Message):
    try:
        args = m.text.split(" ", 1)[1]
        date, time, city, country = parse_one_line(args)
    except Exception:
        return await m.reply("Формат: /horary ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    await m.answer(warm_text())
    try:
        lat, lon, tz = await resolve_place(city, country)
        payload = {
            "datetime_local": f"{date} {time}",
            "lat": lat, "lon": lon,
            "iana_tz": tz, "house_system": "Regiomontanus"
        }
        data = await call_astro("/api/horary", payload)
        chart = data["chart"]
        ctrl  = fmt_ctrl_core(chart)
        moon  = chart["moon"]
        voc   = "да" if moon.get("void_of_course") else "нет"
        ans   = data.get("answer", "При условии")
        brief = data.get("reason", "Ключ — ближайший применяющийся аспект Луны и рецепции сигнификаторов.")
        await m.answer(
            f"🔢 Контрольные цифры:\n{ctrl}\n\n"
            f"Луна (VOC): {voc}\n"
            f"Ответ: <b>{ans}</b>\nПричина: {brief}"
        )
    except Exception as e:
        await m.answer(f"⚠️ Экшен не вернул данные. {str(e)}")

_syn_buf: Dict[int, Dict[str, str]] = {}

@dp.message(Command("synastry"))
async def cmd_synastry(m: Message):
    _syn_buf[m.from_user.id] = {"step": "A"}
    await m.answer(
        "Ок! Пришли данные A в формате:\n"
        "ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна\n"
        "Потом — такие же данные B."
    )

@dp.message(F.text)
async def syn_steps(m: Message):
    buf = _syn_buf.get(m.from_user.id)
    if not buf:
        return  # обычные сообщения игнорим
    step = buf.get("step")
    try:
        date, time, city, country = parse_one_line(m.text)
    except Exception:
        return await m.reply("Формат: ДД.ММ.ГГГГ, ЧЧ:ММ, Город, Страна")
    await m.answer(warm_text())
    try:
        if step == "A":
            lat, lon, tz = await resolve_place(city, country)
            buf["A"] = {"datetime_local": f"{date} {time}", "lat": lat, "lon": lon, "iana_tz": tz, "house_system": "Placidus"}
            buf["step"] = "B"
            return await m.answer("Принято A ✅ Теперь пришли данные B тем же форматом.")
        elif step == "B":
            lat, lon, tz = await resolve_place(city, country)
            A = buf["A"]
            B = {"datetime_local": f"{date} {time}", "lat": lat, "lon": lon, "iana_tz": tz, "house_system": "Placidus"}
            data = await call_astro("/api/synastry", {"a": A, "b": B})
            aspects = data.get("top_aspects", [])[:10]
            if aspects:
                rows = ["ТОП-аспекты:"]
                for x in aspects:
                    rows.append(f"{x['a']} — {x['aspect']} — {x['b']} — орб {abs(x['orb']):.2f}°")
                tbl = "\n".join(rows)
            else:
                tbl = "ТОП-аспекты не найдены."
            notes = data.get("notes", [
                "Сильное взаимное притяжение по ключевым точкам.",
                "Есть зоны напряжения, которые можно превратить в рост при осознанном подходе.",
            ])
            _syn_buf.pop(m.from_user.id, None)
            await m.answer(f"{tbl}\n\nОбщая динамика:\n• " + "\n• ".join(notes))
    except Exception as e:
        _syn_buf.pop(m.from_user.id, None)
        await m.answer(f"⚠️ Экшен не вернул данные. {str(e)}")

# ---------- aiohttp app / webhook ----------

async def on_startup(app: web.Application):
    # пробуждаем astro-ephemeris
    await warmup_astro()
    await bot.set_webhook(f"{PUBLIC_URL}{WEBHOOK_PATH}", drop_pending_updates=True)

async def on_shutdown(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=True)
    await client.aclose()

def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    app.router.add_get("/setup", lambda _: web.json_response({"webhook": f"{PUBLIC_URL}{WEBHOOK_PATH}"}))
    return app

app = build_app()

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
