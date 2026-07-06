import os
import json
import time
import asyncio
import sqlite3
import requests
from aiohttp import web
from twitchio.ext import commands


# project root = the folder ABOVE this server/ folder (where .env lives)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- tiny .env loader (absolute path, works from any cwd) ----------
def load_env(path=os.path.join(ROOT, '.env')):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

CLIENT_ID     = os.environ['TWITCH_CLIENT_ID']
CLIENT_SECRET = os.environ['TWITCH_CLIENT_SECRET']
DB_PATH       = os.environ.get('DB_PATH', 'onp.db')
UPDATE_PORT   = int(os.environ.get('UPDATE_PORT', '8080'))
HELP_URL      = os.environ.get('HELP_URL', 'https://onp.example.com')
OSU_CLIENT_ID     = os.environ.get('OSU_CLIENT_ID')
OSU_CLIENT_SECRET = os.environ.get('OSU_CLIENT_SECRET')
TTL           = 30          # seconds before an agent play is "stale"
NP_FRESH_MINS = 8           # osu! last play newer than this = "currently playing"
POLL_SECONDS  = 10          # re-check modded channels every 10s (near-instant remove/re-add)
TOKENS_FILE   = os.path.join(ROOT, '.bot_tokens.json')

STATE = {}   # channel -> {'np': {...}, 'ts': epoch}
PAIRS = {}   # pair_token -> channel


# ---------- token storage / refresh ----------
def load_tokens():
    if os.path.exists(TOKENS_FILE):
        return json.load(open(TOKENS_FILE))
    return {'access': os.environ['BOT_TOKEN'], 'refresh': os.environ['BOT_REFRESH']}

def save_tokens(t):
    json.dump(t, open(TOKENS_FILE, 'w'))

def refresh_tokens(t):
    r = requests.post('https://id.twitch.tv/oauth2/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': t['refresh'],
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
    }, timeout=10).json()
    if 'access_token' not in r:
        raise RuntimeError(f'token refresh failed: {r}')
    t = {'access': r['access_token'], 'refresh': r.get('refresh_token', t['refresh'])}
    save_tokens(t)
    return t


# ---------- twitch helix helpers ----------
def helix(path, token, params=None):
    return requests.get(f'https://api.twitch.tv/helix/{path}', headers={
        'Client-Id': CLIENT_ID, 'Authorization': f'Bearer {token}',
    }, params=params or {}, timeout=10)

def fetch_self(token):
    data = helix('users', token).json()['data'][0]
    return data['id'], data['login']

def fetch_modded_channels(token, bot_id):
    """Every channel where the bot is a moderator (auto-join list)."""
    names, cursor = [], None
    while True:
        params = {'user_id': bot_id, 'first': 100}
        if cursor:
            params['after'] = cursor
        resp = helix('moderation/channels', token, params).json()
        for row in resp.get('data', []):
            names.append(row['broadcaster_login'])
        cursor = resp.get('pagination', {}).get('cursor')
        if not cursor:
            break
    return names


# ---------- database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def mark_joined(channel):
    conn = db()
    try:
        conn.execute(
            'INSERT INTO channels (channel, bot_joined) VALUES (?, 1) '
            'ON CONFLICT(channel) DO UPDATE SET bot_joined=1', (channel,))
        conn.commit()
    finally:
        conn.close()

def get_settings(channel):
    with db() as conn:
        return conn.execute(
            'SELECT np_template, enabled FROM channels WHERE channel=?',
            (channel,)).fetchone()

def set_enabled(channel, value):
    conn = db()
    try:
        conn.execute('UPDATE channels SET enabled=? WHERE channel=?', (value, channel))
        conn.commit()
    finally:
        conn.close()

def set_template(channel, template):
    conn = db()
    try:
        conn.execute('UPDATE channels SET np_template=? WHERE channel=?', (template, channel))
        conn.commit()
    finally:
        conn.close()

def load_pairs():
    with db() as conn:
        rows = conn.execute(
            'SELECT channel, pair_token FROM channels WHERE pair_token IS NOT NULL'
        ).fetchall()
    return {r['pair_token']: r['channel'] for r in rows}

def get_removed():
    """Channels the user explicitly removed via the dashboard (don't rejoin)."""
    with db() as conn:
        try:
            rows = conn.execute('SELECT channel FROM channels WHERE removed=1').fetchall()
        except sqlite3.OperationalError:
            return set()   # older DB without the column yet
    return {r['channel'] for r in rows}


