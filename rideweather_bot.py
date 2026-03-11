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

def render_card(pts_weather, hourly_series, route_name, start_dt, end_dt, total_km, all_pts=None):
    """Рисуем итоговую карточку через Cairo. Вертикальный макет."""
    try:
        import cairo
    except ImportError:
        return _render_fallback(pts_weather, hourly_series, route_name, start_dt, end_dt, total_km)

    # ── Размеры ──────────────────────────────────────────────────────────────
    # Вертикальный макет для телефона
    W        = 900
    PAD      = 16
    GAP      = 12
    HEADER_H = 68
    MAP_H    = 480      # карта занимает много места
    CHART_H  = 200      # каждый график
    H        = PAD + HEADER_H + GAP + MAP_H + GAP + CHART_H + GAP + CHART_H + PAD

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx  = cairo.Context(surf)

    # ── Цвета ────────────────────────────────────────────────────────────────
    BG       = (0.10, 0.12, 0.16)
    CARD_BG  = (0.14, 0.16, 0.22)
    ACCENT   = (0.96, 0.42, 0.15)
    TEXT     = (0.92, 0.92, 0.95)
    TEXT_DIM = (0.55, 0.58, 0.65)
    GRID     = (0.22, 0.25, 0.32)
    TEMP_C   = (0.35, 0.70, 1.00)
    PRECIP_C = (0.25, 0.55, 0.90)
    CLOUD_C  = (0.60, 0.62, 0.70)

    def sc(c, a=1.0): ctx.set_source_rgba(*c, a)

    def rr(x, y, w, h, r=10):
        ctx.new_sub_path()
        ctx.arc(x+r,   y+r,   r, math.pi,       1.5*math.pi)
        ctx.arc(x+w-r, y+r,   r, 1.5*math.pi,   0)
        ctx.arc(x+w-r, y+h-r, r, 0,              0.5*math.pi)
        ctx.arc(x+r,   y+h-r, r, 0.5*math.pi,   math.pi)
        ctx.close_path()

    # ── Фон ──────────────────────────────────────────────────────────────────
    sc(BG); ctx.paint()

    # ── Заголовок ─────────────────────────────────────────────────────────────
    hx, hy = PAD, PAD
    hw = W - 2*PAD
    sc(CARD_BG); rr(hx, hy, hw, HEADER_H); ctx.fill()
    sc(ACCENT);  ctx.rectangle(hx, hy, 4, HEADER_H); ctx.fill()

    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(22)
    sc(TEXT); ctx.move_to(hx+18, hy+32); ctx.show_text(route_name[:55])

    ctx.set_font_size(14); sc(TEXT_DIM)
    duration_h = (end_dt - start_dt).total_seconds() / 3600
    detail = (f"{start_dt.strftime('%d.%m.%Y')}   "
              f"{start_dt.strftime('%H:%M')} → {end_dt.strftime('%H:%M')}   "
              f"□ {total_km:.1f} км   □ {duration_h:.1f} ч")
    ctx.move_to(hx+18, hy+56); ctx.show_text(detail)

    # ── Карта ─────────────────────────────────────────────────────────────────
    cy = hy + HEADER_H + GAP
    sc(CARD_BG); rr(PAD, cy, W-2*PAD, MAP_H); ctx.fill()
    ctx.set_font_size(13); sc(ACCENT)
    ctx.move_to(PAD+14, cy+22); ctx.show_text("ВЕТЕР НА МАРШРУТЕ")

    _draw_wind_map(ctx, pts_weather,
                   PAD+10, cy+32, W-2*PAD-20, MAP_H-42,
                   ACCENT, TEXT, TEXT_DIM, GRID, all_pts=all_pts)

    # ── Температура ───────────────────────────────────────────────────────────
    ty = cy + MAP_H + GAP
    sc(CARD_BG); rr(PAD, ty, W-2*PAD, CHART_H); ctx.fill()
    ctx.set_font_size(13); sc(ACCENT)
    ctx.move_to(PAD+14, ty+22); ctx.show_text("ТЕМПЕРАТУРА")
    _draw_temp_chart(ctx, hourly_series,
                     PAD+14, ty+34, W-2*PAD-28, CHART_H-50,
                     TEMP_C, TEXT, TEXT_DIM, GRID)

    # ── Облачность / осадки ───────────────────────────────────────────────────
    py2 = ty + CHART_H + GAP
    sc(CARD_BG); rr(PAD, py2, W-2*PAD, CHART_H); ctx.fill()
    ctx.set_font_size(13); sc(ACCENT)
    ctx.move_to(PAD+14, py2+22); ctx.show_text("ОБЛАЧНОСТЬ / ОСАДКИ")
    _draw_precip_chart(ctx, hourly_series,
                       PAD+14, py2+34, W-2*PAD-28, CHART_H-50,
                       PRECIP_C, CLOUD_C, TEXT, TEXT_DIM, GRID)

    # ── Экспорт (PNG без потерь) ──────────────────────────────────────────────
    buf = io.BytesIO()
    surf.write_to_png(buf)
    buf.seek(0)
    return buf


