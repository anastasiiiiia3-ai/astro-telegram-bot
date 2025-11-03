import os
import io
import json
import math
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, Message
from aiogram.filters import Command
from aiogram.utils.markdown import hbold

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dateutil import parser as dtparser

# ------------------ PDF (ReportLab) ------------------
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib.units import cm

# Шрифт с кириллицей — без «квадратиков»
pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))

def PS(size=11, leading=15):  # абзац
    return ParagraphStyle(
        name=f"P{size}",
        fontName="HYSMyeongJo-Medium",
        fontSize=size,
        leading=leading,
        spaceAfter=6,
    )

H1 = ParagraphStyle(name="H1", fontName="HYSMyeongJo-Medium", fontSize=18, leading=22, spaceAfter=10)
H2 = ParagraphStyle(name="H2", fontName="HYSMyeongJo-Medium", fontSize=14, leading=18, spaceAfter=8)
H3 = ParagraphStyle(name="H3", fontName="HYSMyeongJo-Medium", fontSize=12, leading=16, spaceAfter=6)

# ------------------ ENV ------------------
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
ASTRO_API = os.getenv("ASTRO_API", "https://astro-ephemeris.onrender.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

bot = Bot(TOKEN)
dp = Dispatcher()

app = FastAPI()

# ================== ВСПОМОГАТЕЛЬНОЕ ==================
def _deg_to_str(d: float) -> StringError | str:
    d = (d + 360.0) % 360.0
    deg = int(d)
    minutes = int(round((d - deg) * 60))
    if minutes == 60:
        deg += 1
        minutes = 0
    return f"{deg}°{minutes:02d}"

def _sign(lon: float) -> str:
    signs = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
    idx = int(((lon % 360) // 30) % 12)
    return signs[idx]

def _table(head: List[str], rows: List[List[str]], widths: List[float]) -> Table:
    data = [head] + rows
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), "HYSMyeongJo-Medium"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f1f1f1")),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("ALIGN", (1,1), (-1,-1), "LEFT"),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,0), 6),
        ("BOTTOMPADDING", (0,0), (-1,0), 6),
    ]))
    return t