# ---------- osu! API v2 (instant mode: last play + online status) ----------
_OSU_TOKEN = {'access': None, 'exp': 0}

def osu_token():
    if _OSU_TOKEN['access'] and time.time() < _OSU_TOKEN['exp'] - 60:
        return _OSU_TOKEN['access']
    r = requests.post('https://osu.ppy.sh/oauth/token', json={
        'client_id': OSU_CLIENT_ID, 'client_secret': OSU_CLIENT_SECRET,
        'grant_type': 'client_credentials', 'scope': 'public',
    }, timeout=10).json()
    _OSU_TOKEN['access'] = r.get('access_token')
    _OSU_TOKEN['exp'] = time.time() + r.get('expires_in', 3600)
    return _OSU_TOKEN['access']

def osu_get(path):
    return requests.get(f'https://osu.ppy.sh/api/v2/{path}', headers={
        'Authorization': f'Bearer {osu_token()}'}, timeout=10)

def get_osu_id(channel):
    with db() as conn:
        try:
            row = conn.execute('SELECT osu_id FROM channels WHERE channel=?',
                              (channel,)).fetchone()
        except sqlite3.OperationalError:
            return None
    return row['osu_id'] if row else None

def osu_last_play(osu_id):
    """Returns (minutes_since_play, play_dict) or (None, None). Uses the play's
    timestamp instead of osu!'s unreliable is_online flag."""
    try:
        scores = osu_get(
            f'users/{osu_id}/scores/recent?include_fails=1&mode=osu&limit=1').json()
    except Exception:
        return None, None
    if not scores:
        return None, None
    s = scores[0]
    bm, bs = s.get('beatmap', {}), s.get('beatmapset', {})
    mods = ''.join(s.get('mods', [])) or 'None'
    play = {
        'artist': bs.get('artist', '?'), 'title': bs.get('title', '?'),
        'diff': bm.get('version', '?'), 'sr': bm.get('difficulty_rating', '?'),
        'ar': bm.get('ar', '?'), 'cs': bm.get('cs', '?'),
        'od': bm.get('accuracy', '?'), 'hp': bm.get('drain', '?'),
        'bpm': bm.get('bpm', '?'), 'creator': bs.get('creator', '?'),
        'id': bm.get('id', ''), 'url': bm.get('url', ''), 'mods': mods,
    }
    # how long ago was this play? parse the ISO 'created_at' timestamp
    mins = None
    try:
        from datetime import datetime, timezone
        ts = s.get('created_at', '').replace('Z', '+00:00')
        played = datetime.fromisoformat(ts)
        mins = (datetime.now(timezone.utc) - played).total_seconds() / 60
    except Exception:
        pass
    return mins, play


# ---------- template rendering ----------
class SafeDict(dict):
    def __missing__(self, key):
        return '?'

def render_np(template, play):
    data = dict(play)
    data.setdefault('url', f"https://osu.ppy.sh/b/{data.get('id', '')}")
    try:
        data['sr'] = f"{float(data['sr']):.2f}"
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return template.format_map(SafeDict(data))
    except Exception:
        return f"{data.get('artist','?')} - {data.get('title','?')} [{data.get('diff','?')}]"


