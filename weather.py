#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""天气聚合模块（多供应商 + 缓存 + Prompt 注入块）

供应商（均为官方公开 HTTP API）：
  qweather    和风天气：geoapi.qweather.com/v2/city/lookup + {api_host}/v7/weather/now
  amap        高德地图：restapi.amap.com/v3/geocode/geo（城市→adcode）+ /v3/weather/weatherInfo
  caiyun      彩云天气：api.caiyunapp.com/v2.6/{token}/{lon},{lat}/realtime（需配置经纬度）
  openweather OpenWeather：api.openweathermap.org/data/2.5/weather?q={city}&units=metric&lang=zh
  mcp         通用 MCP 天气服务（Streamable HTTP JSON-RPC：initialize → tools/call）

缓存：同一供应商+城市 15 分钟内直接复用（避免刷爆配额）。
输出统一为 {"city","text","temp","humidity","wind","extra"}，失败返回 None 并记日志。
"""

import json
import re
import time

import requests

import logbus

_CACHE = {}          # (provider, city) -> {"ts": float, "data": dict}（多城市独立缓存）
CACHE_TTL_DEFAULT = 60 * 60

# 用户提及天气的触发词：命中才查询（未提及不请求，节省 API 额度）
MENTION_RE = re.compile(
    r"天气|气温|温度|湿度|下雨|降雨|下雪|降雪|刮风|风力|风速|紫外线|带伞|冷不冷|热不热|穿什么")
# 「你那边/你那里」等指代 BOT 所在城市
YOU_RE = re.compile(r"你(那边|那里|所在|的城市|当地|那儿|那边的|住的地方)")
# 「XX天气 / XX的天气」城市提取
CITY_RE = re.compile(r"([\u4e00-\u9fa5]{2,6}?)(?:市)?(?:的)?(?:天气|气温|温度|湿度)")
# 城市提取时的日期/方位前缀剥离（防"今天天气"→"今天"被当城市）
_CITY_STRIP = ("今天早上", "明天早上", "后天早上", "今天", "明天", "后天", "昨天", "现在",
               "目前", "外面", "本地", "周末", "这里", "那里", "这边", "那边",
               "上午", "下午", "晚上", "早上", "清晨")

SKYCON = {"CLEAR_DAY": "晴", "CLEAR_NIGHT": "晴夜", "PARTLY_CLOUDY_DAY": "多云",
          "PARTLY_CLOUDY_NIGHT": "多云", "CLOUDY": "阴", "LIGHT_HAZE": "轻度雾霾",
          "MODERATE_HAZE": "中度雾霾", "HEAVY_HAZE": "重度雾霾", "LIGHT_RAIN": "小雨",
          "MODERATE_RAIN": "中雨", "HEAVY_RAIN": "大雨", "STORM_RAIN": "暴雨",
          "FOG": "雾", "LIGHT_SNOW": "小雪", "MODERATE_SNOW": "中雪",
          "HEAVY_SNOW": "大雪", "STORM_SNOW": "暴雪", "DUST": "浮尘",
          "SAND": "沙尘", "WIND": "大风"}


def _norm(city, text, temp=None, humidity=None, wind=None, extra=""):
    return {"city": city, "text": text, "temp": temp, "humidity": humidity,
            "wind": wind, "extra": extra}


def _strip_footer(text):
    """去除工具返回文本尾部的「数据来源：…」类尾注与上游缺字段的字面 None。"""
    text = re.sub(r"[（(]?数据来源[：:].*?[)）]?$", "", str(text)).strip()
    text = re.sub(r"\s*None\s*", "", text)      # MCP 上游字段缺失时会输出字面 None
    return text.rstrip("；;，, 　·-")


def _qweather(cfg, city):
    """和风天气：旧账户 devapi（地理=geoapi 旧域名）；新账户专属 API Host（地理=/geo/v2 路径）。"""
    w = cfg.get("weather", {})
    key = w.get("api_key", "")
    host = (w.get("api_host", "") or "https://devapi.qweather.com").rstrip("/")
    if "devapi.qweather.com" in host:
        geo_url = "https://geoapi.qweather.com/v2/city/lookup"
        wx_url = host + "/v7/weather/now"
    else:
        geo_url = host + "/geo/v2/city/lookup"
        wx_url = host + "/v7/weather/now"
    r = requests.get(geo_url, params={"location": city, "key": key}, timeout=10)
    r.raise_for_status()
    locs = r.json().get("location") or []
    if not locs:
        raise ValueError("和风城市查询无结果：" + city)
    lid = locs[0]["id"]
    r2 = requests.get(wx_url, params={"location": lid, "key": key}, timeout=10)
    r2.raise_for_status()
    now = r2.json().get("now", {})
    return _norm(city, now.get("text", ""), now.get("temp") and now["temp"] + "°C",
                 now.get("humidity") and now["humidity"] + "%",
                 (now.get("windDir", "") + " " + now.get("windScale", "") + " 级").strip(),
                 "体感 " + now.get("feelsLike", "?") + "°C")


def _amap(cfg, city):
    w = cfg.get("weather", {})
    key = w.get("api_key", "")
    r = requests.get("https://restapi.amap.com/v3/geocode/geo",
                     params={"address": city, "key": key}, timeout=10)
    r.raise_for_status()
    geos = r.json().get("geocodes") or []
    if not geos:
        raise ValueError("高德城市解析无结果：" + city)
    adcode = geos[0]["adcode"]
    r2 = requests.get("https://restapi.amap.com/v3/weather/weatherInfo",
                      params={"city": adcode, "key": key, "extensions": "base"}, timeout=10)
    r2.raise_for_status()
    lives = r2.json().get("lives") or []
    if not lives:
        raise ValueError("高德天气无实况：" + city)
    it = lives[0]
    return _norm(city, it.get("weather", ""), it.get("temperature") and it["temperature"] + "°C",
                 it.get("humidity") and it["humidity"] + "%",
                 (it.get("winddirection", "") + " " + it.get("windpower", "") + " 级").strip(),
                 "发布于 " + it.get("reporttime", ""))


def _caiyun(cfg, city):
    w = cfg.get("weather", {})
    token = w.get("api_key", "")
    lon, lat = w.get("lon", 0), w.get("lat", 0)
    if not lon or not lat:
        raise ValueError("彩云天气需在设置中填写经纬度")
    r = requests.get("https://api.caiyunapp.com/v2.6/%s/%s,%s/realtime" % (token, lon, lat),
                     timeout=10)
    r.raise_for_status()
    rt = r.json().get("result", {}).get("realtime", {})
    sky = SKYCON.get(rt.get("skycon", ""), rt.get("skycon", ""))
    wind = rt.get("wind", {})
    return _norm(city, sky,
                 rt.get("temperature") is not None and "%.1f°C" % rt["temperature"],
                 rt.get("humidity") is not None and "%d%%" % round(rt["humidity"] * 100),
                 "%s %.1f 级" % (wind.get("direction", ""), wind.get("speed", 0) * 3.6 / 5),
                 "彩云实时")


def _openweather(cfg, city):
    w = cfg.get("weather", {})
    key = w.get("api_key", "")
    r = requests.get("https://api.openweathermap.org/data/2.5/weather",
                     params={"q": city, "appid": key, "units": "metric", "lang": "zh"},
                     timeout=10)
    r.raise_for_status()
    d = r.json()
    return _norm(city, (d.get("weather") or [{}])[0].get("description", ""),
                 d.get("main", {}).get("temp") is not None and "%.1f°C" % d["main"]["temp"],
                 d.get("main", {}).get("humidity") is not None and "%d%%" % d["main"]["humidity"],
                 "%.1f m/s" % d.get("wind", {}).get("speed", 0))


def _hfx_mcp(cfg, city):
    """华风爱科天气 MCP（stdio 型）：子进程 weatherwork-consumer-mcp（PyPI，Python 3.11+）。

    鉴权：env WEATHERCN_OPENAPI_KEY（标准版）；实况工具 get_current_weather，
    参数名默认 location（可在 WebUI 修改）。协议：initialize → initialized → tools/call。
    """
    import os
    import subprocess
    w = cfg.get("weather", {})
    cmd = w.get("mcp_command", "weatherwork-consumer-mcp")
    tool = w.get("mcp_tool", "get_current_weather")
    arg_key = w.get("mcp_arg_city_key", "location")
    env = dict(os.environ)
    env[w.get("mcp_env_key", "WEATHERCN_OPENAPI_KEY")] = w.get("api_key", "")
    proc = subprocess.Popen([cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, text=True,
                            encoding="utf-8", bufsize=1)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv_until(want_id, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("id") == want_id:
                return data
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                         "clientInfo": {"name": "tianyi-bot", "version": "1.0"}}})
        if not recv_until(1, 20):
            raise ValueError("MCP initialize 无响应（确认已 pip install weatherwork-consumer-mcp）")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tool_args = {arg_key: city}
        logbus.push("[天气MCP] tools/call %s %s" % (tool, json.dumps(tool_args, ensure_ascii=False)), tag="API")
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": tool, "arguments": tool_args}})
        resp = recv_until(2, 30)
        if not resp:
            raise ValueError("MCP tools/call 无响应")
        if resp.get("error"):
            raise ValueError("MCP 错误：%s" % json.dumps(resp["error"], ensure_ascii=False)[:200])
        content = (resp.get("result", {}).get("content") or [{}])[0].get("text", "")
        logbus.push("[天气MCP 返回] %s" % str(content)[:300], tag="API")
        # 工具软错误（如上游短暂网络异常）会以 JSON error 字段返回——识别后抛错走失败路径
        try:
            parsed = json.loads(content)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("error"):
            raise ValueError("MCP 工具错误：%s" % str(parsed["error"])[:120])
        if isinstance(parsed, dict):
            # 结构化返回：提取实况字段（summary/weather/temperature/humidity/wind_*）
            temp = parsed.get("temperature")
            hum = parsed.get("humidity")
            ws = parsed.get("wind_speed")
            wind = ("%s %.1f km/h" % (parsed.get("wind_direction") or "", ws)
                    if isinstance(ws, (int, float)) and parsed.get("wind_direction") else None)
            # 去除工具自带的「数据来源：…」尾注，避免设备端回复念出来
            text = (parsed.get("summary") or parsed.get("weather") or "天气数据已获取")
            return _norm(city, _strip_footer(text),
                         "%.1f°C" % temp if isinstance(temp, (int, float)) else None,
                         "%d%%" % hum if isinstance(hum, (int, float)) else None,
                         wind, extra="华风爱科 MCP")
        return _norm(city, _strip_footer(content[:150]) or "天气数据已获取", extra="华风爱科 MCP")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


_PROVIDERS = {"qweather": _qweather, "amap": _amap, "caiyun": _caiyun,
              "openweather": _openweather, "hfx_mcp": _hfx_mcp}

# 厂商中文名（WebUI 显示用）
PROVIDER_NAMES = {"qweather": "和风天气", "amap": "高德地图", "caiyun": "彩云天气",
                  "openweather": "OpenWeather", "hfx_mcp": "华风爱科天气 MCP"}


def _resolve_city(cfg, city=None):
    """目标城市：显式指定 > 天气播报城市 > BOT 常驻城市。"""
    w = cfg.get("weather", {})
    return city or w.get("city", "") or cfg.get("bot", {}).get("city", "")


def get_cached(cfg, city=None):
    """只读缓存：返回有效期内（默认 1 小时）该城市的数据；无缓存返回 None。

    绝不触发网络请求——format_block 只读缓存，避免每轮对话偷跑 API。"""
    w = cfg.get("weather", {})
    provider = w.get("provider", "")
    city = _resolve_city(cfg, city)
    if not provider or not city:
        return None
    ttl = int(w.get("cache_minutes", 60)) * 60
    e = _CACHE.get((provider, city))
    if e and time.time() - e["ts"] < ttl:
        return e["data"]
    return None


def get_weather(cfg, city=None, force=False):
    """获取天气（带 1 小时缓存，多城市独立缓存）。失败返回 None 并记录日志。"""
    w = cfg.get("weather", {})
    provider = w.get("provider", "")
    city = _resolve_city(cfg, city)
    if not provider or not city:
        return None
    if not force:
        cached = get_cached(cfg, city)
        if cached:
            return cached
    fn = _PROVIDERS.get(provider)
    if not fn:
        return None
    try:
        logbus.push("[天气请求] %s / %s" % (PROVIDER_NAMES.get(provider, provider), city), tag="API")
        data = fn(cfg, city)
        logbus.push("[天气返回] %s %s %s 湿度%s" %
                    (data["text"], data["temp"] or "", data["wind"] or "", data["humidity"] or "未知"),
                    tag="API")
        _CACHE[(provider, city)] = {"ts": time.time(), "data": data}
        logbus.push("天气获取成功（%s/%s）：%s %s %s" %
                    (provider, city, data["text"], data["temp"] or "", data["wind"] or ""))
        return data
    except Exception as e:
        logbus.push("[警告] 天气获取失败（%s/%s）：%r" % (provider, city, e))
        return None


def extract_city(user_text, cfg):
    """从用户消息解析目标城市：你那边→BOT 城市；「XX天气」→城市名；否则默认城市。"""
    bot_city = cfg.get("bot", {}).get("city", "")
    default = _resolve_city(cfg)
    t = str(user_text or "")
    if YOU_RE.search(t):
        return bot_city or default
    m = CITY_RE.search(t)
    if m:
        c = m.group(1)
        # 逐层剥离日期/方位前缀（如"明天上海的天气"→"上海"；"今天天气"→""→默认城市）
        changed = True
        while changed and c:
            changed = False
            for pfx in _CITY_STRIP:
                if c.startswith(pfx):
                    c = c[len(pfx):]
                    changed = True
                    break
        if len(c) >= 2:
            return c
    return default


def on_user_mention(cfg, user_text):
    """对话注入入口：用户提及天气才确保数据就绪（缓存复用或按需查询）。

    未提及天气 → 返回空串且不做任何请求（节省 API 额度）；
    提及 → 解析目标城市（指定城市/你那边/BOT 城市），1 小时内复用缓存。"""
    if not MENTION_RE.search(str(user_text or "")):
        return ""
    city = extract_city(user_text, cfg)
    logbus.push("[天气] 检测到天气话题，目标城市=%s" % city, tag="API")
    data = get_weather(cfg, city=city)
    if not data:
        return ""
    return format_block(cfg, city=city)


def format_block(cfg, city=None):
    """注入 system prompt 的【天气】块：只读缓存（不触发请求）。

    仅当该城市已有 1 小时内的缓存数据时注入——配合 on_user_mention 按需查询。"""
    w = cfg.get("weather", {})
    if not w.get("provider") or w.get("inject_prompt", True) is False:
        return ""
    data = get_cached(cfg, city)
    if not data:
        return ""
    bot = cfg.get("bot", {})
    bot_name, bot_city = bot.get("name", "洛天依"), bot.get("city", "")
    parts = ["【天气】"]
    if bot_city:
        parts.append("%s所在城市：%s。" % (bot_name, bot_city))
    seg = "%s当前天气：%s" % (data["city"], data["text"])
    # 部分供应商（如华风爱科 MCP）的 text 已含温/湿/风详情，避免重复拼接
    if data["temp"] and "°C" not in data["text"]:
        seg += " " + data["temp"]
    if data["wind"] and "风" not in data["text"]:
        seg += "，" + data["wind"]
    if data["humidity"] and "湿度" not in data["text"]:
        seg += "，湿度 " + data["humidity"]
    parts.append(seg + "。你可以在聊到出门、穿着、心情等话题时自然提到天气，但不要每次都报天气。")
    return "\n".join(parts)
