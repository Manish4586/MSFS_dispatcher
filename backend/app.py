from __future__ import annotations

import re
import math
import asyncio, base64, csv, hashlib, hmac, io, json, os, secrets, tempfile, time, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI, HTTPException, Header, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from redis.asyncio import Redis
import uvicorn
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent

load_dotenv(BASE / '.env', override=False)
DATA_DIR = Path(os.getenv('DATA_DIR', BASE / 'data'))
DATA_FILE = DATA_DIR / 'records.json'
AIRPORTS_FILE = Path(os.getenv('AIRPORTS_FILE', BASE / 'data' / 'airports.csv'))
RUNWAYS_FILE = Path(os.getenv('RUNWAYS_FILE', BASE / 'data' / 'runways.csv'))
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')
GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', '0') or '0')
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', '')
DASHBOARD_URL = os.getenv('DASHBOARD_URL', '/msfs/')
SESSION_COOKIE_NAME = os.getenv('SESSION_COOKIE_NAME', 'fltplan_session')
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'true').lower() in ('1','true','yes','on')
SESSION_TTL_SECONDS = int(os.getenv('SESSION_TTL_SECONDS', '43200'))
VATSIM_DATA_URL = 'https://data.vatsim.net/v3/vatsim-data.json'
VATSIM_CACHE_KEY = 'fltplan:vatsim:datafeed'
VATSIM_CACHE_TTL = 12
OAUTH_STATE_TTL_SECONDS = 600
TTL_HOURS = 12
_AIRPORT_CACHE: dict[str, dict[str, str]] | None = None
_RUNWAY_CACHE: dict[tuple[int, int], list[dict[str, float | str]]] | None = None
_METAR_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}

DATA_DIR.mkdir(parents=True, exist_ok=True)
lock = asyncio.Lock()
redis: Redis | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_records() -> dict[str, list[dict[str, Any]]]:
    if not DATA_FILE.exists():
        return {'searches': [], 'published': []}
    try:
        data = json.loads(DATA_FILE.read_text(encoding='utf-8'))
        return {'searches': data.get('searches', []), 'published': data.get('published', [])}
    except (json.JSONDecodeError, OSError):
        return {'searches': [], 'published': []}


def save_records(data: dict[str, list[dict[str, Any]]]) -> None:
    fd, tmp = tempfile.mkstemp(prefix='records-', suffix='.json', dir=DATA_DIR)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def prune(data: dict[str, list[dict[str, Any]]]) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TTL_HOURS)
    changed = False
    for bucket in ('searches', 'published'):
        kept = []
        for item in data[bucket]:
            try:
                created = datetime.fromisoformat(item['created_at'].replace('Z', '+00:00'))
                if created >= cutoff: kept.append(item)
                else: changed = True
            except (KeyError, ValueError, TypeError):
                changed = True
        data[bucket] = kept
    return changed


async def get_records() -> dict[str, list[dict[str, Any]]]:
    async with lock:
        data = load_records()
        if prune(data): save_records(data)
        return data


async def add_record(bucket: str, record: dict[str, Any]) -> None:
    async with lock:
        data = load_records()
        prune(data)
        data[bucket].insert(0, record)
        save_records(data)


class Route(BaseModel):
    dep: str = Field(min_length=3, max_length=4)
    arr: str = Field(min_length=3, max_length=4)
    dep_name: str = ''
    arr_name: str = ''
    dep_country: str = ''
    arr_country: str = ''
    aircraft: str
    aircraft_category: str = ''
    cruise_kts: Optional[int] = None
    scenery: str = ''
    time_label: str = ''
    departure_utc: str = ''
    distance_nm: int = Field(ge=0)
    duration: str = ''
    block_minutes: Optional[int] = None
    liveries: list[str] = Field(default_factory=list)
    max_time_minutes: Optional[int] = None


class PublishRequest(Route):
    pass


app = FastAPI(title='FLTPLAN Dispatch API', version='2.0.0')
_cors_origins = [x.strip() for x in os.getenv('CORS_ORIGINS', '').split(',') if x.strip()]
if _cors_origins and '*' not in _cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_credentials=True,
                       allow_methods=['GET','POST','DELETE','OPTIONS'],
                       allow_headers=['Content-Type','X-CSRF-Token'])


class SelectGuildRequest(BaseModel):
    guild_id: str = Field(min_length=17, max_length=20, pattern=r'^\d{17,20}$')


def redis_required() -> Redis:
    if redis is None:
        raise HTTPException(status_code=503, detail='Redis is not ready')
    return redis


async def rate_limit(key: str, limit: int, window_seconds: int) -> None:
    r = redis_required()
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, window_seconds)
    if count > limit:
        raise HTTPException(status_code=429, detail='Too many requests. Please try again shortly.')


def client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


async def load_session(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or redis is None:
        return None
    raw = await redis.get(f'fltplan:session:{token}')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        await redis.delete(f'fltplan:session:{token}')
        return None


async def require_session(request: Request) -> dict[str, Any]:
    session = await load_session(request)
    if not session:
        raise HTTPException(status_code=401, detail='Sign in with Discord to continue')
    return session


def require_csrf(request: Request, session: dict[str, Any]) -> None:
    supplied = request.headers.get('X-CSRF-Token', '')
    expected = str(session.get('csrf_token', ''))
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail='CSRF validation failed. Refresh the dashboard and try again.')


async def save_session(token: str, session: dict[str, Any]) -> None:
    await redis_required().set(f'fltplan:session:{token}', json.dumps(session), ex=SESSION_TTL_SECONDS)


async def audit(event: str, **fields: Any) -> None:
    if redis is None:
        return
    entry = {'at': now_iso(), 'event': event} | fields
    key = 'fltplan:audit'
    await redis.lpush(key, json.dumps(entry, ensure_ascii=False))
    await redis.ltrim(key, 0, 4999)


def get_oauth_guild_icon(guild: dict[str, Any]) -> str | None:
    icon = guild.get('icon')
    gid = guild.get('id')
    return f'https://cdn.discordapp.com/icons/{gid}/{icon}.png?size=64' if icon and gid else None


