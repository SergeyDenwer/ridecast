#!/usr/bin/env python3
"""
RideWeather — Telegram бот для прогноза погоды на маршруте.
Загрузи GPX, укажи дату/время старта и скорость — получи карту ветра и графики.

Использование:
  export TELEGRAM_BOT_TOKEN="твой_токен"
  python rideweather_bot.py
"""

import asyncio
import os
import io
import logging
import math
import tempfile
from datetime import datetime, timedelta, timezone

import gpxpy
import requests
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Состояния диалога ────────────────────────────────────────────────────────
WAITING_GPX      = 0
WAITING_DATE     = 1
WAITING_TIME     = 2
WAITING_SPEED    = 3   # скорость ИЛИ время финиша


# ─── GPX парсинг ─────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def parse_gpx(path):
    """Возвращает список (lat, lon, elev_m)."""
    with open(path, 'r') as f:
        gpx = gpxpy.parse(f)
    pts = []
    for track in gpx.tracks:
        for seg in track.segments:
            for p in seg.points:
                pts.append((p.latitude, p.longitude, p.elevation or 0))
    if not pts:
        for route in gpx.routes:
            for p in route.points:
                pts.append((p.latitude, p.longitude, p.elevation or 0))
    return pts


def sample_points(pts, n=12):
    """Равномерно выбираем n точек вдоль трека, возвращаем (lat, lon, elev, dist_km, time_frac)."""
    if len(pts) < 2:
        return [(pts[0][0], pts[0][1], pts[0][2], 0.0, 0.0)] if pts else []

    # Накапливаем дистанцию
    cumdist = [0.0]
    for i in range(1, len(pts)):
        cumdist.append(cumdist[-1] + haversine_km(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1]))
    total = cumdist[-1]

    result = []
    targets = [total * i / (n - 1) for i in range(n)]
    j = 0
    for t in targets:
        while j < len(cumdist) - 1 and cumdist[j + 1] < t:
            j += 1
        result.append((pts[j][0], pts[j][1], pts[j][2], cumdist[j], cumdist[j] / total))
    return result, total


# ─── Open-Meteo погода ────────────────────────────────────────────────────────