def _wind_arrow(ctx, cx, cy, direction_deg, length, color, alpha=0.9, line_w=2.0):
    """Рисует стрелку ветра.
    direction_deg — метеорологическое (ОТКУДА дует).
    Стрелка показывает КУДА движется воздух.
    """
    angle = math.radians(direction_deg + 180)
    vx =  math.sin(angle)
    vy = -math.cos(angle)

    tail_x = cx - vx * length * 0.5
    tail_y = cy - vy * length * 0.5
    tip_x  = cx + vx * length * 0.5
    tip_y  = cy + vy * length * 0.5

    ctx.set_source_rgba(*color, alpha)
    ctx.set_line_width(line_w)
    ctx.set_line_cap(0)   # cairo.LINE_CAP_BUTT

    ctx.move_to(tail_x, tail_y)
    ctx.line_to(tip_x, tip_y)
    ctx.stroke()

    head_len = length * 0.38
    for sign in (+1, -1):
        a2 = angle + sign * math.radians(145)
        hx = tip_x + math.sin(a2) * head_len * 0.55
        hy = tip_y - math.cos(a2) * head_len * 0.55
        ctx.move_to(tip_x, tip_y)
        ctx.line_to(hx, hy)
        ctx.stroke()


def _osm_tile(z, x_tile, y_tile):
    """Скачивает один OSM тайл, возвращает bytes или None."""
    url = f"https://tile.openstreetmap.org/{z}/{x_tile}/{y_tile}.png"
    headers = {"User-Agent": "RideWeatherBot/1.0 (telegram bot)"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def _lat_lon_to_tile(lat, lon, zoom):
    """Номер тайла OSM для заданных координат."""
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    lat_r = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y


def _tile_to_lat_lon(x_tile, y_tile, zoom):
    """Координаты северо-западного угла тайла."""
    n = 2 ** zoom
    lon = x_tile / n * 360 - 180
    lat_r = math.atan(math.sinh(math.pi * (1 - 2 * y_tile / n)))
    lat = math.degrees(lat_r)
    return lat, lon


def _pick_zoom(lat_min, lat_max, lon_min, lon_max, px_w, px_h, tile_size=256):
    """Подбираем зум так чтобы bbox занимал ~65% меньшей стороны области."""
    for z in range(17, 5, -1):
        n = 2 ** z
        # Ширина bbox в пикселях при данном зуме
        mid_lat = (lat_min + lat_max) / 2
        px_lon = (lon_max - lon_min) / 360 * n * tile_size
        # Высота bbox в пикселях (меркатор)
        def merc(la):
            return math.log(math.tan(math.pi/4 + math.radians(la)/2))
        px_lat = abs(merc(lat_max) - merc(lat_min)) / (2 * math.pi) * n * tile_size
        if px_lon < px_w * 0.65 and px_lat < px_h * 0.65:
            return z
    return 6


def _draw_wind_map(ctx, pts_weather, x, y, w, h, accent, text_c, dim_c, grid_c, all_pts=None):
    """Карта маршрута с OSM-подложкой и стрелками ветра."""
    import cairo

    if not pts_weather:
        return

    # ── bbox по ВСЕМ точкам трека (для правильного зума и центрирования) ─────
    track_pts    = all_pts if all_pts else [(p["lat"], p["lon"], 0) for p in pts_weather]
    track_lats   = [p[0] for p in track_pts]
    track_lons   = [p[1] for p in track_pts]

    lat_min, lat_max = min(track_lats), max(track_lats)
    lon_min, lon_max = min(track_lons), max(track_lons)

    # Отступ 15% с каждой стороны
    lat_pad = max((lat_max - lat_min) * 0.18, 0.004)
    lon_pad = max((lon_max - lon_min) * 0.18, 0.006)
    lat_min -= lat_pad; lat_max += lat_pad
    lon_min -= lon_pad; lon_max += lon_pad

    TILE = 256
    zoom = _pick_zoom(lat_min, lat_max, lon_min, lon_max, w, h)

    # ── Тайлы ────────────────────────────────────────────────────────────────
    tx0, ty0 = _lat_lon_to_tile(lat_max, lon_min, zoom)   # СЗ
    tx1, ty1 = _lat_lon_to_tile(lat_min, lon_max, zoom)   # ЮВ
    tx1 = max(tx1, tx0); ty1 = max(ty1, ty0)

    tiles_w = tx1 - tx0 + 1
    tiles_h = ty1 - ty0 + 1
    mosaic_w = tiles_w * TILE
    mosaic_h = tiles_h * TILE

    nw_lat, nw_lon = _tile_to_lat_lon(tx0,     ty0,     zoom)
    se_lat, se_lon = _tile_to_lat_lon(tx1 + 1, ty1 + 1, zoom)

    mosaic = cairo.ImageSurface(cairo.FORMAT_ARGB32, mosaic_w, mosaic_h)
    mc = cairo.Context(mosaic)
    mc.set_source_rgb(0.13, 0.15, 0.20); mc.paint()

    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            data = _osm_tile(zoom, tx, ty)
            if data:
                try:
                    ts = cairo.ImageSurface.create_from_png(io.BytesIO(data))
                    mc.set_source_surface(ts, (tx - tx0) * TILE, (ty - ty0) * TILE)
                    mc.paint()
                except Exception:
                    pass

    # Лёгкое затемнение для читаемости трека
    mc.set_source_rgba(0, 0, 0, 0.35); mc.paint()

    # ── Проекция Меркатора: координаты → пиксели мозаики ─────────────────────
    def merc(la):
        return math.log(math.tan(math.pi / 4 + math.radians(la) / 2))

    merc_nw = merc(nw_lat)
    merc_se = merc(se_lat)

    def geo_to_mosaic(lat, lon):
        px_ = (lon - nw_lon) / (se_lon - nw_lon) * mosaic_w
        py_ = (merc_nw - merc(lat)) / (merc_nw - merc_se) * mosaic_h
        return px_, py_

    # ── Центрируем мозаику в области (x, y, w, h) ────────────────────────────
    # Масштаб: вписываем мозаику целиком
    scale = min(w / mosaic_w, h / mosaic_h)
    disp_w = mosaic_w * scale
    disp_h = mosaic_h * scale
    ox = x + (w - disp_w) / 2
    oy = y + (h - disp_h) / 2

    # Округляем до пикселя — убирает субпиксельное дрожание
    ox = round(ox); oy = round(oy)

    ctx.save()
    # Клиппируем по области блока
    ctx.rectangle(x, y, w, h)
    ctx.clip()
    ctx.translate(ox, oy)
    ctx.scale(scale, scale)
    ctx.set_source_surface(mosaic, 0, 0)
    ctx.paint()
    ctx.restore()

    def to_px(lat, lon):
        mx, my = geo_to_mosaic(lat, lon)
        return ox + mx * scale, oy + my * scale

    # ── Трек: тень + линия ───────────────────────────────────────────────────
    track_coords = [(p[0], p[1]) for p in track_pts]

    ctx.save()
    ctx.rectangle(x, y, w, h); ctx.clip()

    # тень
    ctx.set_source_rgba(0, 0, 0, 0.55)
    ctx.set_line_width(5.0)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.move_to(*to_px(*track_coords[0]))
    for c in track_coords[1:]: ctx.line_to(*to_px(*c))
    ctx.stroke()

    # линия
    ctx.set_source_rgba(*accent, 0.92)
    ctx.set_line_width(2.8)
    ctx.move_to(*to_px(*track_coords[0]))
    for c in track_coords[1:]: ctx.line_to(*to_px(*c))
    ctx.stroke()

    # ── Стрелки ветра (чёрные, два размера) ──────────────────────────────────
    # Маленькая если max(wind_spd, wind_gust) <= 10 м/с, иначе большая
    for p in pts_weather:
        px, py = to_px(p["lat"], p["lon"])
        max_spd = max(p["wind_spd"], p["wind_gust"])
        # Размер соразмерен карте: ~2% от меньшей стороны области
        base = min(w, h) * 0.022
        arrow_len = base if max_spd <= 10 else base * 1.7

        _wind_arrow(ctx, px, py, p["wind_dir"], arrow_len,
                    color=(0, 0, 0), alpha=1.0, line_w=2.2)

    # ── Маркеры старт / финиш ─────────────────────────────────────────────────
    for (lat, lon), label in [
        ((pts_weather[0]["lat"],  pts_weather[0]["lon"]),  "старт"),
        ((pts_weather[-1]["lat"], pts_weather[-1]["lon"]), "финиш"),
    ]:
        px, py = to_px(lat, lon)
        # Тень
        ctx.set_source_rgba(0, 0, 0, 0.65)
        ctx.arc(px, py, 8, 0, 2*math.pi); ctx.fill()
        # Круг
        ctx.set_source_rgba(*accent, 1.0)
        ctx.arc(px, py, 6, 0, 2*math.pi); ctx.fill()
        # Подпись с тенью
        ctx.set_font_size(12)
        ctx.set_source_rgba(0, 0, 0, 0.75)
        ctx.move_to(px + 10, py + 5); ctx.show_text(label)
        ctx.set_source_rgba(1, 1, 1, 0.95)
        ctx.move_to(px + 9,  py + 4); ctx.show_text(label)

    ctx.restore()

    # Копирайт OSM
    ctx.set_font_size(9)
    ctx.set_source_rgba(0, 0, 0, 0.5)
    ctx.move_to(x + w - 174, y + h - 3)
    ctx.show_text("© OpenStreetMap contributors")
    ctx.set_source_rgba(1, 1, 1, 0.5)
    ctx.move_to(x + w - 175, y + h - 4)
    ctx.show_text("© OpenStreetMap contributors")


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
            all_pts=pts,
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