async def eligible_guilds_for_user(user_id: str, oauth_guilds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not bot.is_ready() or redis is None:
        return []
    user_guild_ids = {str(g.get('id')): g for g in oauth_guilds if g.get('id')}
    configured: list[tuple[int, str, str]] = []
    async for key in redis.scan_iter(match='fltplan:guild:*:dispatcher_role'):
        parts = key.split(':')
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        gid = parts[2]
        if gid not in user_guild_ids:
            continue
        role_id = await redis.get(key)
        channel_id = await redis.get(f'fltplan:guild:{gid}:publish_channel')
        if role_id and channel_id:
            configured.append((int(gid), str(role_id), str(channel_id)))

    semaphore = asyncio.Semaphore(5)
    async def check(item: tuple[int, str, str]) -> dict[str, Any] | None:
        gid, role_id, channel_id = item
        async with semaphore:
            guild = bot.get_guild(gid)
            if guild is None:
                return None
            try:
                role = guild.get_role(int(role_id))
                if role is None:
                    return None
                member = await guild.fetch_member(int(user_id))
                if not any(r.id == role.id for r in member.roles):
                    return None
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                return None
            info = user_guild_ids.get(str(gid), {})
            return {'id': str(gid), 'name': info.get('name') or guild.name,
                    'icon': get_oauth_guild_icon(info), 'role_id': role_id}

    checked = await asyncio.gather(*(check(x) for x in configured))
    return sorted([x for x in checked if x], key=lambda g: g['name'].casefold())


async def selected_publish_context(session: dict[str, Any]) -> tuple[discord.Guild, discord.abc.Messageable, discord.Member]:
    if not bot.is_ready() or redis is None:
        raise HTTPException(status_code=503, detail='Discord bot or Redis is not ready')
    user = session.get('user') or {}
    user_id = str(user.get('id', ''))
    guild_id = str(session.get('selected_guild_id') or '')
    if not user_id:
        raise HTTPException(status_code=401, detail='Discord session is incomplete. Sign in again.')
    if not guild_id:
        raise HTTPException(status_code=409, detail='Select a Discord server before publishing')
    if not any(str(x.get('id')) == guild_id for x in session.get('eligible_guilds', [])):
        raise HTTPException(status_code=403, detail='This Discord server is not available to your account. Sign in again.')

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise HTTPException(status_code=403, detail='The FLTPLAN bot is no longer in the selected Discord server')
    channel_id = await redis.get(f'fltplan:guild:{guild_id}:publish_channel')
    role_id = await redis.get(f'fltplan:guild:{guild_id}:dispatcher_role')
    if not channel_id or not role_id:
        raise HTTPException(status_code=409, detail='This server is not fully configured. Ask a server administrator to run /msfs setup.')
    role = guild.get_role(int(role_id))
    if role is None:
        raise HTTPException(status_code=409, detail='The configured Dispatcher role no longer exists. Ask an administrator to run /msfs setup again.')
    try:
        member = await guild.fetch_member(int(user_id))
    except discord.NotFound as exc:
        raise HTTPException(status_code=403, detail='You are no longer a member of the selected Discord server') from exc
    except discord.Forbidden as exc:
        raise HTTPException(status_code=502, detail='The bot cannot verify server membership. Check bot access and Discord configuration.') from exc
    except discord.HTTPException as exc:
        raise HTTPException(status_code=502, detail='Discord could not verify your server membership; try again.') from exc
    if not any(r.id == role.id for r in member.roles):
        raise HTTPException(status_code=403, detail='Publishing requires the configured Dispatcher role in this Discord server')

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except discord.NotFound as exc:
            raise HTTPException(status_code=409, detail='Configured publish channel no longer exists') from exc
        except discord.DiscordException as exc:
            raise HTTPException(status_code=502, detail='Configured Discord channel is unavailable') from exc
    if not isinstance(channel, (discord.TextChannel, discord.Thread)) or channel.guild.id != guild.id:
        raise HTTPException(status_code=409, detail='Configured destination is not a text channel in the selected server')
    return guild, channel, member


@app.get('/api/auth/login')
async def auth_login(request: Request):
    await rate_limit(f'fltplan:ratelimit:oauth-login:{client_ip(request)}', 10, 60)
    if not (DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI):
        raise HTTPException(status_code=503, detail='Discord OAuth is not configured. Set DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI.')
    state = secrets.token_urlsafe(32)
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b'=').decode('ascii')
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
    await redis_required().set(f'fltplan:oauth-state:{state}', json.dumps({'verifier': verifier}), ex=OAUTH_STATE_TTL_SECONDS)
    params = {'client_id': DISCORD_CLIENT_ID, 'response_type': 'code',
              'redirect_uri': DISCORD_REDIRECT_URI, 'scope': 'identify guilds',
              'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
    response = RedirectResponse('https://discord.com/oauth2/authorize?' + urllib.parse.urlencode(params), status_code=302)
    response.set_cookie('fltplan_oauth_state', state, max_age=OAUTH_STATE_TTL_SECONDS,
                        httponly=True, secure=SESSION_COOKIE_SECURE, samesite='lax', path='/')
    return response


@app.get('/api/auth/callback')
async def auth_callback(request: Request, code: str | None = None, state: str | None = None,
                        error: str | None = None, iss: str | None = None):
    await rate_limit(f'fltplan:ratelimit:oauth-callback:{client_ip(request)}', 20, 60)
    if error:
        response = RedirectResponse(DASHBOARD_URL + '?auth_error=' + urllib.parse.quote(error[:80]), status_code=303)
        response.delete_cookie('fltplan_oauth_state', path='/', secure=SESSION_COOKIE_SECURE, httponly=True, samesite='lax')
        return response
    if not code or not state:
        raise HTTPException(status_code=400, detail='Discord OAuth callback is missing code or state')
    if iss != 'https://discord.com':
        raise HTTPException(status_code=400, detail='Missing or unexpected OAuth issuer')
    r = redis_required()
    if not request.cookies.get('fltplan_oauth_state') or not hmac.compare_digest(request.cookies.get('fltplan_oauth_state', ''), state):
        raise HTTPException(status_code=400, detail='OAuth state cookie did not match. Start sign-in again.')
    state_key = f'fltplan:oauth-state:{state}'
    state_raw = await r.get(state_key)
    if not state_raw:
        raise HTTPException(status_code=400, detail='OAuth state is invalid or expired. Start sign-in again.')
    await r.delete(state_key)
    try:
        state_data = json.loads(state_raw)
        async with httpx.AsyncClient(timeout=15) as client:
            token_res = await client.post('https://discord.com/api/oauth2/token', data={
                'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET,
                'grant_type': 'authorization_code', 'code': code,
                'redirect_uri': DISCORD_REDIRECT_URI,
                'code_verifier': state_data['verifier'],
            }, headers={'Content-Type': 'application/x-www-form-urlencoded'})
            token_res.raise_for_status()
            access_token = token_res.json()['access_token']
            headers = {'Authorization': f'Bearer {access_token}'}
            user_res = await client.get('https://discord.com/api/users/@me', headers=headers)
            user_res.raise_for_status()
            guild_res = await client.get('https://discord.com/api/users/@me/guilds?limit=200', headers=headers)
            guild_res.raise_for_status()
            discord_user = user_res.json()
            oauth_guilds = guild_res.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=502, detail='Discord OAuth verification failed. Check the OAuth application and redirect URI.') from exc

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Discord bot is starting. Please sign in again in a moment.')
    user_id = str(discord_user.get('id', ''))
    if not user_id:
        raise HTTPException(status_code=502, detail='Discord did not return a user ID')
    eligible = await eligible_guilds_for_user(user_id, oauth_guilds)
    session = {
        'user': {'id': user_id, 'username': discord_user.get('username', 'Discord user'),
                 'global_name': discord_user.get('global_name'), 'avatar': discord_user.get('avatar')},
        'eligible_guilds': eligible,
        'selected_guild_id': eligible[0]['id'] if len(eligible) == 1 else None,
        'csrf_token': secrets.token_urlsafe(32),
        'created_at': now_iso(),
    }
    session_token = secrets.token_urlsafe(48)
    await save_session(session_token, session)
    await audit('oauth_login', user_id=user_id, eligible_guild_ids=[g['id'] for g in eligible])
    response = RedirectResponse(DASHBOARD_URL, status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, session_token, max_age=SESSION_TTL_SECONDS,
                        httponly=True, secure=SESSION_COOKIE_SECURE, samesite='lax', path='/')
    response.delete_cookie('fltplan_oauth_state', path='/', secure=SESSION_COOKIE_SECURE, httponly=True, samesite='lax')
    return response


@app.get('/api/auth/me')
async def auth_me(request: Request):
    session = await load_session(request)
    if not session:
        return {'authenticated': False, 'login_url': '/msfs/api/auth/login'}
    user = session.get('user', {})
    avatar_hash = user.get('avatar')
    avatar_url = (f"https://cdn.discordapp.com/avatars/{user.get('id')}/{avatar_hash}.png?size=64" if avatar_hash else None)
    return {'authenticated': True, 'user': {'id': user.get('id'), 'username': user.get('global_name') or user.get('username'), 'avatar_url': avatar_url},
            'guilds': session.get('eligible_guilds', []), 'selected_guild_id': session.get('selected_guild_id'),
            'csrf_token': session.get('csrf_token')}


@app.post('/api/auth/select-guild')
async def auth_select_guild(body: SelectGuildRequest, request: Request):
    session = await require_session(request)
    require_csrf(request, session)
    if not any(str(x.get('id')) == body.guild_id for x in session.get('eligible_guilds', [])):
        raise HTTPException(status_code=403, detail='You do not have the Dispatcher role in that configured server')
    updated = dict(session)
    updated['selected_guild_id'] = body.guild_id
    token = request.cookies.get(SESSION_COOKIE_NAME, '')
    await save_session(token, updated)
    await audit('guild_selected', user_id=session['user']['id'], guild_id=body.guild_id)
    return {'ok': True, 'selected_guild_id': body.guild_id}


