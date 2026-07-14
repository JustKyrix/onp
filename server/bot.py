import os
import json
import time
import random
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
SITE_URL      = os.environ.get('SITE_URL', 'https://onp.artline-studio.de').rstrip('/')
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



def osu_recent(osu_id):
    """Richer recent-score details for the !rs command."""
    try:
        scores = osu_get(
            f'users/{osu_id}/scores/recent?include_fails=1&mode=osu&limit=1').json()
    except Exception:
        return None
    if not scores:
        return None
    s = scores[0]
    bs, bm = s.get('beatmapset', {}), s.get('beatmap', {})
    return {
        'artist': bs.get('artist', '?'), 'title': bs.get('title', '?'),
        'diff': bm.get('version', '?'),
        'rank': s.get('rank', '?'),
        'acc': round((s.get('accuracy') or 0) * 100, 2),
        'pp': round(s.get('pp') or 0, 2),
        'combo': s.get('max_combo', '?'),
        'mods': ''.join(s.get('mods', [])) or 'None',
        'url': bm.get('url', ''),
    }


def osu_user(osu_id):
    """Profile stats for the !stats command."""
    try:
        u = osu_get(f'users/{osu_id}/osu').json()
    except Exception:
        return None
    st = u.get('statistics', {}) or {}
    lvl = (st.get('level') or {}).get('current')
    return {
        'username': u.get('username', '?'),
        'pp': round(st.get('pp', 0) or 0),
        'rank': st.get('global_rank'),
        'crank': st.get('country_rank'),
        'country': u.get('country_code', ''),
        'acc': round(st.get('hit_accuracy', 0) or 0, 2),
        'playcount': st.get('play_count', 0),
        'level': lvl,
    }


def twitch_stream(token, login):
    try:
        d = helix('streams', token, {'user_login': login}).json().get('data', [])
        return d[0] if d else None
    except Exception:
        return None


def twitch_user(token, login):
    try:
        d = helix('users', token, {'login': login}).json().get('data', [])
        return d[0] if d else None
    except Exception:
        return None


def twitch_channel_info(token, bid):
    try:
        d = helix('channels', token, {'broadcaster_id': bid}).json().get('data', [])
        return d[0] if d else None
    except Exception:
        return None


# ---------- command system (DB-driven) ----------
def list_commands(channel):
    with db() as conn:
        try:
            return conn.execute(
                'SELECT name, type, kind, response, enabled, permission, cooldown '
                'FROM commands WHERE channel=?', (channel,)).fetchall()
        except sqlite3.OperationalError:
            return []


def list_skins(channel):
    with db() as conn:
        try:
            return conn.execute(
                'SELECT title, link FROM skins WHERE channel=? ORDER BY created_at DESC',
                (channel,)).fetchall()
        except sqlite3.OperationalError:
            return []


def get_tourney_row(channel):
    with db() as conn:
        try:
            return conn.execute(
                'SELECT name, status FROM tourneys WHERE channel=?', (channel,)).fetchone()
        except sqlite3.OperationalError:
            return None


def get_so_template(channel):
    with db() as conn:
        try:
            row = conn.execute('SELECT so_template FROM channels WHERE channel=?',
                               (channel,)).fetchone()
            return row['so_template'] if row else None
        except sqlite3.OperationalError:
            return None


import re as _re

def parse_beatmap_id(text):
    """Pull a beatmap (difficulty) id out of a link or raw id."""
    text = text.strip()
    # beatmapsets/456#osu/123  ->  123 (the diff id after the mode)
    m = _re.search(r'#\w+/(\d+)', text)
    if m:
        return m.group(1)
    # /b/123  or  /beatmaps/123
    m = _re.search(r'/b(?:eatmaps)?/(\d+)', text)
    if m:
        return m.group(1)
    # bare number
    if text.isdigit():
        return text
    # last number anywhere as a fallback
    m = _re.search(r'(\d+)\D*$', text)
    return m.group(1) if m else None


def osu_beatmap(bid):
    try:
        b = osu_get(f'beatmaps/{bid}').json()
    except Exception:
        return None
    if not b or 'id' not in b:
        return None
    bs = b.get('beatmapset', {}) or {}
    return {
        'beatmap_id': str(b.get('id', bid)),
        'title': bs.get('title', '?'),
        'artist': bs.get('artist', '?'),
        'version': b.get('version', '?'),
        'stars': round(b.get('difficulty_rating', 0) or 0, 2),
        'url': b.get('url', f'https://osu.ppy.sh/b/{bid}'),
    }


def add_request(channel, bm, requested_by):
    with db() as conn:
        conn.execute(
            'INSERT INTO requests (channel, beatmap_id, title, artist, version, '
            'stars, url, requested_by) VALUES (?,?,?,?,?,?,?,?)',
            (channel, bm['beatmap_id'], bm['title'], bm['artist'], bm['version'],
             str(bm['stars']), bm['url'], requested_by))
        conn.commit()