# ---------- the bot ----------
class Bot(commands.Bot):
    def __init__(self, tokens, bot_id, channels):
        self.tokens = tokens
        self.bot_id = bot_id
        self.joined = set(channels)
        PAIRS.update(load_pairs())
        super().__init__(token=tokens['access'], prefix='!', initial_channels=channels)

    async def event_ready(self):
        print(f'✅ {self.nick} online | joined {len(self.joined)} channels')
        self.loop.create_task(self.poll_modded())
        self.loop.create_task(self.start_web())

    # --- !np and friends ---
    @commands.command(name='np')
    async def np(self, ctx, arg: str = None):
        channel = ctx.channel.name

        if arg in ('help', '?'):
            await ctx.send(f"🎶 !np shows the streamer's current osu! map. Want it in your own chat? {HELP_URL}")
            return

        if arg in ('on', 'off'):
            if not (ctx.author.is_mod or ctx.author.is_broadcaster):
                return
            set_enabled(channel, 1 if arg == 'on' else 0)
            await ctx.send(f"!np is now {arg}.")
            return

        s = get_settings(channel)
        if s and s['enabled'] == 0:
            await ctx.send("💤 !np is currently offline.")
            return

        template = s['np_template'] if s and s['np_template'] else (
            "🎶 {artist} - {title} [{diff}] | ⭐ {sr} | Mods: {mods} | {url}")

        # 1) live agent data (tosu) takes priority
        entry = STATE.get(channel)
        if entry and time.time() - entry['ts'] <= TTL:
            await ctx.send(render_np(template, entry['np']))
            return

        # 2) instant mode: osu! API — a recent play means "currently playing"
        osu_id = get_osu_id(channel)
        if osu_id and OSU_CLIENT_ID:
            mins, play = osu_last_play(osu_id)
            if play and mins is not None and mins <= NP_FRESH_MINS:
                await ctx.send(render_np(template, play))
            else:
                await ctx.send("😴 The streamer is currently offline — wait for them to be online!")
            return

        await ctx.send("😴 The streamer is currently offline — wait for them to be online!")

    # --- auto-join channels where we get modded; leave removed ones ---
    async def poll_modded(self):
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                self.tokens = refresh_tokens(self.tokens)
                modded = set(fetch_modded_channels(self.tokens['access'], self.bot_id))
                removed = get_removed()
                wanted = modded - removed          # modded, minus ones the user removed

                to_join = wanted - self.joined
                to_part = self.joined - wanted     # unmodded OR removed via dashboard

                if to_join:
                    await self.join_channels(list(to_join))
                    for ch in to_join:
                        mark_joined(ch)
                    self.joined |= to_join
                    PAIRS.update(load_pairs())
                    print(f'➕ joined: {sorted(to_join)}')

                if to_part:
                    await self.part_channels(list(to_part))
                    self.joined -= to_part
                    print(f'➖ left: {sorted(to_part)}')
            except Exception as e:
                print('poll error:', e)

    # --- agent push endpoint ---
    async def start_web(self):
        app = web.Application()
        app.router.add_post('/update', self.handle_update)
        app.router.add_get('/api/settings', self.handle_get_settings)
        app.router.add_post('/api/settings', self.handle_set_settings)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', UPDATE_PORT).start()
        print(f'🌐 /update listening on 127.0.0.1:{UPDATE_PORT}')

    async def handle_update(self, request):
        channel = PAIRS.get(request.headers.get('X-Pair-Token'))
        if not channel:
            return web.Response(status=403, text='bad token')
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text='bad json')
        STATE[channel] = {'np': data, 'ts': time.time()}
        return web.Response(text='ok')

    def _auth(self, request):
        """Resolve a pair token to its own channel (reload pairs if unknown)."""
        token = request.headers.get('X-Pair-Token')
        channel = PAIRS.get(token)
        if channel is None:
            PAIRS.update(load_pairs())
            channel = PAIRS.get(token)
        return channel

    async def handle_get_settings(self, request):
        channel = self._auth(request)
        if not channel:
            return web.json_response({'error': 'bad token'}, status=403)
        s = get_settings(channel)
        return web.json_response({
            'channel': channel,
            'enabled': bool(s['enabled']) if s else True,
            'np_template': (s['np_template'] if s and s['np_template'] else ''),
        })

    async def handle_set_settings(self, request):
        channel = self._auth(request)
        if not channel:
            return web.json_response({'error': 'bad token'}, status=403)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'bad json'}, status=400)
        # a token may only ever change its OWN channel
        if 'enabled' in data:
            set_enabled(channel, 1 if data['enabled'] else 0)
        if 'np_template' in data and isinstance(data['np_template'], str):
            set_template(channel, data['np_template'][:400])
        s = get_settings(channel)
        return web.json_response({
            'ok': True,
            'enabled': bool(s['enabled']) if s else True,
            'np_template': (s['np_template'] if s and s['np_template'] else ''),
        })


def main():
    tokens = refresh_tokens(load_tokens())          # start with a fresh token
    bot_id, bot_login = fetch_self(tokens['access'])
    print(f'🤖 bot account: {bot_login} ({bot_id})')
    modded = set(fetch_modded_channels(tokens['access'], bot_id))
    channels = sorted(modded - get_removed())        # skip user-removed channels
    for ch in channels:
        mark_joined(ch)
    print(f'📋 active in {len(channels)} channels: {channels}')
    Bot(tokens, bot_id, channels).run()


if __name__ == '__main__':
    main()