@app.post('/api/auth/logout')
async def auth_logout(request: Request, response: Response):
    session = await require_session(request)
    require_csrf(request, session)
    token = request.cookies.get(SESSION_COOKIE_NAME, '')
    await redis_required().delete(f'fltplan:session:{token}')
    await audit('logout', user_id=session['user']['id'])
    response.delete_cookie(SESSION_COOKIE_NAME, path='/', secure=SESSION_COOKIE_SECURE,
                           httponly=True, samesite='lax')
    return {'ok': True}


@app.get('/api/airports.csv')
async def airports_csv():
    if not AIRPORTS_FILE.is_file():
        raise HTTPException(status_code=404, detail='Airport CSV dataset is missing')
    return FileResponse(AIRPORTS_FILE, media_type='text/csv', filename='airports.csv',
                        headers={'Cache-Control': 'public, max-age=3600'})


@app.get('/api/airports')
async def airport_search(q: str = Query(default='', max_length=100),
                         limit: int = Query(default=20, ge=1, le=100)):
    """Search airport records by ICAO, ident, IATA, name or municipality."""
    if not AIRPORTS_FILE.is_file():
        raise HTTPException(status_code=404, detail='Airport CSV dataset is missing')
    term = q.strip().casefold()
    if not term:
        return {'count': 0, 'results': []}
    results = []
    with AIRPORTS_FILE.open('r', encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            code = (row.get('icao_code') or '').strip()
            ident = (row.get('ident') or '').strip()
            iata = (row.get('iata_code') or '').strip()
            name = (row.get('name') or '').strip()
            municipality = (row.get('municipality') or '').strip()
            haystack = ' '.join((code, ident, iata, name, municipality)).casefold()
            if term not in haystack:
                continue
            try:
                lat, lon = float(row['latitude_deg']), float(row['longitude_deg'])
            except (KeyError, TypeError, ValueError):
                continue
            results.append({'icao': code or ident, 'ident': ident, 'iata': iata,
                'name': name, 'municipality': municipality,
                'country': (row.get('iso_country') or '').strip(),
                'type': (row.get('type') or '').strip(), 'lat': lat, 'lon': lon,
                'elevation_ft': row.get('elevation_ft') or ''})
            if len(results) >= limit:
                break
    return {'count': len(results), 'results': results}


@app.get('/api/metar')
async def airport_metar(ids: str = Query(min_length=4, max_length=9)):
    """Return recent METARs for up to two ICAO stations, cached briefly."""
    stations = [part.strip().upper() for part in ids.split(',') if part.strip()]
    if not stations or len(stations) > 2 or any(not re.fullmatch(r'[A-Z0-9]{4}', code) for code in stations):
        raise HTTPException(status_code=400, detail='Provide one or two valid four-character ICAO station codes')
    cache_key = ','.join(sorted(set(stations)))
    now = asyncio.get_running_loop().time()
    cached = _METAR_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return {'results': cached[1], 'cached': True}
    url = 'https://aviationweather.gov/api/data/metar'
    try:
        async with httpx.AsyncClient(timeout=12.0, headers={'Accept': 'application/json', 'User-Agent': 'FLTPLAN-Dispatch/1.0'}) as client:
            upstream = await client.get(url, params={'ids': ','.join(stations), 'format': 'json', 'hours': 3})
        if upstream.status_code == 204:
            rows = []
        else:
            upstream.raise_for_status()
            data = upstream.json()
            rows = data if isinstance(data, list) else []
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail='Live METAR service is temporarily unavailable') from exc
    _METAR_CACHE[cache_key] = (now + 180, rows)
    return {'results': rows, 'cached': False}


@app.get('/api/health')
async def health():
    redis_ok = False
    if redis:
        try: redis_ok = bool(await redis.ping())
        except Exception: pass
    return {'status': 'ok', 'redis': redis_ok, 'ttl_hours': TTL_HOURS, 'oauth_configured': bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI)}


@app.get('/api/history')
async def history(request: Request):
    session = await require_session(request)
    rows = (await get_records())['searches']
    user_id = str(session['user']['id'])
    return [x for x in rows if str(x.get('user_id', '')) == user_id]


@app.get('/api/published')
async def published(request: Request):
    session = await require_session(request)
    guild_id = str(session.get('selected_guild_id') or '')
    if not guild_id:
        return []
    rows = (await get_records())['published']
    return [x for x in rows if str(x.get('guild_id', '')) == guild_id]


@app.post('/api/search', status_code=201)
async def save_search(route: Route, request: Request):
    session = await require_session(request)
    require_csrf(request, session)
    item = route.model_dump()
    item.update({'id': __import__('uuid').uuid4().hex, 'created_at': now_iso(),
                 'user_id': str(session['user']['id'])})
    await add_record('searches', item)
    if redis: await redis.incr('fltplan:search_count')
    return {'ok': True, 'record': item}




VATSIM_TRACK_INTERVAL = 15
VATSIM_TRACK_TTL = 30 * 24 * 60 * 60
VATSIM_TRACK_MAX_POINTS = 12000


async def _fetch_vatsim_feed(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(VATSIM_DATA_URL)
    response.raise_for_status()
    feed = response.json()
    if not isinstance(feed, dict) or not isinstance(feed.get('pilots'), list):
        raise ValueError('Unexpected VATSIM data feed structure')
    return feed


def _parse_filed_altitude(value: Any) -> int | None:
    if value is None:
        return None
    raw = str(value).strip().upper().replace(' ', '')
    if not raw:
        return None
    match = re.fullmatch(r'(?:FL)?(\d{2,3})', raw)
    if match and raw.startswith('FL'):
        return int(match.group(1)) * 100
    if raw.isdigit():
        number = int(raw)
        return number * 100 if number < 1000 else number
    return None


def _runway_spatial_index() -> dict[tuple[int, int], list[dict[str, float | str]]]:
    global _RUNWAY_CACHE
    if _RUNWAY_CACHE is None:
        _RUNWAY_CACHE = {}
        cell_size = 0.05
        if RUNWAYS_FILE.is_file():
            with RUNWAYS_FILE.open('r', encoding='utf-8-sig', newline='') as handle:
                for row in csv.DictReader(handle):
                    if str(row.get('closed') or '0').strip() == '1':
                        continue
                    try:
                        le_lat = float(row['le_latitude_deg'])
                        le_lon = float(row['le_longitude_deg'])
                        he_lat = float(row['he_latitude_deg'])
                        he_lon = float(row['he_longitude_deg'])
                        le_elev = float(row['le_elevation_ft'])
                        he_elev = float(row['he_elevation_ft'])
                        width = float(row.get('width_ft') or 150)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not (-90 <= le_lat <= 90 and -90 <= he_lat <= 90 and
                            -180 <= le_lon <= 180 and -180 <= he_lon <= 180):
                        continue
                    runway = {
                        'le_lat': le_lat, 'le_lon': le_lon, 'he_lat': he_lat,
                        'he_lon': he_lon, 'le_elev': le_elev, 'he_elev': he_elev,
                        'width': max(10.0, width),
                        'airport': str(row.get('airport_ident') or '').upper(),
                        'surface': str(row.get('surface') or '')
                    }
                    lat_pad = 0.002
                    mean_lat = math.radians((le_lat + he_lat) / 2)
                    lon_pad = 0.002 / max(0.15, math.cos(mean_lat))
                    min_y = math.floor((min(le_lat, he_lat) - lat_pad) / cell_size)
                    max_y = math.floor((max(le_lat, he_lat) + lat_pad) / cell_size)
                    min_x = math.floor((min(le_lon, he_lon) - lon_pad) / cell_size)
                    max_x = math.floor((max(le_lon, he_lon) + lon_pad) / cell_size)
                    for gy in range(min_y, max_y + 1):
                        for gx in range(min_x, max_x + 1):
                            _RUNWAY_CACHE.setdefault((gx, gy), []).append(runway)
    return _RUNWAY_CACHE


def _aircraft_ground_speed_limit(aircraft: str) -> float:
    code = re.split(r'[/\s]', str(aircraft or '').upper())[0]
    if code.startswith(('H', 'R')):
        return 100.0
    if code.startswith(('AT', 'DH', 'BE', 'C1', 'C2', 'PC', 'SF', 'JS')):
        return 165.0
    if code.startswith(('A', 'B', 'C', 'D', 'E', 'F', 'CRJ', 'MD', 'DC', 'IL')):
        return 225.0
    return 140.0


def _runway_proximity(lat: float, lon: float, altitude: Any) -> dict[str, Any] | None:
    try:
        alt = float(altitude)
    except (TypeError, ValueError):
        return None
    cell_size = 0.05
    gx = math.floor(lon / cell_size)
    gy = math.floor(lat / cell_size)
    index = _runway_spatial_index()
    candidates = []
    seen = set()
    for y in range(gy - 1, gy + 2):
        for x in range(gx - 1, gx + 2):
            for runway in index.get((x, y), []):
                key = (runway['airport'], runway['le_lat'], runway['le_lon'], runway['he_lat'], runway['he_lon'])
                if key not in seen:
                    seen.add(key)
                    candidates.append(runway)
    best = None
    for runway in candidates:
        mean_lat = math.radians((lat + runway['le_lat'] + runway['he_lat']) / 3)
        scale_x = 111320 * max(0.15, math.cos(mean_lat))
        scale_y = 110540
        ax = (runway['le_lon'] - lon) * scale_x
        ay = (runway['le_lat'] - lat) * scale_y
        bx = (runway['he_lon'] - lon) * scale_x
        by = (runway['he_lat'] - lat) * scale_y
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / denom)) if denom else 0.0
        distance = math.hypot(ax + t * dx, ay + t * dy)
        runway_half_width = max(15.0, runway['width'] * 0.3048 / 2)
        elevation = runway['le_elev'] + (runway['he_elev'] - runway['le_elev']) * t
        vertical_delta = abs(alt - elevation)
        if distance <= runway_half_width + 35 and vertical_delta <= 180:
            score = distance + vertical_delta * 0.35
            if best is None or score < best['score']:
                best = {'score': score, 'distance_m': distance, 'elevation_ft': elevation,
                        'airport': runway['airport'], 'vertical_delta_ft': vertical_delta}
    return best


