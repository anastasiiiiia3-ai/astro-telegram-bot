"""
Эзотерические астрологические расчёты
"""
import swisseph as swe
import math

SIGNS_RU = [
    "Овен", "Телец", "Близнецы", "Рак",
    "Лев", "Дева", "Весы", "Скорпион", 
    "Стрелец", "Козерог", "Водолей", "Рыбы"
]

def get_sign(lon: float) -> str:
    """Получить знак зодиака по долготе"""
    sign_num = int(lon / 30)
    return SIGNS_RU[sign_num]

def normalize_angle(angle: float) -> float:
    """Нормализовать угол к диапазону 0-360"""
    while angle < 0:
        angle += 360
    while angle >= 360:
        angle -= 360
    return angle

def calculate_esoteric_points(jd: float, lat: float, lon: float, asc: float, mc: float, 
                              sun_lon: float, moon_lon: float) -> dict:
    """
    Рассчитать эзотерические точки карты
    
    jd - Julian Day
    lat, lon - географические координаты
    asc - долгота асцендента
    mc - долгота MC
    sun_lon - долгота Солнца
    moon_lon - долгота Луны
    """
    
    # 1. ЭЛЕКТРИЧЕСКИЙ АСЦЕНДЕНТ (Эзотерический асцендент)
    # Формула: ASC + 90° (квадрат к асценденту)
    electric_asc = normalize_angle(asc + 90)
    
    # 2. МАГНИТНЫЙ АСЦЕНДЕНТ  
    # Формула: ASC - 90° (квадрат в другую сторону)
    magnetic_asc = normalize_angle(asc - 90)
    
    # 3. БЕЛАЯ ЛУНА (СЕЛЕНА)
    # Используем среднюю Селену
    # Апогей лунной орбиты (противоположность Лилит)
    # Вычисляем через среднюю Лилит и берём противоположную точку
    lilith_mean = swe.calc_ut(jd, swe.MEAN_APOG)[0][0]  # Средняя Лилит
    selena = normalize_angle(lilith_mean + 180)  # Белая Луна противоположна Чёрной
    
    # 4. ПАРС ФОРТУНЫ (Колесо Фортуны)
    # Формула для дневного рождения: ASC + Moon - Sun
    # Формула для ночного рождения: ASC + Sun - Moon
    # Упрощённо используем дневную формулу
    pars_fortuna = normalize_angle(asc + moon_lon - sun_lon)
    
    # 5. ПАРС ДУХА
    # Формула обратная Фортуне: ASC + Sun - Moon
    pars_spirit = normalize_angle(asc + sun_lon - moon_lon)
    
    # 6. СЕВЕРНЫЙ УЗЕЛ (Раху) - кармическое предназначение
    north_node = swe.calc_ut(jd, swe.TRUE_NODE)[0][0]
    
    # 7. ЮЖНЫЙ УЗЕЛ (Кету) - кармический опыт
    south_node = normalize_angle(north_node + 180)
    
    # 8. ФИКСИРОВАННЫЕ ЗВЁЗДЫ (берём самые важные)
    fixed_stars = get_important_fixed_stars(jd)
    
    return {
        "electric_ascendant": {
            "degree": round(electric_asc, 2),
            "sign": get_sign(electric_asc),
            "degree_in_sign": round(electric_asc % 30, 1)
        },
        "magnetic_ascendant": {
            "degree": round(magnetic_asc, 2),
            "sign": get_sign(magnetic_asc),
            "degree_in_sign": round(magnetic_asc % 30, 1)
        },
        "selena": {
            "degree": round(selena, 2),
            "sign": get_sign(selena),
            "degree_in_sign": round(selena % 30, 1)
        },
        "pars_fortuna": {
            "degree": round(pars_fortuna, 2),
            "sign": get_sign(pars_fortuna),
            "degree_in_sign": round(pars_fortuna % 30, 1)
        },
        "pars_spirit": {
            "degree": round(pars_spirit, 2),
            "sign": get_sign(pars_spirit),
            "degree_in_sign": round(pars_spirit % 30, 1)
        },
        "north_node": {
            "degree": round(north_node, 2),
            "sign": get_sign(north_node),
            "degree_in_sign": round(north_node % 30, 1)
        },
        "south_node": {
            "degree": round(south_node, 2),
            "sign": get_sign(south_node),
            "degree_in_sign": round(south_node % 30, 1)
        },
        "fixed_stars": fixed_stars
    }

def get_important_fixed_stars(jd: float) -> list:
    """
    Получить позиции важнейших фиксированных звёзд
    """
    # Список важных фиксированных звёзд с их координатами (примерные на 2000 год)
    stars = [
        {"name": "Регул (Сердце Льва)", "lon": 149.8, "meaning": "Королевская власть, успех, слава"},
        {"name": "Спика (Колос Девы)", "lon": 204.0, "meaning": "Творчество, таланты, удача"},
        {"name": "Антарес (Сердце Скорпиона)", "lon": 249.6, "meaning": "Страсть, трансформация"},
        {"name": "Альдебаран (Глаз Тельца)", "lon": 69.9, "meaning": "Целеустремлённость, сила"},
        {"name": "Сириус", "lon": 104.0, "meaning": "Духовное просветление"},
    ]
    
    # Добавляем прецессию (~0.014° в год с 2000 года)
    from datetime import datetime
    year_2000_jd = 2451545.0  # JD для 01.01.2000
    years_diff = (jd - year_2000_jd) / 365.25
    precession = years_diff * 0.014
    
    result = []
    for star in stars:
        current_lon = normalize_angle(star["lon"] + precession)
        result.append({
            "name": star["name"],
            "degree": round(current_lon, 1),
            "sign": get_sign(current_lon),
            "degree_in_sign": round(current_lon % 30, 1),
            "meaning": star["meaning"]
        })
    
    return result

def format_esoteric_data(esoteric: dict) -> str:
    """
    Форматировать эзотерические данные для GPT
    """
    text = "ЭЗОТЕРИЧЕСКИЕ ТОЧКИ:\n\n"
    
    text += f"⚡ Электрический Асцендент: {esoteric['electric_ascendant']['sign']} {esoteric['electric_ascendant']['degree_in_sign']}°\n"
    text += f"🧲 Магнитный Асцендент: {esoteric['magnetic_ascendant']['sign']} {esoteric['magnetic_ascendant']['degree_in_sign']}°\n\n"
    
    text += f"🤍 Белая Луна (Селена): {esoteric['selena']['sign']} {esoteric['selena']['degree_in_sign']}°\n\n"
    
    text += f"💎 Парс Фортуны: {esoteric['pars_fortuna']['sign']} {esoteric['pars_fortuna']['degree_in_sign']}°\n"
    text += f"✨ Парс Духа: {esoteric['pars_spirit']['sign']} {esoteric['pars_spirit']['degree_in_sign']}°\n\n"
    
    text += f"🌳 Северный Узел (предназначение): {esoteric['north_node']['sign']} {esoteric['north_node']['degree_in_sign']}°\n"
    text += f"🍂 Южный Узел (опыт): {esoteric['south_node']['sign']} {esoteric['south_node']['degree_in_sign']}°\n\n"
    
    text += "⭐ ФИКСИРОВАННЫЕ ЗВЁЗДЫ:\n"
    for star in esoteric['fixed_stars']:
        text += f"  • {star['name']}: {star['sign']} {star['degree_in_sign']}° — {star['meaning']}\n"
    
    return text