def fetch_hourly_forecast(lat, lon):
    """Получаем почасовой прогноз на 16 дней."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join([
            "temperature_2m",
            "precipitation_probability",
            "precipitation",
            "cloud_cover",
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m",
        ]),
        "forecast_days": 16,
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_weather_at_time(forecast, dt: datetime):
    """Интерполируем погоду в нужный момент времени."""
    times = forecast["hourly"]["time"]
    dt_str = dt.strftime("%Y-%m-%dT%H:%M")

    # Находим ближайший час
    best_i = 0
    best_diff = abs((datetime.fromisoformat(times[0]) - dt).total_seconds())
    for i, t in enumerate(times):
        diff = abs((datetime.fromisoformat(t) - dt).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best_i = i

    h = forecast["hourly"]
    return {
        "time": dt,
        "temp":      h["temperature_2m"][best_i],
        "precip_prob": h["precipitation_probability"][best_i],
        "precip":    h["precipitation"][best_i],
        "clouds":    h["cloud_cover"][best_i],
        "wind_spd":  h["wind_speed_10m"][best_i],
        "wind_dir":  h["wind_direction_10m"][best_i],
        "wind_gust": h["wind_gusts_10m"][best_i],
    }


def get_route_weather(pts_sampled, start_dt: datetime, duration_h: float):
    """
    Для каждой точки маршрута вычисляем время прохождения и берём прогноз.
    pts_sampled — [(lat, lon, elev, dist_km, frac), ...]
    """
    # Используем центральную точку для запроса (можно улучшить позже)
    mid = pts_sampled[len(pts_sampled) // 2]
    forecast = fetch_hourly_forecast(mid[0], mid[1])

    # Также получаем прогноз для начала и конца для графиков
    first_forecast = fetch_hourly_forecast(pts_sampled[0][0], pts_sampled[0][1])

    result = []
    for lat, lon, elev, dist_km, frac in pts_sampled:
        point_dt = start_dt + timedelta(hours=frac * duration_h)
        w = get_weather_at_time(forecast, point_dt)
        w["lat"] = lat
        w["lon"] = lon
        w["elev"] = elev
        w["dist_km"] = dist_km
        result.append(w)

    # Почасовой ряд для графиков (от старта до финиша каждый час)
    end_dt = start_dt + timedelta(hours=duration_h)
    hourly_series = []
    dt = start_dt
    step = timedelta(hours=max(1, int(duration_h / 8)))
    while dt <= end_dt + timedelta(minutes=30):
        w = get_weather_at_time(first_forecast, dt)
        hourly_series.append(w)
        dt += step

    return result, hourly_series, forecast


# ─── Рендер карточки ─────────────────────────────────────────────────────────

def render_card(pts_weather, hourly_series, route_name, start_dt, end_dt, total_km):
    """Рисуем итоговую карточку через Cairo."""
    try:
        import cairo
    except ImportError:
        return _render_fallback(pts_weather, hourly_series, route_name, start_dt, end_dt, total_km)

    # ── Размеры ──────────────────────────────────────────────────────────────
    W, H = 900, 700
    PAD  = 40
    CHART_H = 180
    MAP_H   = 300
    MAP_W   = 420

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx  = cairo.Context(surf)

    # ── Цвета ────────────────────────────────────────────────────────────────
    BG        = (0.10, 0.12, 0.16)
    CARD_BG   = (0.14, 0.16, 0.22)
    ACCENT    = (0.96, 0.42, 0.15)   # оранжевый
    TEXT      = (0.92, 0.92, 0.95)
    TEXT_DIM  = (0.55, 0.58, 0.65)
    GRID      = (0.22, 0.25, 0.32)
    TEMP_C    = (0.35, 0.70, 1.00)
    PRECIP_C  = (0.25, 0.55, 0.90)
    CLOUD_C   = (0.60, 0.62, 0.70)
    WIND_C    = (0.30, 0.85, 0.60)
    GUST_C    = (1.00, 0.65, 0.20)

    def set_color(c, alpha=1.0):
        ctx.set_source_rgba(*c, alpha)

    def rounded_rect(x, y, w, h, r=12):
        ctx.new_sub_path()
        ctx.arc(x+r, y+r, r, math.pi, 1.5*math.pi)
        ctx.arc(x+w-r, y+r, r, 1.5*math.pi, 0)
        ctx.arc(x+w-r, y+h-r, r, 0, 0.5*math.pi)
        ctx.arc(x+r, y+h-r, r, 0.5*math.pi, math.pi)
        ctx.close_path()

    # ── Фон ──────────────────────────────────────────────────────────────────
    set_color(BG)
    ctx.paint()

    # ── Заголовок ─────────────────────────────────────────────────────────────
    HEADER_H = 70
    set_color(CARD_BG)
    rounded_rect(PAD, 12, W - 2*PAD, HEADER_H, 10)
    ctx.fill()

    # Акцентная полоса слева
    set_color(ACCENT)
    ctx.rectangle(PAD, 12, 4, HEADER_H)
    ctx.fill()

    # Название маршрута
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(20)
    set_color(TEXT)
    ctx.move_to(PAD + 18, 12 + 30)
    ctx.show_text(route_name[:50])

    # Детали маршрута
    ctx.set_font_size(13)
    set_color(TEXT_DIM)
    duration_h = (end_dt - start_dt).total_seconds() / 3600
    detail = (
        f"{start_dt.strftime('%d.%m.%Y')}   "
        f"{start_dt.strftime('%H:%M')} → {end_dt.strftime('%H:%M')}   "
        f"📏 {total_km:.1f} км   ⏱ {duration_h:.1f} ч"
    )
    ctx.move_to(PAD + 18, 12 + 55)
    ctx.show_text(detail)

    # ── Карта ветра ───────────────────────────────────────────────────────────
    MAP_X = PAD
    MAP_Y = 12 + HEADER_H + 14

    set_color(CARD_BG)
    rounded_rect(MAP_X, MAP_Y, MAP_W, MAP_H, 10)
    ctx.fill()

    # Заголовок секции
    ctx.set_font_size(13)
    set_color(ACCENT)
    ctx.move_to(MAP_X + 14, MAP_Y + 22)
    ctx.show_text("ВЕТЕР НА МАРШРУТЕ")

    _draw_wind_map(ctx, pts_weather, MAP_X + 14, MAP_Y + 34, MAP_W - 28, MAP_H - 48,
                   ACCENT, TEXT, TEXT_DIM, GRID, WIND_C, GUST_C)

    # ── Правая колонка: 2 графика ─────────────────────────────────────────────
    RX = MAP_X + MAP_W + 18
    RW = W - PAD - RX

    # --- График температуры ---
    TEMP_Y = MAP_Y
    TEMP_BLOCK_H = (MAP_H - 12) // 2

    set_color(CARD_BG)
    rounded_rect(RX, TEMP_Y, RW, TEMP_BLOCK_H, 10)
    ctx.fill()

    ctx.set_font_size(13)
    set_color(ACCENT)
    ctx.move_to(RX + 14, TEMP_Y + 22)
    ctx.show_text("ТЕМПЕРАТУРА")

    _draw_temp_chart(ctx, hourly_series, RX + 14, TEMP_Y + 34, RW - 28, TEMP_BLOCK_H - 48,
                     TEMP_C, TEXT, TEXT_DIM, GRID)

    # --- График осадков/облачности ---
    PREC_Y = TEMP_Y + TEMP_BLOCK_H + 12

    set_color(CARD_BG)
    rounded_rect(RX, PREC_Y, RW, TEMP_BLOCK_H, 10)
    ctx.fill()

    ctx.set_font_size(13)
    set_color(ACCENT)
    ctx.move_to(RX + 14, PREC_Y + 22)
    ctx.show_text("ОБЛАЧНОСТЬ / ОСАДКИ")

    _draw_precip_chart(ctx, hourly_series, RX + 14, PREC_Y + 34, RW - 28, TEMP_BLOCK_H - 48,
                       PRECIP_C, CLOUD_C, TEXT, TEXT_DIM, GRID)

    # ── Нижняя полоска с легендой ветра ───────────────────────────────────────
    LEG_Y = MAP_Y + MAP_H + 12
    LEG_H = H - LEG_Y - 12

    set_color(CARD_BG)
    rounded_rect(PAD, LEG_Y, W - 2*PAD, LEG_H, 10)
    ctx.fill()

    _draw_wind_legend(ctx, pts_weather, PAD + 14, LEG_Y, W - 2*PAD - 28, LEG_H,
                      TEXT, TEXT_DIM, WIND_C, GUST_C, ACCENT)

    # ── Экспорт ──────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    surf.write_to_png(buf)
    buf.seek(0)
    return buf


def _wind_arrow(ctx, cx, cy, direction_deg, length, color, alpha=0.9):
    """Рисует стрелку ветра (направление ОТ куда дует → противоположно метеорологическому)."""
    import cairo
    # В метеорологии direction — откуда дует. Рисуем стрелку В сторону движения воздуха.
    angle = math.radians(direction_deg + 180)
    dx = math.sin(angle) * length
    dy = -math.cos(angle) * length

    ctx.set_source_rgba(*color, alpha)
    ctx.set_line_width(2.0)

    # Тело стрелки
    ctx.move_to(cx - dx * 0.5, cy - dy * 0.5)
    ctx.line_to(cx + dx * 0.5, cy + dy * 0.5)
    ctx.stroke()

    # Наконечник
    tip_x = cx + dx * 0.5
    tip_y = cy + dy * 0.5
    head_len = length * 0.35
    head_angle = 0.45
    ctx.set_source_rgba(*color, alpha)
    for sign in (+1, -1):
        hx = tip_x - dx * 0.5 * head_len / length + math.cos(angle + sign * head_angle) * head_len * 0.6
        hy = tip_y - dy * 0.5 * head_len / length + math.sin(angle + sign * head_angle) * head_len * 0.6
        ctx.move_to(tip_x, tip_y)
        ctx.line_to(hx, hy)
        ctx.stroke()


def _draw_wind_map(ctx, pts_weather, x, y, w, h, accent, text_c, dim_c, grid_c, wind_c, gust_c):
    """Рисует упрощённый профиль маршрута со стрелками ветра."""
    import cairo

    n = len(pts_weather)
    if n == 0:
        return

    lats = [p["lat"] for p in pts_weather]
    lons = [p["lon"] for p in pts_weather]
    dists = [p["dist_km"] for p in pts_weather]
    elevs = [p["elev"] for p in pts_weather]

    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    lat_span = max(lat_max - lat_min, 0.01)
    lon_span = max(lon_max - lon_min, 0.01)

    # Нормируем по сторонам с сохранением пропорций
    aspect = lon_span / lat_span * math.cos(math.radians((lat_min + lat_max) / 2))
    if aspect > w / h:
        map_w, map_h = w, w / aspect
        ox, oy = x, y + (h - map_h) / 2
    else:
        map_w, map_h = h * aspect, h
        ox, oy = x + (w - map_w) / 2, y

    def to_px(lat, lon):
        px = ox + (lon - lon_min) / lon_span * map_w
        py = oy + map_h - (lat - lat_min) / lat_span * map_h
        return px, py

    # Фоновая сетка
    ctx.set_source_rgba(*grid_c, 0.3)
    ctx.set_line_width(0.5)
    for i in range(4):
        xi = ox + map_w * i / 3
        ctx.move_to(xi, oy)
        ctx.line_to(xi, oy + map_h)
        ctx.stroke()

    # Линия трека
    ctx.set_source_rgba(*accent, 0.5)
    ctx.set_line_width(1.5)
    ctx.set_dash([4, 4])
    ctx.move_to(*to_px(pts_weather[0]["lat"], pts_weather[0]["lon"]))
    for p in pts_weather[1:]:
        ctx.line_to(*to_px(p["lat"], p["lon"]))
    ctx.stroke()
    ctx.set_dash([])

    # Стрелки ветра и точки
    winds = [p["wind_spd"] for p in pts_weather]
    max_wind = max(winds) if winds else 1
    max_wind = max(max_wind, 1)

    for p in pts_weather:
        px, py = to_px(p["lat"], p["lon"])
        spd = p["wind_spd"]
        arrow_len = 16 + (spd / max_wind) * 20

        # Цвет по скорости ветра
        if spd < 3:
            c = (0.3, 0.85, 0.45)
        elif spd < 7:
            c = (1.0, 0.80, 0.20)
        else:
            c = (1.0, 0.35, 0.25)

        _wind_arrow(ctx, px, py, p["wind_dir"], arrow_len, c, alpha=0.92)

        # Точка
        ctx.set_source_rgba(*c, 0.9)
        ctx.arc(px, py, 3, 0, 2 * math.pi)
        ctx.fill()

    # Метки старт/финиш
    ctx.set_font_size(11)
    sx, sy = to_px(pts_weather[0]["lat"], pts_weather[0]["lon"])
    ex, ey = to_px(pts_weather[-1]["lat"], pts_weather[-1]["lon"])

    ctx.set_source_rgba(*accent, 1.0)
    ctx.arc(sx, sy, 5, 0, 2 * math.pi); ctx.fill()
    ctx.arc(ex, ey, 5, 0, 2 * math.pi); ctx.fill()

    ctx.set_source_rgba(*text_c, 0.9)
    ctx.move_to(sx + 7, sy + 4); ctx.show_text("▶ старт")
    ctx.move_to(ex + 7, ey + 4); ctx.show_text("⬛ финиш")


def _chart_axes(ctx, x, y, w, h, values, time_labels, y_unit, text_c, dim_c, grid_c, n_grid=4):
    """Рисует оси и сетку, возвращает (vmin, vmax, scale_y)."""
    import cairo

    vmin = min(values)
    vmax = max(values)
    if vmax == vmin:
        vmax = vmin + 1

    # Горизонтальные линии
    ctx.set_line_width(0.5)
    ctx.set_source_rgba(*grid_c, 0.4)
    for i in range(n_grid + 1):
        gy = y + h - h * i / n_grid
        ctx.move_to(x, gy); ctx.line_to(x + w, gy); ctx.stroke()

        val = vmin + (vmax - vmin) * i / n_grid
        ctx.set_font_size(10)
        ctx.set_source_rgba(*dim_c, 0.9)
        label = f"{val:.0f}{y_unit}"
        ctx.move_to(x, gy - 2)
        ctx.show_text(label)

    # Подписи времени
    step = max(1, len(time_labels) // 4)
    for i in range(0, len(time_labels), step):
        tx = x + w * i / (len(time_labels) - 1) if len(time_labels) > 1 else x
        ctx.set_source_rgba(*dim_c, 0.85)
        ctx.set_font_size(10)
        t = time_labels[i]
        lbl = t.strftime("%H:%M") if isinstance(t, datetime) else str(t)
        ctx.move_to(tx - 10, y + h + 14)
        ctx.show_text(lbl)

    return vmin, vmax


def _draw_temp_chart(ctx, series, x, y, w, h, temp_c, text_c, dim_c, grid_c):
    """График температуры."""
    import cairo

    if len(series) < 2:
        return

    times = [s["time"] for s in series]
    temps = [s["temp"] for s in series]
    feels = [s["temp"] - (s["wind_spd"] * 0.5 if s["wind_spd"] > 3 else 0) for s in series]  # упрощённый windchill

    all_vals = temps + feels
    vmin, vmax = _chart_axes(ctx, x, y, w, h - 18, all_vals, times, "°", text_c, dim_c, grid_c)

    def to_y(v):
        return y + (h - 18) - (v - vmin) / (vmax - vmin) * (h - 18)

    n = len(series)

    # Заливка "feels like"
    ctx.set_source_rgba(*temp_c, 0.15)
    ctx.move_to(x, to_y(feels[0]))
    for i, v in enumerate(feels):
        ctx.line_to(x + w * i / (n - 1), to_y(v))
    ctx.line_to(x + w, y + h - 18)
    ctx.line_to(x, y + h - 18)
    ctx.close_path()
    ctx.fill()

    # Линия температуры
    ctx.set_source_rgba(*temp_c, 0.9)
    ctx.set_line_width(2.0)
    ctx.move_to(x, to_y(temps[0]))
    for i, v in enumerate(temps):
        ctx.line_to(x + w * i / (n - 1), to_y(v))
    ctx.stroke()

    # Метки min/max
    t_max_i = temps.index(max(temps))
    t_min_i = temps.index(min(temps))
    for i, label in [(t_max_i, f"{temps[t_max_i]:.0f}°"), (t_min_i, f"{temps[t_min_i]:.0f}°")]:
        ctx.set_source_rgba(*text_c, 0.95)
        ctx.set_font_size(11)
        px = x + w * i / (n - 1)
        py = to_y(temps[i])
        ctx.move_to(px + 4, py - 4)
        ctx.show_text(label)


def _draw_precip_chart(ctx, series, x, y, w, h, precip_c, cloud_c, text_c, dim_c, grid_c):
    """Комбинированный график облачности и осадков."""
    import cairo

    if len(series) < 2:
        return

    times = [s["time"] for s in series]
    clouds = [s["clouds"] for s in series]        # %
    prec   = [s["precip_prob"] for s in series]   # %
    prec_mm = [s["precip"] for s in series]       # мм

    # Облачность — серая заливка (ось 0-100%)
    n = len(series)

    def to_y_pct(v):
        return y + (h - 18) - v / 100.0 * (h - 18)

    # Горизонтальные линии
    ctx.set_line_width(0.5)
    for pct in [25, 50, 75, 100]:
        gy = to_y_pct(pct)
        ctx.set_source_rgba(*grid_c, 0.35)
        ctx.move_to(x, gy); ctx.line_to(x + w, gy); ctx.stroke()
        ctx.set_source_rgba(*dim_c, 0.8)
        ctx.set_font_size(10)
        ctx.move_to(x, gy - 2)
        ctx.show_text(f"{pct}%")

    # Метки времени
    step = max(1, n // 4)
    for i in range(0, n, step):
        px = x + w * i / (n - 1) if n > 1 else x
        ctx.set_source_rgba(*dim_c, 0.85)
        ctx.set_font_size(10)
        ctx.move_to(px - 10, y + h - 4)
        ctx.show_text(times[i].strftime("%H:%M"))

    # Заливка облачности
    ctx.set_source_rgba(*cloud_c, 0.25)
    ctx.move_to(x, to_y_pct(clouds[0]))
    for i, v in enumerate(clouds):
        ctx.line_to(x + w * i / (n - 1), to_y_pct(v))
    ctx.line_to(x + w, y + h - 18)
    ctx.line_to(x, y + h - 18)
    ctx.close_path()
    ctx.fill()

    # Линия облачности
    ctx.set_source_rgba(*cloud_c, 0.7)
    ctx.set_line_width(1.5)
    ctx.move_to(x, to_y_pct(clouds[0]))
    for i, v in enumerate(clouds):
        ctx.line_to(x + w * i / (n - 1), to_y_pct(v))
    ctx.stroke()

    # Столбики вероятности осадков
    bar_w = max(3, w / n - 2)
    for i, v in enumerate(prec):
        if v > 0:
            bx = x + w * i / (n - 1) - bar_w / 2
            bh = v / 100.0 * (h - 18)
            by = y + h - 18 - bh
            ctx.set_source_rgba(*precip_c, 0.65)
            ctx.rectangle(bx, by, bar_w, bh)
            ctx.fill()

    # Легенда
    legend_items = [
        (cloud_c, 0.7, "Облачность"),
        (precip_c, 0.65, "Вер. осадков"),
    ]
    lx = x + w - 140
    for ci, (color, alpha, label) in enumerate(legend_items):
        liy = y + 14 + ci * 16
        ctx.set_source_rgba(*color, alpha)
        ctx.rectangle(lx, liy - 8, 12, 10)
        ctx.fill()
        ctx.set_source_rgba(*dim_c, 0.9)
        ctx.set_font_size(10)
        ctx.move_to(lx + 16, liy)
        ctx.show_text(label)


def _draw_wind_legend(ctx, pts_weather, x, y, w, h, text_c, dim_c, wind_c, gust_c, accent):
    """Нижняя строка: скорость ветра по точкам + легенда."""
    import cairo

    n = len(pts_weather)
    if n == 0:
        return

    # Заголовки колонок: расстояние, ветер, порыв, направление
    ctx.set_font_size(11)
    ctx.set_source_rgba(*dim_c, 0.85)

    col_headers = ["РАССТ.", "ВРЕМЯ", "ВЕТЕР", "ПОРЫВЫ", "НАПРАВЛ."]
    COL_N = len(col_headers)
    col_w = w / (COL_N)

    ctx.move_to(x, y + h * 0.38)
    for ci, hdr in enumerate(col_headers):
        ctx.set_source_rgba(*dim_c, 0.7)
        ctx.move_to(x + col_w * ci, y + h * 0.38)
        ctx.show_text(hdr)

    # Показываем max 6 точек
    show_pts = pts_weather
    if n > 6:
        indices = [int(i * (n - 1) / 5) for i in range(6)]
        show_pts = [pts_weather[i] for i in indices]

    row_w = w / len(show_pts)
    for i, p in enumerate(show_pts):
        rx = x + row_w * i
        ry = y + h * 0.72

        spd = p["wind_spd"]
        gust = p["wind_gust"]

        if spd < 3:
            c = (0.3, 0.85, 0.45)
        elif spd < 7:
            c = (1.0, 0.80, 0.20)
        else:
            c = (1.0, 0.35, 0.25)

        ctx.set_font_size(11)
        # Расстояние
        ctx.set_source_rgba(*dim_c, 0.9)
        ctx.move_to(rx, ry - 22); ctx.show_text(f"{p['dist_km']:.0f} км")
        # Время
        ctx.move_to(rx, ry - 8); ctx.show_text(p["time"].strftime("%H:%M"))
        # Ветер
        ctx.set_source_rgba(*c, 1.0)
        ctx.move_to(rx, ry + 6); ctx.show_text(f"↕{spd:.1f} м/с")
        # Порывы
        ctx.set_source_rgba(*gust_c, 0.85)
        ctx.move_to(rx, ry + 20); ctx.show_text(f"▲{gust:.1f}")
        # Направление
        ctx.set_source_rgba(*dim_c, 0.9)
        ctx.move_to(rx, ry + 34); ctx.show_text(_deg_to_compass(p["wind_dir"]))


def _deg_to_compass(deg):
    dirs = ["С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ",
            "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ"]
    return dirs[round(deg / 22.5) % 16]


def _render_fallback(pts_weather, hourly_series, route_name, start_dt, end_dt, total_km):
    """Если нет Cairo — возвращаем None."""
    return None


# ─── Диалог ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚴 <b>RideWeather</b>\n\n"
        "Пришли GPX файл — скажу какой ветер, температуру и осадки ждать на маршруте.\n\n"
        "Просто отправь <b>.gpx файл</b> чтобы начать.",
        parse_mode="HTML",
    )
    return WAITING_GPX


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚴 <b>RideWeather — справка</b>\n\n"
        "1. Отправь GPX файл\n"
        "2. Укажи дату старта\n"
        "3. Укажи время старта (HH:MM)\n"
        "4. Укажи <b>среднюю скорость</b> (км/ч) или <b>время финиша</b> (HH:MM)\n\n"
        "Получишь карту ветра по маршруту + графики температуры и осадков.",
        parse_mode="HTML",
    )


async def handle_gpx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".gpx"):
        await update.message.reply_text("❌ Отправь файл с расширением .gpx")
        return WAITING_GPX

    status = await update.message.reply_text("⏳ Загружаю GPX...")

    try:
        file = await context.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=".gpx", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            path = tmp.name

        pts = parse_gpx(path)
        os.unlink(path)

        if len(pts) < 2:
            await status.edit_text("❌ GPX файл пустой или повреждён")
            return WAITING_GPX

        # Считаем дистанцию
        total_km = sum(
            haversine_km(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1])
            for i in range(1, len(pts))
        )

        context.user_data["pts"] = pts
        context.user_data["total_km"] = total_km
        context.user_data["route_name"] = doc.file_name.replace(".gpx", "").replace("_", " ")

        await status.delete()

        today = datetime.now()
        tomorrow = today + timedelta(days=1)

        keyboard = [
            [today.strftime("%d.%m.%Y"), tomorrow.strftime("%d.%m.%Y")],
            [(today + timedelta(days=2)).strftime("%d.%m.%Y"),
             (today + timedelta(days=3)).strftime("%d.%m.%Y")],
        ]

        await update.message.reply_text(
            f"✅ GPX загружен: <b>{context.user_data['route_name']}</b>\n"
            f"📏 Дистанция: {total_km:.1f} км | 📍 {len(pts)} точек\n\n"
            f"📅 <b>Выбери или введи дату старта</b> (ДД.ММ.ГГГГ):",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return WAITING_DATE

    except Exception as e:
        logger.error(f"GPX error: {e}")
        await status.edit_text("❌ Ошибка при чтении GPX файла")
        return WAITING_GPX


async def handle_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        date = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Введи дату как <b>ДД.ММ.ГГГГ</b> (например, 15.03.2026):",
            parse_mode="HTML",
        )
        return WAITING_DATE

    context.user_data["date"] = date

    keyboard = [["06:00", "07:00", "08:00"], ["09:00", "10:00", "11:00"], ["12:00", "14:00", "16:00"]]
    await update.message.reply_text(
        f"✅ Дата: <b>{date.strftime('%d.%m.%Y')}</b>\n\n"
        "⏰ <b>Выбери или введи время старта</b> (ЧЧ:ММ):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return WAITING_TIME


async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        t = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Введи время как <b>ЧЧ:ММ</b> (например, 09:30):",
            parse_mode="HTML",
        )
        return WAITING_TIME

    context.user_data["start_time"] = t
    total_km = context.user_data["total_km"]

    # Подсказываем скорость
    keyboard = [["15 км/ч", "18 км/ч", "20 км/ч"], ["25 км/ч", "30 км/ч", "35 км/ч"]]
    await update.message.reply_text(
        f"✅ Старт: <b>{t.strftime('%H:%M')}</b>\n\n"
        f"🚴 <b>Средняя скорость или время финиша?</b>\n"
        f"Напиши скорость (например <code>20 км/ч</code> или просто <code>20</code>)\n"
        f"ИЛИ время финиша (например <code>14:30</code>)",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return WAITING_SPEED


async def handle_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace("км/ч", "").replace("kmh", "").strip()

    start_dt = datetime.combine(context.user_data["date"], context.user_data["start_time"])
    total_km = context.user_data["total_km"]

    # Пробуем распознать как скорость
    duration_h = None
    end_dt = None

    try:
        speed = float(text)
        if speed <= 0 or speed > 200:
            raise ValueError("out of range")
        duration_h = total_km / speed
        end_dt = start_dt + timedelta(hours=duration_h)
    except ValueError:
        # Пробуем как время финиша
        try:
            end_t = datetime.strptime(text, "%H:%M").time()
            end_dt = datetime.combine(context.user_data["date"], end_t)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            duration_h = (end_dt - start_dt).total_seconds() / 3600
        except ValueError:
            await update.message.reply_text(
                "❌ Не понял. Введи скорость (например <code>20</code>) или время финиша (<code>14:30</code>):",
                parse_mode="HTML",
            )
            return WAITING_SPEED

    if duration_h < 0.1 or duration_h > 72:
        await update.message.reply_text(
            "❌ Продолжительность кажется неверной. Проверь скорость или время финиша.",
        )
        return WAITING_SPEED

    status = await update.message.reply_text(
        f"⏳ Считаю погоду по маршруту...\n"
        f"📅 {start_dt.strftime('%d.%m %H:%M')} → {end_dt.strftime('%H:%M')}\n"
        f"⏱ {duration_h:.1f} ч",
        reply_markup=ReplyKeyboardRemove(),
    )

    async def safe_edit(text):
        """Редактирует статус-сообщение, при ошибке отправляет новое."""
        nonlocal status
        try:
            await status.edit_text(text)
        except Exception:
            status = await update.message.reply_text(text)

    try:
        pts = context.user_data["pts"]
        sampled_with_frac, total_km = sample_points(pts, n=10)

        await safe_edit("⏳ Запрашиваю прогноз погоды...")
        pts_weather, hourly_series, forecast = await asyncio.to_thread(
            get_route_weather, sampled_with_frac, start_dt, duration_h
        )

        await safe_edit("🎨 Рисую карточку...")
        png_buf = await asyncio.to_thread(
            render_card,
            pts_weather, hourly_series,
            context.user_data["route_name"],
            start_dt, end_dt, total_km,
        )

        try:
            await status.delete()
        except Exception:
            pass

        if png_buf:
            avg_temp = sum(p["temp"] for p in pts_weather) / len(pts_weather)
            max_wind = max(p["wind_spd"] for p in pts_weather)
            max_gust = max(p["wind_gust"] for p in pts_weather)
            max_precip_prob = max(p["precip_prob"] for p in pts_weather)

            caption = (
                f"🚴 <b>{context.user_data['route_name']}</b>\n"
                f"📅 {start_dt.strftime('%d.%m.%Y')}  "
                f"{start_dt.strftime('%H:%M')} → {end_dt.strftime('%H:%M')}\n"
                f"📏 {total_km:.1f} км  ⏱ {duration_h:.1f} ч\n"
                f"🌡 {avg_temp:.0f}°C  💨 до {max_wind:.1f} м/с (порывы {max_gust:.1f})  "
                f"🌧 {max_precip_prob:.0f}%"
            )
            await update.message.reply_photo(photo=png_buf, caption=caption, parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Ошибка при создании карточки (нет Cairo)")

    except Exception as e:
        logger.error(f"Processing error: {e}", exc_info=True)
        await safe_edit(f"❌ Ошибка при обработке: {e}")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ Установи переменную TELEGRAM_BOT_TOKEN")
        return

    print("🚴 Запускаю RideWeather...")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Document.ALL, handle_gpx),
        ],
        states={
            WAITING_GPX:   [MessageHandler(filters.Document.ALL, handle_gpx)],
            WAITING_DATE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date)],
            WAITING_TIME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time)],
            WAITING_SPEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_speed)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))

    print("✅ RideWeather запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()