def _ground_candidate(lat: float, lon: float, altitude: Any, groundspeed: Any,
                      aircraft: str) -> dict[str, Any] | None:
    try:
        speed = float(groundspeed)
    except (TypeError, ValueError):
        return None
    if speed < 0 or speed > _aircraft_ground_speed_limit(aircraft):
        return None
    match = _runway_proximity(lat, lon, altitude)
    if not match:
        return None
    match['speed_kt'] = speed
    return match


def _confirmed_ground(candidate: dict[str, Any] | None, previous: dict[str, Any] | None,
                      aircraft: str) -> bool:
    if not candidate:
        return False
    if previous and previous.get('on_ground'):
        old_epoch = float(previous.get('epoch') or 0)
        current_epoch = time.time()
        if current_epoch - old_epoch <= 45:
            return True
    if previous and previous.get('ground_candidate'):
        old_epoch = float(previous.get('epoch') or 0)
        if time.time() - old_epoch <= 45:
            return True
    return False


def _vatsim_session_id(pilot: dict[str, Any]) -> str:
    raw = str(pilot.get('logon_time') or pilot.get('callsign') or 'unknown')
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


async def _store_vatsim_track(r: Redis, pilot: dict[str, Any]) -> None:
    cid = str(pilot.get('cid') or '')
    lat, lon = pilot.get('latitude'), pilot.get('longitude')
    if not cid.isdigit() or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return

    fp = pilot.get('flight_plan') or {}
    callsign = str(pilot.get('callsign') or '').strip().upper()
    departure = str(fp.get('departure') or '').strip().upper()
    arrival = str(fp.get('arrival') or '').strip().upper()
    active_key = f'fltplan:vatsim:track:active:{cid}'
    session_id = await r.get(active_key)
    meta_key = f'fltplan:vatsim:trackmeta:{cid}:{session_id}' if session_id else ''
    previous_meta = {}
    if meta_key:
        try:
            previous_meta = json.loads(await r.get(meta_key) or '{}')
        except (ValueError, TypeError):
            previous_meta = {}

    same_flight = (
        session_id
        and previous_meta.get('callsign', '').upper() == callsign
        and previous_meta.get('departure', '').upper() == departure
        and previous_meta.get('arrival', '').upper() == arrival
        and time.time() - float(previous_meta.get('last_seen_epoch') or 0) <= 7200
    )
    if not same_flight:
        session_id = _vatsim_session_id(pilot)
        if await r.exists(f'fltplan:vatsim:track:{cid}:{session_id}'):
            session_id = hashlib.sha256(
                f'{cid}:{pilot.get("logon_time")}:{time.time_ns()}'.encode()
            ).hexdigest()[:20]
        await r.set(active_key, session_id, ex=VATSIM_TRACK_TTL)

    track_key = f'fltplan:vatsim:track:{cid}:{session_id}'
    point = {
        'lat': round(float(lat), 6),
        'lon': round(float(lon), 6),
        'ts': now_iso(),
        'epoch': time.time(),
        'altitude': pilot.get('altitude'),
        'groundspeed': pilot.get('groundspeed'),
        'heading': pilot.get('heading'),
        'transponder': pilot.get('transponder'),
        'qnh_i_hg': pilot.get('qnh_i_hg'),
        'qnh_mb': pilot.get('qnh_mb'),
    }
    aircraft = str(fp.get('aircraft_short') or fp.get('aircraft') or '')
    last_raw = await r.lindex(track_key, 0)
    previous_point = None
    if last_raw:
        try:
            previous_point = json.loads(last_raw)
        except (ValueError, TypeError):
            previous_point = None
    candidate = _ground_candidate(float(lat), float(lon), pilot.get('altitude'),
                                  pilot.get('groundspeed'), aircraft)
    point['ground_candidate'] = bool(candidate)
    point['ground_airport'] = candidate.get('airport') if candidate else None
    point['on_ground'] = _confirmed_ground(candidate, previous_point, aircraft)
    if last_raw:
        try:
            last = previous_point or json.loads(last_raw)
            previous_epoch = float(last.get('epoch') or 0)
            point['gap_before'] = bool(previous_epoch and point['epoch'] - previous_epoch > 45)
            point['gap_seconds'] = max(0, round(point['epoch'] - previous_epoch)) if point['gap_before'] else 0
            if last.get('lat') == point['lat'] and last.get('lon') == point['lon']:
                point['gap_before'] = last.get('gap_before', False)
                point['gap_seconds'] = last.get('gap_seconds', 0)
                pipe = r.pipeline()
                pipe.lset(track_key, 0, json.dumps(point, separators=(',', ':')))
                meta = previous_meta | {'last_seen_epoch': time.time(), 'last_seen': point['ts']}
                pipe.set(meta_key or f'fltplan:vatsim:trackmeta:{cid}:{session_id}',
                         json.dumps(meta, separators=(',', ':')), ex=VATSIM_TRACK_TTL)
                pipe.set(active_key, session_id, ex=VATSIM_TRACK_TTL)
                await pipe.execute()
                return
        except (ValueError, TypeError):
            pass

    meta = {
        'cid': cid,
        'callsign': callsign,
        'name': pilot.get('name') or '',
        'departure': departure,
        'arrival': arrival,
        'aircraft': str(fp.get('aircraft_short') or fp.get('aircraft') or ''),
        'logon_time': pilot.get('logon_time') or '',
        'session_id': session_id,
        'last_seen_epoch': time.time(),
        'last_seen': point['ts'],
        'reconnected': bool(same_flight and previous_meta.get('logon_time') != pilot.get('logon_time')),
    }
    pipe = r.pipeline()
    pipe.lpush(track_key, json.dumps(point, separators=(',', ':')))
    pipe.ltrim(track_key, 0, VATSIM_TRACK_MAX_POINTS - 1)
    pipe.expire(track_key, VATSIM_TRACK_TTL)
    pipe.set(f'fltplan:vatsim:trackmeta:{cid}:{session_id}',
             json.dumps(meta, separators=(',', ':')), ex=VATSIM_TRACK_TTL)
    pipe.set(active_key, session_id, ex=VATSIM_TRACK_TTL)
    sessions_key = f'fltplan:vatsim:track:sessions:{cid}'
    pipe.zadd(sessions_key, {session_id: time.time()})
    pipe.expire(sessions_key, VATSIM_TRACK_TTL)
    await pipe.execute()