def get_command(channel, name):
    with db() as conn:
        try:
            return conn.execute(
                'SELECT name, type, kind, response, enabled, permission, cooldown '
                'FROM commands WHERE channel=? AND name=?', (channel, name)).fetchone()
        except sqlite3.OperationalError:
            return None


_COOLDOWNS = {}   # (channel, name) -> last-used epoch


def _on_cooldown(channel, name, secs):
    key = (channel, name)
    now = time.time()
    last = _COOLDOWNS.get(key, 0)
    if now - last < (secs or 0):
        return True
    _COOLDOWNS[key] = now
    return False


EIGHTBALL = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.",
    "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Don't count on it.", "My reply is no.",
    "Very doubtful.", "Outlook not so good.",
]

DUEL_WINS = [
    "{w} lands a clean 300 on {l}'s forehead. GG.",
    "{w} FCs the duel. {l} chokes at 99%.",
    "{w} sends {l} straight back to the lobby.",
    "{w} hits every note. {l} missed the first circle.",
    "{w} wins. {l} blames their tablet area.",
    "{w} destroys {l} with a single spinner.",
    "{w} takes it! {l} says the map was overrated anyway.",
    "{w} snipes {l} by 2pp. Brutal.",
]

HUGS = [
    "{a} wraps {b} in the warmest hug \U0001F42C",
    "{a} gives {b} a big squeezy hug \u2661",
    "{a} tackles {b} with a hug!",
    "{a} hugs {b} so tight they nearly drop combo",
    "{a} offers {b} a comfy hug \U0001F9F8",
]

PATS = [
    "{a} pats {b} gently on the head \u2661",
    "{a} gives {b} some well-deserved headpats",
    "{a} pat pat pats {b}",
    "{a} pats {b}. {b} feels better already.",
    "{a} headpats {b} for that clutch FC",
]