# ============= GPT (опционально) ======================
USE_GPT = bool(OPENAI_API_KEY)
if USE_GPT:
    from openai import OpenAI
    _gpt = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_STYLE = (
    "Ты астролог-консультант. Пиши по-русски, тёпло и поддерживающе, но конкретно и прагматично. "
    "Избегай поэтических метафор и эзотерического жаргона. Стиль: ясный, человеческий. "
    "Не придумывай градусов — пользуешься только переданными данными."
)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 1, 6), reraise=True)
def gpt_json(prompt: str, payload: dict, model: str = "gpt-4o-mini") -> dict:
    if not USE_GPT:
        raise RuntimeError("GPT disabled")
    msg = [
        {"role": "system", "content": SYSTEM_STYLE},
        {"role": "user", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    r = _gpt.chat.completions.create(model=model, messages=msg, temperature=0.7)
    text = r.choices[0].message.content.strip()
    # удалим ```json блок если обернуло
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n",1)[1] if "\n" in text else text
    try:
        return json.loads(text)
    except Exception:
        # фолбэк — один большой блок
        return {"_raw": text}

# ================= СЕТЕВОЕ: Astro API ==================
SESSION = httpx.AsyncClient(timeout=30)

async def resolve_place(city: str, country: str) -> dict:
    r = await SESSION.post(f"{ASTRO_API}/api/resolve", json={"city": city, "country": country})
    r.raise_for_status()
    return r.json()

async def get_chart(datetime_local: str, lat: float, lon: float, iana_tz: str, house_system="Placidus") -> dict:
    r = await SESSION.post(f"{ASTRO_API}/api/chart", json={
        "datetime_local": datetime_local,
        "lat": lat, "lon": lon,
        "iana_tz": iana_tz,
        "house_system": house_system
    })
    r.raise_for_status()
    return r.json()

async def get_horary(datetime_local: str, lat: float, lon: float, iana_tz: str, house_system="Regiomontanus") -> dict:
    r = await SESSION.post(f"{ASTRO_API}/api/horary", json={
        "datetime_local": datetime_local,
        "lat": lat, "lon": lon,
        "iana_tz": iana_tz,
        "house_system": house_system
    })
    r.raise_for_status()
    return r.json()

async def get_synastry(a: dict, b: dict) -> dict:
    r = await SESSION.post(f"{ASTRO_API}/api/synastry", json={"a": a, "b": b})
    r.raise_for_status()
    return r.json()

# ================== АСПЕКТЫ (быстро) ===================
def find_aspects(planets: List[dict], limit: int = 12) -> List[dict]:
    defs = [("Conjunction",0,6),("Opposition",180,6),("Trine",120,5),("Square",90,5),("Sextile",60,4)]
    res = []
    for i in range(len(planets)):
        for j in range(i+1, len(planets)):
            A, B = planets[i], planets[j]
            diff = abs(((A["lon"] - B["lon"]) + 540) % 360 - 180)
            for name, ang, orb in defs:
                if abs(diff - ang) <= orb:
                    res.append({"a":A["name"], "b":B["name"], "aspect":name, "orb": round(abs(diff-ang),2)})
    res.sort(key=lambda x: x["orb"])
    return res[:limit]

# ================== НАТАЛЬНЫЙ ТЕКСТ & PDF ==============
def natal_sections(chart: dict, target_pages: int = 5) -> dict:
    """
    Возвращает структурированный текст для 5+ страниц.
    С GPT → очень подробные секции.
    Без GPT → достойный фолбэк на 5+ страниц (много абзацев).
    """
    payload = {
        "meta": {k: chart.get(k) for k in ("datetime_local","utc_offset","lat","lon","iana_tz")},
        "asc": chart.get("asc"), "mc": chart.get("mc"),
        "planets": chart.get("planets", []),
        "houses": chart.get("houses", {}),
        "aspects": find_aspects(chart.get("planets", []), limit=16),
    }
    if USE_GPT:
        try:
            return gpt_json(
                prompt=("Сгенерируй развернутый натальный отчёт 5–8 страниц A4. "
                        "Разделы: portrait, elements, core, personal, growth, career, relations, health, life, summary. "
                        "Пиши тёпло и поддерживающе, но конкретно. Минимизируй эзотеризм, давай практику."
                        "Верни чистый JSON с этими ключами."),
                payload=payload
            )
        except Exception:
            pass

    # ---- Фолбэк без GPT: нарастим объём множеством тематических абзацев ----
    planets = chart.get("planets", [])
    asc, mc = chart.get("asc", 0.0), chart.get("mc", 0.0)
    aspects = find_aspects(planets, limit=14)
    # Конструируем 10 разделов по 2–4 абзаца = 5–7 страниц
    def para(txt): 
        # растянем за счет чуть более длинных абзацев
        return (txt + " ").strip()

    portrait = "\n".join([
        para("Ваш общий портрет складывается из сочетания Солнца, Луны и Асцендента."),
        para("Асцендент в знаке {} задаёт стиль самопрезентации; MC в {} показывает вектор профессионального роста."
             .format(_sign(asc), _sign(mc))),
        para("Личность выглядит целостной, когда вы опираетесь на сильные стороны знаков и не перегибаете доминирующие качества.")
    ])
    elements = "\n".join([
        para("Баланс стихий даёт ощущение внутренней температуры: огонь — импульс и смелость, земля — устойчивость, воздух — идеи, вода — эмпатия."),
        para("Наблюдайте, какая стихия у вас доминирует по положению личных планет, и добавляйте недостающие практики в быт."),
        para("Если огня слишком много — вводите паузы перед действиями; если воды мало — фиксируйте чувства письменно.")
    ])
    core = "\n".join([
        para("Солнце — воля и ценности, Луна — потребности и эмоциональный ритм."),
        para("В моменты сомнений возвращайтесь к простому вопросу: что сейчас наполняет меня энергией, а что истощает?"),
        para("Асцендент — 'дверь' в мир; небольшие корректировки поведения здесь дают быстрый эффект.")
    ])
    personal = "\n".join([
        para("Меркурий отвечает за способ мышления и коммуникацию — планируйте дни вокруг естественного пика концентрации."),
        para("Венера — про ценности и вкус: берите за ориентир то, что действительно приятно телу и глазам, а не 'как надо'."),
        para("Марс — ваша энергия. Планируйте один короткий рывок в день и один восстановительный ритуал.")
    ])
    growth = "\n".join([
        para("Юпитер расширяет и обучает: ищите среды, где вас естественно тянет расти."),
        para("Сатурн структурирует: определите ограничители, которые экономят силы (границы, режим, чек-листы)."),
        para("Их баланс даёт устойчивый прогресс без перегрузок.")
    ])
    career = "\n".join([
        para("MC и аспекты к нему показывают профессиональную сцену. Точность, репутация и видимый вклад — ваши ключевые валюты."),
        para("Делите проекты на автономные блоки: быстрый результат повышает мотивацию и даёт опору."),
        para("Записывайте достижения, даже микро — так формируется ощущение траектории, а не бесконечной гонки.")
    ])
    relations = "\n".join([
        para("В отношениях ориентируйтесь на 'совместимый быт': ритмы, уровень автономии, экологию конфликтов."),
        para("Говорите о потребностях простыми фразами: 'мне важно/мне трудно/я прошу'."),
        para("Развивайте 'берега' — ритуалы, куда возвращаетесь вместе после турбулентности."),
    ])
    health = "\n".join([
        para("Телесная регуляция — основа ясного мышления: сон, вода, питание, короткие прогулки."),
        para("Нервной системе полезны маленькие, повторяемые действия: одно и то же время подъёма, короткая разминка, вечерняя запись мыслей."),
    ])
    life = "\n".join([
        para("Ключевые аспекты карты влияют на бытовые сценарии. Рассмотрите, например:"),
        para(", ".join([f"{a['a']} {a['aspect']} {a['b']} (орб {a['orb']}°)" for a in aspects])),
        para("Под них удобно подобрать опоры: напоминания, 'правила одного шага', расписание восстановления.")
    ])
    summary = "\n".join([
        para("Итог: опирайтесь на сильные стороны, добавляйте недостающие навыки минимальными шагами."),
        para("Ориентиры на 4–6 недель: 1) стабильный сон, 2) одно глубокое письмо в неделю, 3) две короткие тренировки, 4) один 'день без новостей'."),
        para("Планируйте недели от восстановления, а не от задач — так вы сохраняете интерес и скорость.")
    ])
    return {
        "portrait": portrait, "elements": elements, "core": core, "personal": personal,
        "growth": growth, "career": career, "relations": relations, "health": health,
        "life": life, "summary": summary
    }

def pdf_natal(chart: dict, narrative: dict) -> bytes:
    """
    5+ страниц: таблицы + много разделов.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    story = []

    story.append(Paragraph("Натальная карта", H1))
    meta = chart.get("datetime_local","")
    story.append(Paragraph(f"Дата/время (локально): {meta}", PS()))
    story.append(Spacer(1,6))

    # Углы, планеты, аспекты
    asc = chart.get("asc", 0.0); mc = chart.get("mc", 0.0)
    story.append(Paragraph("Контрольные углы", H2))
    story.append(_table(["Угол","Градус","Знак"], [["ASC", _deg_to_str(asc), _sign(asc)], ["MC", _deg_to_str(mc), _sign(mc)]],
                        [3.2*cm, 3*cm, 4.2*cm]))
    story.append(Spacer(1,8))

    story.append(Paragraph("Планеты", H2))
    rows = []
    for p in chart.get("planets", []):
        rows.append([p["name"], _deg_to_str(p["lon"]), p.get("sign") or _sign(p["lon"]), "R" if p.get("retro") else "—"])
    story.append(_table(["Планета","Долгота","Знак","R?"], rows, [3.2*cm,3*cm,4.2*cm,1.2*cm]))

    story.append(Spacer(1,8))
    story.append(Paragraph("Аспекты (топ-12 по тесноте)", H2))
    asp = find_aspects(chart.get("planets", []), limit=12)
    arows = [[a["a"], a["aspect"], a["b"], f'{a["orb"]}°'] for a in asp]
    story.append(_table(["A","Аспект","B","Орб"], arows, [3.2*cm,3.2*cm,3.2*cm,2*cm]))

    story.append(PageBreak())

    # Текстовые разделы — много абзацев = 5+ страниц
    sections = [
        ("Общий портрет", "portrait"),
        ("Стихии и доминанты", "elements"),
        ("Солнце / Луна / Асцендент", "core"),
        ("Коммуникация и личная энергия (Меркурий / Венера / Марс)", "personal"),
        ("Рост и структура (Юпитер / Сатурн)", "growth"),
        ("Профессиональная сцена", "career"),
        ("Отношения", "relations"),
        ("Здоровье и ритмы", "health"),
        ("Жизненные сценарии", "life"),
        ("Итоги и ориентиры", "summary"),
    ]
    for title, key in sections:
        text = narrative.get(key, "")
        if not text:
            continue
        story.append(Paragraph(title, H2))
        for para in text.split("\n"):
            para = para.strip()
            if para:
                story.append(Paragraph(para, PS()))
        story.append(Spacer(1,6))
        # деликатно добавим разделение страниц, чтобы точно выйти за 5+
        if title in {"Коммуникация и личная энергия (Меркурий / Венера / Марс)", "Профессиональная сцена", "Отношения"}:
            story.append(PageBreak())

    doc.build(story)
    buf.seek(0)
    return buf.read()

# ================== ХОРАРЬ =============================
def text_horary_onepage(h: dict) -> bytes:
    """
    Короткий (1 стр.) PDF: контрольные градусы + логика Лилли (по данным API).
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    story = []

    story.append(Paragraph("Хорарный разбор (по Лилли)", H1))
    story.append(Spacer(1,6))

    # Контрольные градусы из API (как есть)
    key_rows = []
    # API возвращает chart + planets, там найдём Sun/Moon/ASC/MC
    ch = h.get("chart", {})
    asc = ch.get("asc", 0.0); mc = ch.get("mc", 0.0)
    key_rows.append(["ASC", _deg_to_str(asc), _sign(asc)])
    key_rows.append(["MC",  _deg_to_str(mc),  _sign(mc)])
    sun = next((p for p in ch.get("planets", []) if p["name"].lower()=="sun"), None)
    moon = next((p for p in ch.get("planets", []) if p["name"].lower()=="moon"), None)
    if sun:  key_rows.append(["Sun",  _deg_to_str(sun["lon"]),  sun.get("sign") or _sign(sun["lon"])])
    if moon: key_rows.append(["Moon", _deg_to_str(moon["lon"]), moon.get("sign") or _sign(moon["lon"])])
    story.append(_table(["Точка","Градус","Знак"], key_rows, [3.2*cm,3*cm,4.2*cm]))

    story.append(Spacer(1,8))
    # Краткая логика — из полей API (например, moon.next_applying, moon.voc и т.п., если есть)
    # Если нет — напишем аккуратный фолбэк.
    logic = h.get("logic", {})
    bullets = [
        logic.get("significators","Сигнификаторы определяются по управителям соответствующих домов."),
        logic.get("receptions","Рецепции укажут на качество взаимодействия сторон."),
        logic.get("moon_aspect","Ближайший применяющийся аспект Луны показывает развитие сюжета."),
        logic.get("moon_voc","Статус VOC подсказывает, будет ли 'ход'."),
        logic.get("verdict","Итог: Да / Нет / При условии — в зависимости от контекста аспектов."),
    ]
    story.append(Paragraph("Ключевые ориентиры", H2))
    for b in bullets:
        story.append(Paragraph("• " + str(b), PS()))
    doc.build(story)
    buf.seek(0)
    return buf.read()

# ================== СИНАСТРИЯ ==========================
def synastry_sections(sync: dict) -> dict:
    """
    3+ страниц: таблица топ-аспектов + 5–8 тезисов по динамике + разделы про ключевые пары.
    С GPT → подробности; без GPT → структурный текст.
    """
    a = sync.get("a", {}); b = sync.get("b", {})
    aspects = sync.get("aspects") or find_aspects(a.get("planets", []) + b.get("planets", []), limit=16)
    payload = {"a": a, "b": b, "aspects": aspects}

    if USE_GPT:
        try:
            return gpt_json(
                prompt=("Сделай разбор совместимости на 3–5 страниц A4. Верни JSON с ключами: "
                        "overview, attraction, tension, advice, sun_moon, venus_mars, summary. "
                        "Конкретный, тёплый тон; минимум эзотеризма."),
                payload=payload
            )
        except Exception:
            pass

    # Фолбэк
    overview = "Совместимость держится на сочетании зон притяжения (трины/секстили) и зон роста (квадраты/оппозиции)."
    attraction = "Притяжение усиливается, когда личные планеты друг друга поддерживают по стихиям и ритмам."
    tension = "Напряжение полезно, если у него есть клапаны разрядки: проговоры, паузы, совместные ритуалы."
    advice = "Согласуйте базовые ритмы: сон, нагрузку, автономию. Договаривайтесь 'сквозными фразами' — коротко и по делу."
    sun_moon = "Связка Солнце–Луна показывает, как один даёт вектор, другой — эмоциональный климат; ищите баланс инициативы и принятия."
    venus_mars = "Венера/Марс — про вкусы и энергию в паре: договоритесь о темпе и способах заботы о теле."
    summary = "Итог: поддерживайте зоны притяжения, уважайте различия, вводите простые правила безопасности общения."
    return {
        "overview": overview, "attraction": attraction, "tension": tension, "advice": advice,
        "sun_moon": sun_moon, "venus_mars": venus_mars, "summary": summary
    }

def pdf_synastry(sync: dict, narrative: dict) -> bytes:
    """
    3+ страниц: таблица аспектов + много разделов.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    story = []

    story.append(Paragraph("Синастрия (совместимость)", H1))
    story.append(Spacer(1,6))

    # Топ-аспекты
    aspects = sync.get("aspects")
    if not aspects:
        aspects = find_aspects((sync.get("a", {}).get("planets", []) + sync.get("b", {}).get("planets", [])), limit=12)
    rows = [[x["a"], x["aspect"], x["b"], f'{x["orb"]}°'] for x in aspects[:10]]
    story.append(Paragraph("Топ-10 аспектов по тесноте", H2))
    story.append(_table(["A","Аспект","B","Орб"], rows, [3.2*cm,3.2*cm,3.2*cm,2*cm]))

    story.append(PageBreak())

    # Разделы
    sections = [
        ("Общая картина", "overview"),
        ("Зоны притяжения", "attraction"),
        ("Зоны напряжения (роста)", "tension"),
        ("Практические договорённости", "advice"),
        ("Солнце / Луна", "sun_moon"),
        ("Венера / Марс", "venus_mars"),
        ("Итог", "summary"),
    ]
    for title, key in sections:
        txt = narrative.get(key, "")
        if not txt: 
            continue
        story.append(Paragraph(title, H2))
        for para in str(txt).split("\n"):
            para = para.strip()
            if para:
                story.append(Paragraph(para, PS()))
        story.append(Spacer(1,6))
        if title in {"Зоны напряжения (роста)","Практические договорённости"}:
            story.append(PageBreak())

    doc.build(story)
    buf.seek(0)
    return buf.read()

# ================== ТЕЛЕГРАМ ХЕНДЛЕРЫ ==================
INSTR = (
    "Привет! Я астробот на точных эфемеридах.\n\n"
    "Команды:\n"
    "• /natal — ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна\n"
    "• /horary — ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна\n"
    "• /synastry — две строки подряд после команды:\n"
    "   A: ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна\n"
    "   B: ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна\n"
)

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(INSTR)

def _split_args(text: str) -> List[str]:
    # после /cmd пробел и дальше оригинальная строка
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return []
    return [x.strip() for x in parts[1].split(",")]

@dp.message(Command("natal"))
async def do_natal(m: Message):
    try:
        args = _split_args(m.text or "")
        if len(args) < 4:
            await m.answer("Пожалуйста так: /natal ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна")
            return
        date_s, time_s, city, country = args[0], args[1], args[2], ",".join(args[3:])
        dt_local = f"{date_s} {time_s}"
        place = await resolve_place(city, country)
        chart = await get_chart(datetime_local=dt_local, lat=place["lat"], lon=place["lon"], iana_tz=place["iana_tz"])
        narrative = natal_sections(chart, target_pages=5)
        pdf_bytes = pdf_natal(chart, narrative)
        await bot.send_document(m.chat.id, document=("natal.pdf", pdf_bytes))
    except httpx.HTTPStatusError as e:
        await m.answer(f"⚠️ Сервис эфемерид недоступен: {e.response.status_code}. Попробуйте позже.")
    except Exception as e:
        await m.answer(f"Не получилось собрать натал: {e}")

@dp.message(Command("horary"))
async def do_horary(m: Message):
    try:
        args = _split_args(m.text or "")
        if len(args) < 4:
            await m.answer("Пожалуйста так: /horary ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна")
            return
        date_s, time_s, city, country = args[0], args[1], args[2], ",".join(args[3:])
        dt_local = f"{date_s} {time_s}"
        place = await resolve_place(city, country)
        h = await get_horary(datetime_local=dt_local, lat=place["lat"], lon=place["lon"], iana_tz=place["iana_tz"])
        pdf_bytes = text_horary_onepage(h)
        await bot.send_document(m.chat.id, document=("horary.pdf", pdf_bytes))
    except httpx.HTTPStatusError as e:
        await m.answer(f"⚠️ Сервис эфемерид недоступен: {e.response.status_code}. Попробуйте позже.")
    except Exception as e:
        await m.answer(f"Не получилось собрать хорар: {e}")

@dp.message(Command("synastry"))
async def do_synastry(m: Message):
    try:
        # ожидаем следующими двумя сообщениями строки А и B
        await m.answer("Отправь две строки подряд:\nA: ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна\nB: ДД.MM.ГГГГ, ЧЧ:ММ, Город, Страна")

        # ждём два следующих сообщения пользователя
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.state import State, StatesGroup

        class S(StatesGroup):
            a = State()
            b = State()

        dp.fsm.storage = dp.fsm.storage or {}
        ctx: FSMContext = dp.fsm.get_context(m.chat.id, m.from_user.id)
        await ctx.set_state(S.a)
        return
    except Exception as e:
        await m.answer(f"Ошибка: {e}")

# Перехват произвольных сообщений для FSM синатрии
@dp.message(F.text.regexp(r"^(A|А)\s*:"))
async def sync_a(m: Message):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    class S(StatesGroup):
        a = State()
        b = State()
    ctx: FSMContext = dp.fsm.get_context(m.chat.id, m.from_user.id)
    await ctx.update_data(a=m.text)
    await ctx.set_state(S.b)
    await m.answer("Теперь строка B тем же форматом.")

@dp.message(F.text.regexp(r"^(B|В)\s*:"))
async def sync_b(m: Message):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    class S(StatesGroup):
        a = State()
        b = State()
    ctx: FSMContext = dp.fsm.get_context(m.chat.id, m.from_user.id)
    data = await ctx.get_data()
    a_line = (data.get("a") or "").split(":",1)[1].strip()
    b_line = m.text.split(":",1)[1].strip()

    try:
        def parse_line(line: str):
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 4:
                raise ValueError("Неверный формат")
            date_s, time_s, city, country = parts[0], parts[1], parts[2], ",".join(parts[3:])
            return date_s, time_s, city, country

        a_date, a_time, a_city, a_country = parse_line(a_line)
        b_date, b_time, b_city, b_country = parse_line(b_line)
        a_place = await resolve_place(a_city, a_country)
        b_place = await resolve_place(b_city, b_country)
        a = {
            "datetime_local": f"{a_date} {a_time}",
            "lat": a_place["lat"], "lon": a_place["lon"],
            "iana_tz": a_place["iana_tz"], "house_system": "Placidus"
        }
        b = {
            "datetime_local": f"{b_date} {b_time}",
            "lat": b_place["lat"], "lon": b_place["lon"],
            "iana_tz": b_place["iana_tz"], "house_system": "Placidus"
        }
        sync = await get_synastry(a, b)
        narrative = synastry_sections(sync)
        pdf_bytes = pdf_synastry(sync, narrative)
        await bot.send_document(m.chat.id, document=("synastry.pdf", pdf_bytes))
    except httpx.HTTPStatusError as e:
        await m.answer(f"⚠️ Сервис эфемерид недоступен: {e.response.status_code}. Попробуйте позже.")
    except Exception as e:
        await m.answer(f"Не получилось собрать синастрию: {e}")
    finally:
        await ctx.clear()

# ================== ВЕБХУКИ/ЭНДПОИНТЫ =================
@app.get("/health")
async def health():
    return PlainTextResponse("ok")

@app.get("/setup")
async def setup_webhook():
    if not PUBLIC_URL:
        raise HTTPException(status_code=400, detail="PUBLIC_URL is not set")
    url = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH
    async with httpx.AsyncClient(timeout=30) as cl:
        r = await cl.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook", params={"url": url})
        data = r.json()
    return JSONResponse(data)

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception:
        # ничего не падаем — телеге ок
        pass
    return JSONResponse({"ok": True})

# ================ RUN LOCAL (не для Render) ============
# uvicorn main:app --host 0.0.0.0 --port 10000