async def vatsim_tracker_loop():
    """Collect registered pilots continuously, independent of dashboard/browser activity."""
    headers = {'User-Agent': 'FLTPLAN-Dispatch/2.1 (VATSIM flight history collector)'}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        while True:
            try:
                r = redis_required()
                registrations = await r.hgetall('fltplan:vatsim:members')
                if registrations:
                    feed = await _fetch_vatsim_feed(client)
                    await r.set(VATSIM_CACHE_KEY, json.dumps(feed, separators=(',', ':')), ex=VATSIM_CACHE_TTL)
                    await r.set('fltplan:vatsim:last_good', json.dumps(feed, separators=(',', ':')), ex=300)
                    registered = {str(cid) for cid in registrations.values()}
                    for pilot in feed.get('pilots', []):
                        if str(pilot.get('cid') or '') in registered:
                            await _store_vatsim_track(r, pilot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'VATSIM history collector warning: {type(exc).__name__}: {exc}')
            await asyncio.sleep(VATSIM_TRACK_INTERVAL)



async def _track_response(r: Redis, cid: str, pilot: dict[str, Any]) -> dict[str, Any]:
    session_id = await r.get(f'fltplan:vatsim:track:active:{cid}')
    if not session_id:
        return {'track': [], 'departure_airport': None, 'arrival_airport': None}
    track_key = f'fltplan:vatsim:track:{cid}:{session_id}'
    meta_raw = await r.get(f'fltplan:vatsim:trackmeta:{cid}:{session_id}')
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except (ValueError, TypeError):
        meta = {}
    raw_points = await r.lrange(track_key, 0, VATSIM_TRACK_MAX_POINTS - 1)
    points = []
    for raw in reversed(raw_points):
        try:
            point = json.loads(raw)
            points.append({'latitude': point['lat'], 'longitude': point['lon'], 'timestamp': point.get('ts'), 'altitude': point.get('altitude'), 'groundspeed': point.get('groundspeed'), 'heading': point.get('heading'), 'on_ground': bool(point.get('on_ground')), 'ground_candidate': bool(point.get('ground_candidate')), 'ground_airport': point.get('ground_airport'), 'gap_before': point.get('gap_before', False), 'gap_seconds': point.get('gap_seconds', 0)})
        except (ValueError, TypeError, KeyError):
            continue

    dep_code = (meta.get('departure') or pilot.get('departure') or '').upper()
    arr_code = (meta.get('arrival') or pilot.get('arrival') or '').upper()
    def airport_point(code: str):
        row = airport_lookup(code) if code else None
        if not row:
            return None
        try:
            return {'code': code, 'name': row.get('name') or code,
                    'latitude': float(row['latitude_deg']), 'longitude': float(row['longitude_deg'])}
        except (KeyError, TypeError, ValueError):
            return None

    aircraft_code = re.split(r'[/\\s]', str(meta.get('aircraft') or pilot.get('aircraft') or '').upper())[0]
    vertical_rate_fpm = None
    if len(points) >= 2:
        first, last = points[-2], points[-1]
        try:
            seconds = (datetime.fromisoformat(last['timestamp'].replace('Z', '+00:00')) -
                       datetime.fromisoformat(first['timestamp'].replace('Z', '+00:00'))).total_seconds()
            if seconds > 0 and first.get('altitude') is not None and last.get('altitude') is not None:
                vertical_rate_fpm = round((float(last['altitude']) - float(first['altitude'])) * 60 / seconds)
        except (ValueError, TypeError, KeyError):
            pass
    phase = 'Unknown'
    if vertical_rate_fpm is not None:
        if vertical_rate_fpm > 300: phase = 'Climbing'
        elif vertical_rate_fpm < -300: phase = 'Descending'
        else: phase = 'Cruise / level'
    return {
        'track': points,
        'aircraft_code': aircraft_code,
        'vertical_rate_fpm': vertical_rate_fpm,
        'flight_phase': phase,
        'session_id': session_id,
        'track_points': len(points),
        'departure_airport': airport_point(dep_code),
        'arrival_airport': airport_point(arr_code),
        'track_started_at': points[0]['timestamp'] if points else None,
    }


@app.get('/api/vatsim/registered')
async def vatsim_registered(request: Request):
    r = redis_required()
    await rate_limit(f'fltplan:ratelimit:vatsim-map:{client_ip(request)}', 40, 60)
    registrations = await r.hgetall('fltplan:vatsim:members')
    if not registrations:
        return {'updated_at': None, 'fetched_at': None, 'registered_count': 0,
                'online_count': 0, 'pilots': []}

    raw = await r.get(VATSIM_CACHE_KEY)
    feed = None
    if raw:
        try:
            feed = json.loads(raw)
        except (ValueError, TypeError):
            feed = None

    if feed is None:
        try:
            async with httpx.AsyncClient(timeout=12, headers={'User-Agent': 'FLTPLAN-Dispatch/2.1 (VATSIM community radar)'}) as client:
                response = await client.get(VATSIM_DATA_URL)
                response.raise_for_status()
                feed = response.json()
            if not isinstance(feed, dict) or not isinstance(feed.get('pilots'), list):
                raise ValueError('Unexpected VATSIM data feed structure')
            await r.set(VATSIM_CACHE_KEY, json.dumps(feed, separators=(',', ':')), ex=VATSIM_CACHE_TTL)
            await r.set('fltplan:vatsim:last_good', json.dumps(feed, separators=(',', ':')), ex=300)
        except (httpx.HTTPError, ValueError) as exc:
            stale = await r.get('fltplan:vatsim:last_good')
            if stale:
                try:
                    feed = json.loads(stale)
                except (ValueError, TypeError):
                    feed = None
            if feed is None:
                raise HTTPException(status_code=502, detail='VATSIM live data is temporarily unavailable') from exc

    registered = {str(cid): True for cid in registrations.values()}
    pilots = []
    for pilot in feed.get('pilots', []):
        cid = str(pilot.get('cid', ''))
        if cid not in registered:
            continue
        lat, lon = pilot.get('latitude'), pilot.get('longitude')
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        fp = pilot.get('flight_plan') or {}
        pilot_data = {
            'cid': cid,
            'name': pilot.get('name') or '',
            'callsign': pilot.get('callsign') or '',
            'latitude': lat,
            'longitude': lon,
            'altitude': pilot.get('altitude'),
            'groundspeed': pilot.get('groundspeed'),
            'heading': pilot.get('heading'),
            'on_ground': False,
            'squawk': pilot.get('transponder') or fp.get('assigned_transponder') or '',
            'transponder': pilot.get('transponder') or '',
            'qnh_i_hg': pilot.get('qnh_i_hg'),
            'qnh_mb': pilot.get('qnh_mb'),
            'planned_altitude': _parse_filed_altitude(fp.get('altitude')),
            'aircraft': fp.get('aircraft_short') or fp.get('aircraft') or '',
            'departure': fp.get('departure') or '',
            'arrival': fp.get('arrival') or '',
            'flight_rules': fp.get('flight_rules') or '',
            'route': fp.get('route') or '',
            'last_updated': pilot.get('last_updated') or '',
            'logon_time': pilot.get('logon_time') or '',
        }
        pilot_data.update(await _track_response(r, cid, pilot_data))
        if pilot_data.get('track'):
            pilot_data['on_ground'] = bool(pilot_data['track'][-1].get('on_ground'))
        pilots.append(pilot_data)
    pilots.sort(key=lambda p: (p['callsign'] or p['cid']).casefold())
    return {
        'updated_at': (feed.get('general') or {}).get('update_timestamp') or (feed.get('general') or {}).get('update') or None,
        'fetched_at': now_iso(),
        'registered_count': len(registrations),
        'online_count': len(pilots),
        'pilots': pilots,
    }