RPS_EMOJI = {'rock': '\U0001FAA8', 'paper': '\U0001F4C4', 'scissors': '\u2702\uFE0F'}
RPS_BEATS = {'rock': 'scissors', 'paper': 'rock', 'scissors': 'paper'}



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

    async def event_command_error(self, ctx, error):
        # dynamic (DB) commands aren't registered as twitchio commands; ignore "not found"
        if isinstance(error, commands.CommandNotFound):
            return
        print('command error:', error)

    async def event_message(self, message):
        if message.echo:
            return
        # let decorated commands (!np) run first
        await self.handle_commands(message)
        content = (message.content or '').strip()
        if not content.startswith('!'):
            return
        parts = content[1:].split()
        if not parts:
            return
        name = parts[0].lower()
        if name == 'np':          # handled by the @command decorator
            return
        await self.dispatch_command(message, name, parts[1:])

    async def dispatch_command(self, message, name, args):
        channel = message.channel.name
        cmd = get_command(channel, name)
        if not cmd or not cmd['enabled']:
            return
        author = message.author
        is_mod = bool(getattr(author, 'is_mod', False))
        is_bc = bool(getattr(author, 'is_broadcaster', False))
        is_sub = bool(getattr(author, 'is_subscriber', False))
        perm = cmd['permission']
        if perm == 'mods' and not (is_mod or is_bc):
            return
        if perm == 'subs' and not (is_sub or is_mod or is_bc):
            return
        if _on_cooldown(channel, name, cmd['cooldown']):
            return

        kind = cmd['kind']
        try:
            if kind == 'text':
                if cmd['response']:
                    await message.channel.send(cmd['response'])
            elif kind == '8ball':
                q = ' '.join(args)
                await message.channel.send(f"🎱 {random.choice(EIGHTBALL)}")
            elif kind == 'coinflip':
                await message.channel.send(f"🪙 {random.choice(['Heads', 'Tails'])}!")
            elif kind == 'duel':
                if not args:
                    await message.channel.send("Usage: !duel <user>")
                    return
                target = args[0].lstrip('@')
                if target.lower() == author.name.lower():
                    await message.channel.send(f"{author.name} duels themselves. Somehow, they lose.")
                    return
                fighters = [author.name, target]
                random.shuffle(fighters)
                w, l = fighters
                await message.channel.send("⚔️ " + random.choice(DUEL_WINS).format(w=w, l=l))
            elif kind == 'rps':
                if not args or args[0].lower() not in RPS_BEATS:
                    await message.channel.send("Usage: !rps rock | paper | scissors")
                    return
                you = args[0].lower()
                me = random.choice(list(RPS_BEATS))
                if you == me:
                    res = "it's a tie!"
                elif RPS_BEATS[you] == me:
                    res = f"{author.name} wins!"
                else:
                    res = f"{author.name} loses!"
                await message.channel.send(f"{RPS_EMOJI[you]} vs {RPS_EMOJI[me]} — {res}")
            elif kind in ('hug', 'pat'):
                if not args:
                    await message.channel.send(f"Usage: !{name} <user>")
                    return
                target = args[0].lstrip('@')
                pool = HUGS if kind == 'hug' else PATS
                await message.channel.send(random.choice(pool).format(a=author.name, b=target))
            elif kind == 'roll':
                try:
                    hi = int(args[0]) if args else 100
                except ValueError:
                    hi = 100
                hi = max(1, min(hi, 1000000))
                await message.channel.send(f"🎲 {author.name} rolled {random.randint(1, hi)}")
            elif kind == 'recent':
                osu_id = get_osu_id(channel)
                if not osu_id:
                    await message.channel.send("This streamer hasn't linked an osu! account.")
                    return
                r = osu_recent(osu_id)
                if not r:
                    await message.channel.send("No recent plays found.")
                    return
                await message.channel.send(
                    f"🎮 Recent: {r['artist']} - {r['title']} [{r['diff']}] | "
                    f"{r['rank']} {r['acc']}% {r['combo']}x | {r['pp']}pp | "
                    f"Mods: {r['mods']} | {r['url']}")
            elif kind == 'stats':
                osu_id = get_osu_id(channel)
                if not osu_id:
                    await message.channel.send("This streamer hasn't linked an osu! account.")
                    return
                u = osu_user(osu_id)
                if not u:
                    await message.channel.send("Couldn't fetch stats right now.")
                    return
                rank = f"#{u['rank']:,}" if u['rank'] else "unranked"
                crank = f" ({u['country']} #{u['crank']:,})" if u['crank'] else ""
                await message.channel.send(
                    f"📊 {u['username']}: {u['pp']:,}pp | {rank}{crank} | "
                    f"{u['acc']}% acc | {u['playcount']:,} plays")
            elif kind == 'skin':
                skins = list_skins(channel)
                if not skins:
                    await message.channel.send("🎨 This streamer hasn't added any skins yet.")
                    return
                parts = []
                for s in skins:
                    # keep a space on both sides of the URL so Twitch auto-links it
                    parts.append(f"{s['title']}: {s['link']}" if s['link'] else s['title'])
                msg = "🎨 Skins — " + "  |  ".join(parts)
                await message.channel.send(msg[:490])
            elif kind == 'uptime':
                st = twitch_stream(self.tokens['access'], channel)
                if not st:
                    await message.channel.send(f"{channel} is offline.")
                    return
                from datetime import datetime, timezone
                started = datetime.fromisoformat(st['started_at'].replace('Z', '+00:00'))
                secs = int((datetime.now(timezone.utc) - started).total_seconds())
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                dur = []
                if h:
                    dur.append(f"{h}h")
                if m or h:
                    dur.append(f"{m}m")
                dur.append(f"{s}s")
                await message.channel.send(f"⏱️ {channel} has been live for {' '.join(dur)}")
            elif kind == 'shoutout':
                if not args:
                    await message.channel.send("Usage: !so <streamer>")
                    return
                target = args[0].lstrip('@').lower()
                u = twitch_user(self.tokens['access'], target)
                if not u:
                    await message.channel.send(f"Couldn't find @{target}.")
                    return
                info = twitch_channel_info(self.tokens['access'], u['id'])
                data = {
                    'name': '@' + u['display_name'],
                    'link': f"https://twitch.tv/{u['login']}",
                    'game': (info.get('game_name') if info else '') or 'something',
                    'title': (info.get('title') if info else '') or '',
                }
                tpl = get_so_template(channel) or (
                    "📢 Go show {name} some love at {link} — they were last streaming {game}!")
                try:
                    out = tpl.format_map(SafeDict(data))
                except Exception:
                    out = f"📢 Go show {data['name']} some love at {data['link']}"
                await message.channel.send(out[:490])
            elif kind == 'request':
                if not args:
                    await message.channel.send("Usage: !request <beatmap link or id>")
                    return
                bid = parse_beatmap_id(args[0])
                if not bid:
                    await message.channel.send("Couldn't read that beatmap link — try the map's osu! URL.")
                    return
                bm = osu_beatmap(bid)
                if not bm:
                    await message.channel.send("Couldn't find that beatmap.")
                    return
                add_request(channel, bm, author.name)
                await message.channel.send(
                    f"✅ Added to the queue: {bm['artist']} - {bm['title']} "
                    f"[{bm['version']}] (⭐{bm['stars']})")
            elif kind == 'help':
                await message.channel.send(
                    f"📖 All of {channel}'s commands: {SITE_URL}/c/{channel}")
            elif kind == 'tourney':
                t = get_tourney_row(channel)
                if not t or t['status'] == 'draft':
                    await message.channel.send("No tournament signups are open right now.")
                    return
                link = f"{SITE_URL}/t/{channel}"
                if t['status'] == 'open':
                    await message.channel.send(
                        f"🏆 {t['name']} — signups are OPEN! Join here: {link}")
                else:
                    await message.channel.send(
                        f"🏆 {t['name']} — signups are closed. Details: {link}")
        except Exception as e:
            print(f'dispatch error ({name}):', e)

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