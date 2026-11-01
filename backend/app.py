from __future__ import annotations

import re
import asyncio, base64, csv, hashlib, hmac, io, json, os, secrets, tempfile, urllib.parse, xml.etree.ElementTree as ET
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
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')
                                                                  
                                                                                                    
# DISCORD_GUILD_ID is retained only as an optional command-sync override.
# Publishing destinations are selected per authenticated user/session.
GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', '0') or '0')
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', '')
DASHBOARD_URL = os.getenv('DASHBOARD_URL', '/msfs/')
SESSION_COOKIE_NAME = os.getenv('SESSION_COOKIE_NAME', 'fltplan_session')
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'true').lower() in ('1','true','yes','on')
SESSION_TTL_SECONDS = int(os.getenv('SESSION_TTL_SECONDS', '43200'))
OAUTH_STATE_TTL_SECONDS = 600
TTL_HOURS = 12
_AIRPORT_CACHE: dict[str, dict[str, str]] | None = None

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
    # Uvicorn proxy_headers=True trusts the local Nginx proxy for forwarded client IP.
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
        f'FLTPLAN configured. Channel: {channel.mention}. Required publishing role: **{dispatcher_role.name}**.\n'
        'Members must sign in to the dashboard with Discord and hold this role to publish.', ephemeral=True)


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