async def _vatsim_history_for_cid(r: Redis, cid: str, session_id: str | None = None,
                                  max_sessions: int = 20) -> list[dict[str, Any]]:
    sessions = await r.zrevrange(f'fltplan:vatsim:track:sessions:{cid}', 0, max_sessions - 1)
    if session_id:
        if session_id not in sessions:
            raise HTTPException(status_code=404, detail='Flight history session not found')
        selected = [session_id]
    else:
        selected = sessions

    history = []
    for sid in selected:
        meta_raw = await r.get(f'fltplan:vatsim:trackmeta:{cid}:{sid}')
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (ValueError, TypeError):
            meta = {}
        raw_points = await r.lrange(f'fltplan:vatsim:track:{cid}:{sid}', 0, VATSIM_TRACK_MAX_POINTS - 1)
        points = []
        for raw in reversed(raw_points):
            try:
                p = json.loads(raw)
                points.append({
                    'latitude': p['lat'],
                    'longitude': p['lon'],
                    'timestamp': p.get('ts'),
                    'altitude': p.get('altitude'),
                    'groundspeed': p.get('groundspeed'),
                    'heading': p.get('heading'),
                    'on_ground': bool(p.get('on_ground')),
                    'gap_before': bool(p.get('gap_before')),
                    'gap_seconds': p.get('gap_seconds', 0),
                })
            except (ValueError, TypeError, KeyError):
                continue
        if not points:
            continue
        history.append({
            **meta,
            'cid': cid,
            'session_id': sid,
            'track_points': len(points),
            'started_at': points[0].get('timestamp'),
            'ended_at': points[-1].get('timestamp'),
            'track': points,
        })
    return history


@app.get('/api/vatsim/history')
async def vatsim_all_flight_history(request: Request):
    r = redis_required()
    await rate_limit(f'fltplan:ratelimit:vatsim-history-all:{client_ip(request)}', 12, 60)
    cids = set()
    async for key in r.scan_iter(match='fltplan:vatsim:track:sessions:*', count=500):
        cid = str(key).rsplit(':', 1)[-1]
        if re.fullmatch(r'\d{1,9}', cid):
            cids.add(cid)

    pilots = []
    for cid in sorted(cids):
        sessions = await _vatsim_history_for_cid(r, cid, max_sessions=20)
        if sessions:
            latest = sessions[0]
            pilots.append({
                'cid': cid,
                'callsign': latest.get('callsign') or f'CID {cid}',
                'name': latest.get('name') or '',
                'sessions': sessions,
                'last_seen': latest.get('last_seen') or latest.get('ended_at'),
            })
    pilots.sort(key=lambda item: str(item.get('last_seen') or ''), reverse=True)
    return {'pilots': pilots, 'retention_days': 30, 'session_limit_per_pilot': 20}


@app.get('/api/vatsim/history/{cid}')
async def vatsim_flight_history(cid: str, request: Request,
                                session_id: str | None = Query(default=None, max_length=40)):
    r = redis_required()
    if not re.fullmatch(r'\d{1,9}', cid):
        raise HTTPException(status_code=400, detail='Invalid VATSIM CID')
    await rate_limit(f'fltplan:ratelimit:vatsim-history:{client_ip(request)}', 30, 60)
    history = await _vatsim_history_for_cid(r, cid, session_id=session_id, max_sessions=100)
    return {'cid': cid, 'sessions': history, 'retention_days': 30}


class DispatchBot(commands.Bot):
    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


intents = discord.Intents.default()
bot = DispatchBot(command_prefix='!', intents=intents)

@bot.tree.command(name='register', description='Register your VATSIM CID for the FLTPLAN live map')
@app_commands.describe(vatsim_id='Your numeric VATSIM CID')
async def register_vatsim(interaction: discord.Interaction, vatsim_id: str):
    cid = vatsim_id.strip()
    if not re.fullmatch(r'\d{1,9}', cid):
        await interaction.response.send_message('Enter your numeric VATSIM CID (digits only).', ephemeral=True)
        return
    cid = str(int(cid))
    if redis is None:
        await interaction.response.send_message('Redis is not connected. Please try again later.', ephemeral=True)
        return
    await rate_limit(f'fltplan:ratelimit:vatsim-register:{interaction.user.id}', 5, 3600)
    owner = await redis.hget('fltplan:vatsim:cid_owners', cid)
    if owner and owner != str(interaction.user.id):
        await interaction.response.send_message('That VATSIM CID is already registered to another Discord account.', ephemeral=True)
        return
    old_cid = await redis.hget('fltplan:vatsim:members', str(interaction.user.id))
    if old_cid and old_cid != cid:
        await redis.hdel('fltplan:vatsim:cid_owners', old_cid)
    await redis.hset('fltplan:vatsim:members', str(interaction.user.id), cid)
    await redis.hset('fltplan:vatsim:cid_owners', cid, str(interaction.user.id))
    await audit('vatsim_cid_registered', discord_user_id=str(interaction.user.id), vatsim_cid=cid, guild_id=str(interaction.guild_id or ''))
    await interaction.response.send_message(
        f'Your VATSIM CID **{cid}** is registered. When you are connected as a pilot, your aircraft will appear on the FLTPLAN Live Map. No dashboard Discord login is required.',
        ephemeral=True
    )


@bot.tree.command(name='unregister', description='Remove your VATSIM CID from the FLTPLAN live map')
async def unregister_vatsim(interaction: discord.Interaction):
    if redis is None:
        await interaction.response.send_message('Redis is not connected. Please try again later.', ephemeral=True)
        return
    user_id = str(interaction.user.id)
    cid = await redis.hget('fltplan:vatsim:members', user_id)
    if not cid:
        await interaction.response.send_message('You do not have a VATSIM CID registered.', ephemeral=True)
        return
    await redis.hdel('fltplan:vatsim:members', user_id)
    await redis.hdel('fltplan:vatsim:cid_owners', cid)
    await audit('vatsim_cid_unregistered', discord_user_id=user_id, vatsim_cid=cid)
    await interaction.response.send_message('Your VATSIM CID has been removed from the FLTPLAN Live Map.', ephemeral=True)


class RegisteredListView(discord.ui.View):
    """Ephemeral, paginated view of all VATSIM CID registrations."""
    def __init__(self, entries: list[tuple[str, str]], requester_id: int):
        super().__init__(timeout=180)
        self.entries = entries
        self.requester_id = requester_id
        self.page = 0
        self.page_size = 15
        self.total_pages = max(1, (len(entries) + self.page_size - 1) // self.page_size)
        self._sync_buttons()

    def make_embed(self) -> discord.Embed:
        start = self.page * self.page_size
        page_entries = self.entries[start:start + self.page_size]
        lines = [f"**{start + i + 1}.** <@{user_id}> — CID `{cid}`"
                 for i, (user_id, cid) in enumerate(page_entries)]
        embed = discord.Embed(
            title='FLTPLAN — Registered VATSIM Pilots',
            description='\n'.join(lines) if lines else 'No VATSIM CIDs are registered.',
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f'{len(self.entries)} registered • Page {self.page + 1}/{self.total_pages}')
        return embed

    def _sync_buttons(self):
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message('Only the person who opened this list can change its page.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Previous', style=discord.ButtonStyle.secondary, emoji='◀')
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label='Next', style=discord.ButtonStyle.secondary, emoji='▶')
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)


