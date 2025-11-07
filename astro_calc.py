"""
Локальные астрологические расчёты без внешних API
"""
import swisseph as swe
from datetime import datetime
from timezonefinder import TimezoneFinder
from pytz import timezone
from geopy.geocoders import Nominatim

# Настройка Swiss Ephemeris
swe.set_ephe_path(None)  # Используем встроенные эфемериды

# Планеты
PLANETS = {
    'Sun': swe.SUN,
    'Moon': swe.MOON,
    'Mercury': swe.MERCURY,
    'Venus': swe.VENUS,
    'Mars': swe.MARS,
    'Jupiter': swe.JUPITER,
    'Saturn': swe.SATURN,
    'Uranus': swe.URANUS,
    'Neptune': swe.NEPTUNE,
    'Pluto': swe.PLUTO,
}

SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", 
    "Leo", "Virgo", "Libra", "Scorpio",
    "Sagittarius", "Capricorn", "Aquarius", "Pisces"
]

SIGNS_RU = [
    "Овен", "Телец", "Близнецы", "Рак",
    "Лев", "Дева", "Весы", "Скорпион", 
    "Стрелец", "Козерог", "Водолей", "Рыбы"
]

# Кеш для геокодинга
geocoder = Nominatim(user_agent="astro_bot")
tf = TimezoneFinder()

def get_location(city: str, country: str) -> tuple:
    """Получить координаты города"""
    try:
        location = geocoder.geocode(f"{city}, {country}", timeout=10)
        if location:
            lat, lon = location.latitude, location.longitude
            tz_name = tf.timezone_at(lat=lat, lng=lon)
            return lat, lon, tz_name
        else:
            raise ValueError(f"Город {city}, {country} не найден")
    except Exception as e:
        raise ValueError(f"Ошибка геокодинга: {str(e)}")

def parse_datetime(dt_str: str, tz_name: str) -> float:
    """Конвертировать datetime в Julian Day"""
    # dt_str формат: "2002-08-17T15:20"
    dt = datetime.fromisoformat(dt_str)
    tz = timezone(tz_name)
    dt_local = tz.localize(dt)
    dt_utc = dt_local.astimezone(timezone('UTC'))
    
    jd = swe.julday(
        dt_utc.year, dt_utc.month, dt_utc.day,
        dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    )
    return jd

def get_sign(lon: float) -> str:
    """Получить знак зодиака по долготе"""
    sign_num = int(lon / 30)
    return SIGNS_RU[sign_num]

def calculate_chart(dt_str: str, lat: float, lon: float, tz_name: str, house_system: str = "P") -> dict:
    """
    Рассчитать натальную карту
    house_system: P=Placidus, K=Koch, E=Equal, R=Regiomontanus
    """
    jd = parse_datetime(dt_str, tz_name)
    
    # Расчёт домов
    houses, ascmc = swe.houses(jd, lat, lon, house_system.encode())
    
    # Расчёт планет
    planets_data = []
    for name, planet_id in PLANETS.items():
        pos, ret = swe.calc_ut(jd, planet_id)
        lon = pos[0]
        speed = pos[3]
        
        planets_data.append({
            "name": name,
            "lon": round(lon, 4),
            "sign": get_sign(lon),
            "retro": speed < 0
        })
    
    result = {
        "datetime_local": dt_str,
        "iana_tz": tz_name,
        "lat": lat,
        "lon": lon,
        "asc": f"{get_sign(ascmc[0])} {round(ascmc[0] % 30, 2)}°",
        "mc": f"{get_sign(ascmc[1])} {round(ascmc[1] % 30, 2)}°",
        "planets": planets_data,
        "houses": [round(h, 2) for h in houses]
    }
    
    return result

def calculate_horary(dt_str: str, lat: float, lon: float, tz_name: str) -> dict:
    """Рассчитать хорарную карту (Regiomontanus)"""
    return calculate_chart(dt_str, lat, lon, tz_name, house_system="R")

def calculate_synastry(dt_a: str, lat_a: float, lon_a: float, tz_a: str,
                       dt_b: str, lat_b: float, lon_b: float, tz_b: str) -> dict:
    """Рассчитать синастрию"""
    chart_a = calculate_chart(dt_a, lat_a, lon_a, tz_a)
    chart_b = calculate_chart(dt_b, lat_b, lon_b, tz_b)
    
    return {
        "chart_a": chart_a,
        "chart_b": chart_b
    }

# Пример использования
if __name__ == "__main__":
    # Тест
    lat, lon, tz = get_location("Кострома", "Россия")
    chart = calculate_chart("2002-08-17T15:20", lat, lon, tz)
    print(f"ASC: {chart['asc']}")
    print(f"MC: {chart['mc']}")
    for p in chart['planets']:
        print(f"{p['name']}: {p['sign']} {round(p['lon'] % 30, 1)}°")