@bot.tree.command(name='registeredlist', description='Show all VATSIM CIDs registered with FLTPLAN')
async def registeredlist(interaction: discord.Interaction):
    if redis is None:
        await interaction.response.send_message('Redis is not connected. Please try again later.', ephemeral=True)
        return
    registrations = await redis.hgetall('fltplan:vatsim:members')
    entries = sorted(
        ((str(user_id), str(cid)) for user_id, cid in registrations.items()),
        key=lambda item: (int(item[1]) if item[1].isdigit() else 0, item[0]),
    )
    view = RegisteredListView(entries, interaction.user.id)
    await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)


msfs_group = app_commands.Group(name='msfs', description='MSFS dispatch configuration')


@msfs_group.command(name='publish', description='Set the channel where FLTPLAN routes are published')
@app_commands.describe(channel='Discord text channel for published flight plans')
@app_commands.checks.has_permissions(manage_guild=True)
async def publish_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if redis is None:
        await interaction.response.send_message('Redis is not connected; cannot save the channel.', ephemeral=True)
        return
    await redis.set(f'fltplan:guild:{interaction.guild_id}:publish_channel', str(channel.id))
    await interaction.response.send_message(f'FLTPLAN publishing channel set to {channel.mention}.', ephemeral=True)


@publish_channel.error
async def publish_channel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = 'You need Manage Server permission to configure the publishing channel.' if isinstance(error, app_commands.MissingPermissions) else 'Could not configure the publishing channel.'
    if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
    else: await interaction.response.send_message(msg, ephemeral=True)


@msfs_group.command(name='setup', description='Configure FLTPLAN publishing channel and Dispatcher role')
@app_commands.describe(channel='Discord text channel for published flight plans',
                       dispatcher_role='Role members must have to publish routes')
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_guild(interaction: discord.Interaction, channel: discord.TextChannel,
                      dispatcher_role: discord.Role):
    if redis is None:
        await interaction.response.send_message('Redis is not connected; cannot save configuration.', ephemeral=True)
        return
    if interaction.guild is None or channel.guild.id != interaction.guild.id:
        await interaction.response.send_message('Choose a channel from this server.', ephemeral=True)
        return
    if dispatcher_role.is_default() or dispatcher_role.managed:
        await interaction.response.send_message('Choose a normal server role, not @everyone or a managed integration role.', ephemeral=True)
        return
    await redis.set(f'fltplan:guild:{interaction.guild_id}:publish_channel', str(channel.id))
    await redis.set(f'fltplan:guild:{interaction.guild_id}:dispatcher_role', str(dispatcher_role.id))
    await audit('guild_configured', guild_id=str(interaction.guild_id), channel_id=str(channel.id), role_id=str(dispatcher_role.id), configured_by=str(interaction.user.id))
    await interaction.response.send_message(
        f'✈️ **MSFS FLTPLAN Dispatch configured successfully!**\n\n'
        f'📢 **Publishing channel:** {channel.mention}\n'
        f'🛡️ **Required publishing role:** **{dispatcher_role.name}**\n\n'
        f'Members must sign in to the dashboard with Discord and hold the required role to publish flights.\n\n'
        f'🌐 **Dashboard:** https://msfs-dispatch.mywire.org',
        ephemeral=False
    )

@setup_guild.error
async def setup_guild_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = 'You need Manage Server permission to configure FLTPLAN.' if isinstance(error, app_commands.MissingPermissions) else 'Could not configure FLTPLAN for this server.'
    if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
    else: await interaction.response.send_message(msg, ephemeral=True)


bot.tree.add_command(msfs_group)


def airport_lookup(code: str) -> dict[str, str] | None:
    """Resolve an ICAO airport from the bundled OurAirports-compatible CSV."""
    global _AIRPORT_CACHE
    if _AIRPORT_CACHE is None:
        _AIRPORT_CACHE = {}
        if AIRPORTS_FILE.is_file():
            with AIRPORTS_FILE.open('r', encoding='utf-8-sig', newline='') as handle:
                for row in csv.DictReader(handle):
                    ident = (row.get('icao_code') or row.get('gps_code') or row.get('ident') or '').strip().upper()
                    if ident and row.get('latitude_deg') and row.get('longitude_deg'):
                        _AIRPORT_CACHE[ident] = row
    return _AIRPORT_CACHE.get(code.strip().upper())


def simbrief_aircraft_code(aircraft: str) -> str:
    """Best-effort mapping from dashboard display names to common SimBrief ICAO types."""
    value = aircraft.upper().replace('AIRBUS', '').replace('BOEING', '').strip()
    mappings = (
        ('A350-1000', 'A35K'), ('A350-900', 'A359'), ('A350', 'A359'),
        ('A380', 'A388'), ('A330-900', 'A339'), ('A330-800', 'A338'), ('A330', 'A333'),
        ('A340-600', 'A346'), ('A340', 'A343'), ('A321NEO', 'A21N'), ('A321 NEO', 'A21N'),
        ('A321', 'A321'), ('A320NEO', 'A20N'), ('A320 NEO', 'A20N'), ('A320', 'A320'),
        ('A319', 'A319'), ('B787-10', 'B78X'), ('787-10', 'B78X'), ('B787-9', 'B789'),
        ('787-9', 'B789'), ('B787-8', 'B788'), ('787-8', 'B788'), ('B777-300ER', 'B77W'),
        ('777-300ER', 'B77W'), ('B777-200LR', 'B77L'), ('777-200LR', 'B77L'),
        ('B777', 'B77W'), ('777', 'B77W'), ('B767-300', 'B763'), ('767', 'B763'),
        ('B757', 'B752'), ('757', 'B752'), ('B747-8', 'B748'), ('747-8', 'B748'),
        ('B747', 'B744'), ('747', 'B744'), ('B737 MAX 8', 'B38M'), ('737 MAX 8', 'B38M'),
        ('B737-800', 'B738'), ('737-800', 'B738'), ('B737', 'B738'), ('737', 'B738'),
        ('CRJ-900', 'CRJ9'), ('CRJ-700', 'CRJ7'), ('E175', 'E75L'), ('E190', 'E190'),
    )
    for label, code in mappings:
        if label in value:
            return code

    import re
    match = re.search(r'\b([A-Z0-9]{3,4})\b', value)
    return match.group(1) if match else ''


def build_msfs_pln(data: dict[str, Any]) -> bytes | None:
    dep = airport_lookup(data['dep'])
    arr = airport_lookup(data['arr'])
    if not dep or not arr:
        return None

    root = ET.Element('SimBase.Document', {'Type': 'AceXML', 'version': '1,0'})
    ET.SubElement(root, 'Descr').text = 'AceXML Document'
    plan = ET.SubElement(root, 'FlightPlan.FlightPlan')
    ET.SubElement(plan, 'Title').text = f"{data['dep']} to {data['arr']}"
    ET.SubElement(plan, 'Descr').text = f"{data['dep']} to {data['arr']} - FLTPLAN Dispatch direct route suggestion"
    ET.SubElement(plan, 'FPType').text = 'IFR'
    ET.SubElement(plan, 'RouteType').text = 'Direct'
    ET.SubElement(plan, 'CruisingAlt').text = '0'
    ET.SubElement(plan, 'DepartureID').text = data['dep'].upper()
    ET.SubElement(plan, 'DestinationID').text = data['arr'].upper()
    ET.SubElement(plan, 'DepartureName').text = data.get('dep_name') or dep.get('name') or data['dep']
    ET.SubElement(plan, 'DestinationName').text = data.get('arr_name') or arr.get('name') or data['arr']
    app_version = ET.SubElement(plan, 'AppVersion')
    ET.SubElement(app_version, 'AppVersionMajor').text = '12'
    def dms(value: float, latitude: bool) -> str:
        direction = ('N' if value >= 0 else 'S') if latitude else ('E' if value >= 0 else 'W')
        absolute = abs(value)
        degrees = int(absolute)
        minutes_float = (absolute - degrees) * 60
        minutes = int(minutes_float)
        seconds = (minutes_float - minutes) * 60
        return f"{direction}{degrees:02d}° {minutes:02d}' {seconds:05.2f}\""
    def world_position(row: dict[str, str]) -> str:
        lat, lon = float(row['latitude_deg']), float(row['longitude_deg'])
        altitude_ft = float(row.get('elevation_ft') or 0)
        return f"{dms(lat, True)},{dms(lon, False)},+{altitude_ft:09.2f}"
    ET.SubElement(plan, 'DepartureLLA').text = world_position(dep)
    ET.SubElement(plan, 'DestinationLLA').text = world_position(arr)
    for code, row in ((data['dep'].upper(), dep), (data['arr'].upper(), arr)):
        waypoint = ET.SubElement(plan, 'ATCWaypoint', {'id': code})
        ET.SubElement(waypoint, 'ATCWaypointType').text = 'Airport'
        ET.SubElement(waypoint, 'WorldPosition').text = world_position(row)
        icao = ET.SubElement(waypoint, 'ICAO')
        ET.SubElement(icao, 'ICAOIdent').text = code
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


@app.post('/api/publish', status_code=201)
async def publish_route(route: PublishRequest, request: Request):
    session = await require_session(request)
    require_csrf(request, session)
    await rate_limit(f"fltplan:ratelimit:publish:{session['user']['id']}", 8, 60)
    guild, channel, member = await selected_publish_context(session)
    user_id = str(session['user']['id'])
    guild_id = str(guild.id)
    cooldown_key = f'fltplan:cooldown:publish:{guild_id}:{user_id}'
    if not await redis.set(cooldown_key, '1', ex=8, nx=True):
        raise HTTPException(status_code=429, detail='Please wait a few seconds before publishing another route')

    data = route.model_dump()
    simbrief_params = {'orig': data['dep'].upper(), 'dest': data['arr'].upper()}
    aircraft_code = simbrief_aircraft_code(data.get('aircraft', ''))
    if aircraft_code:
        simbrief_params['type'] = aircraft_code
    simbrief_url = 'https://www.simbrief.com/system/dispatch.php?' + urllib.parse.urlencode(simbrief_params)

    pln_bytes = build_msfs_pln(data)
    if pln_bytes is None:
        await redis.delete(cooldown_key)
        raise HTTPException(status_code=422, detail='Could not generate MSFS PLN: origin or destination airport coordinates are missing from airports.csv')

    embed = discord.Embed(
        title=f"✈  {data['dep'].upper()}  ━━━▶  {data['arr'].upper()}",
        description=(f"**{data.get('dep_name') or data['dep']}** · {data.get('dep_country') or 'Origin'}\n"
                     f"**{data.get('arr_name') or data['arr']}** · {data.get('arr_country') or 'Destination'}\n\n"
                     f"`{data.get('aircraft_category') or 'FLIGHT'}  /  FLTPLAN DISPATCH`"),
        color=0x4285F4, timestamp=datetime.now(timezone.utc))
    embed.set_author(name='FLTPLAN DISPATCH  •  FLIGHT BRIEFING')
    embed.add_field(name='Aircraft', value=f"**{data['aircraft']}**\n{data.get('aircraft_category') or 'Aircraft'} · {data.get('cruise_kts') or '—'} kt cruise", inline=True)
    embed.add_field(name='Distance / Block', value=f"**{data['distance_nm']:,} NM**\n{data.get('duration') or 'Time unavailable'} block", inline=True)
    suggested_departure = re.sub(r'\s*UTC\s*$', '', (data.get('departure_utc') or '').strip(), flags=re.IGNORECASE)
    embed.add_field(name='Suggested departure', value=f"**{suggested_departure} UTC**" if suggested_departure else 'Not specified', inline=True)
    embed.add_field(name='Scenery / Lighting', value=f"**{data.get('scenery') or 'Not classified'}**\n{data.get('time_label') or 'Day/night not specified'}", inline=True)
    if data.get('liveries'):
        embed.add_field(name='Possible liveries', value=' · '.join(data['liveries'])[:1024], inline=False)
    embed.add_field(name='Flight deck note', value='Suggested route only. Verify weather, NOTAMs, fuel, alternates and aircraft performance before departure.', inline=False)
    embed.set_footer(text='FLTPLAN Dispatch  •  Microsoft Flight Simulator  •  PLN coordinates from airport dataset')

    try:
        message = await channel.send(embed=embed, file=discord.File(io.BytesIO(pln_bytes), filename=f"{data['dep'].upper()}-{data['arr'].upper()}-FLTPLAN.pln"))
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label='Open in SimBrief', style=discord.ButtonStyle.link, url=simbrief_url, emoji='🧭'))
        if message.attachments:
            view.add_item(discord.ui.Button(label='Download MSFS .PLN', style=discord.ButtonStyle.link, url=message.attachments[0].url, emoji='📥'))
        await message.edit(view=view)
    except discord.Forbidden as exc:
        await redis.delete(cooldown_key)
        raise HTTPException(status_code=403, detail='Bot lacks permission to view the channel, send messages, embed links or attach files') from exc
    except discord.DiscordException as exc:
        await redis.delete(cooldown_key)
        raise HTTPException(status_code=502, detail='Discord rejected the publish request') from exc

    item = data | {'id': __import__('uuid').uuid4().hex, 'created_at': now_iso(),
                   'user_id': user_id, 'username': session['user'].get('username'),
                   'guild_id': guild_id, 'guild_name': guild.name,
                   'channel_id': str(channel.id), 'channel_name': getattr(channel, 'name', 'channel'),
                   'message_id': str(message.id), 'message_url': message.jump_url}
    await add_record('published', item)
    await audit('route_published', user_id=user_id, guild_id=guild_id, channel_id=str(channel.id),
                message_id=str(message.id), route=f"{data['dep'].upper()}-{data['arr'].upper()}")
    if redis: await redis.incr('fltplan:published_count')
    return {'ok': True, 'record': item, 'message_url': message.jump_url}


@app.delete('/api/history')
async def clear_history(request: Request):
    session = await require_session(request)
    require_csrf(request, session)
    user_id = str(session['user']['id'])
    async with lock:
        data = load_records()
        data['searches'] = [x for x in data['searches'] if str(x.get('user_id', '')) != user_id]
        save_records(data)
    await audit('history_cleared', user_id=user_id)
    return {'ok': True}


async def maintenance_loop():
    while True:
        await asyncio.sleep(300)
        await get_records()


async def discord_worker():
    """Run Discord separately so authentication failures never take down the API."""
    if not BOT_TOKEN:
        print('WARNING: DISCORD_BOT_TOKEN is empty; API will run but publishing is disabled.')
        return

    try:
        await bot.start(BOT_TOKEN)
    except discord.LoginFailure:
        print('ERROR: Discord rejected DISCORD_BOT_TOKEN (401). API will continue; publishing is disabled.')
    except Exception as exc:
        print(f'ERROR: Discord bot stopped ({type(exc).__name__}: {exc}). API will continue; publishing is disabled.')
    finally:

        if not bot.is_closed():
            await bot.close()


async def main():
    global redis
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await redis.ping()

    config = uvicorn.Config(
        app,
        host=os.getenv('API_HOST', '127.0.0.1'),
        port=int(os.getenv('API_PORT', '8787')),
        log_level='info',
        proxy_headers=True,
    )
    server = uvicorn.Server(config)
    tasks = [
        asyncio.create_task(server.serve(), name='fastapi'),
        asyncio.create_task(maintenance_loop(), name='maintenance'),
        asyncio.create_task(vatsim_tracker_loop(), name='vatsim-history'),
        asyncio.create_task(discord_worker(), name='discord'),
    ]

    try:

        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not bot.is_closed():
            await bot.close()
        if redis:
            await redis.aclose()


if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
