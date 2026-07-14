import os
import re as _re
import secrets
import sqlite3
from functools import wraps
from urllib.parse import urlencode
from flask import (Flask, request, redirect, session,
                   render_template, url_for, flash, Response)
import requests
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename


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
REDIRECT_URI  = os.environ.get('WEB_REDIRECT_URI', 'http://localhost:5000/callback')
BOT_USERNAME  = os.environ.get('BOT_USERNAME', 'your_bot')
DB_PATH       = os.environ.get('DB_PATH', 'onp.db')

AUTH_URL  = 'https://id.twitch.tv/oauth2/authorize'
TOKEN_URL = 'https://id.twitch.tv/oauth2/token'
USERS_URL = 'https://api.twitch.tv/helix/users'

# --- osu! OAuth + API v2 ---
OSU_CLIENT_ID     = os.environ.get('OSU_CLIENT_ID')
OSU_CLIENT_SECRET = os.environ.get('OSU_CLIENT_SECRET')
OSU_REDIRECT_URI  = os.environ.get('OSU_REDIRECT_URI', 'http://localhost:5000/osu/callback')
OSU_AUTH_URL  = 'https://osu.ppy.sh/oauth/authorize'
OSU_TOKEN_URL = 'https://osu.ppy.sh/oauth/token'
OSU_ME_URL    = 'https://osu.ppy.sh/api/v2/me'

DEFAULT_TEMPLATE = ("\U0001F3B6 {artist} - {title} [{diff}] | \u2B50 {sr} "
                    "| AR:{ar} CS:{cs} OD:{od} HP:{hp} | Mods: {mods} | {url}")

DEFAULT_SO_TEMPLATE = ("\U0001F4E2 Go show {name} some love at {link} "
                       "— they were last streaming {game}!")

# placeholders shown in the UI + used by the live preview
PLACEHOLDERS = ['artist', 'title', 'diff', 'sr', 'ar', 'cs', 'od', 'hp',
                'mods', 'bpm', 'creator', 'id', 'url']

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', secrets.token_hex(16))
# behind Plesk/nginx reverse proxy: trust X-Forwarded-* so Flask knows it's HTTPS
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# skin preview image uploads
SKIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'skins')
os.makedirs(SKIN_DIR, exist_ok=True)
ALLOWED_IMG = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4 MB cap on uploads


def _allowed_img(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMG


# ---------- database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                channel      TEXT PRIMARY KEY,
                twitch_id    TEXT UNIQUE,
                pair_token   TEXT UNIQUE,
                np_template  TEXT,
                enabled      INTEGER DEFAULT 1,
                bot_joined   INTEGER DEFAULT 0,
                removed      INTEGER DEFAULT 0,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # self-healing migrations for older DBs
        cols = [r['name'] for r in conn.execute('PRAGMA table_info(channels)')]
        if 'removed' not in cols:
            conn.execute('ALTER TABLE channels ADD COLUMN removed INTEGER DEFAULT 0')
        if 'user_token' not in cols:
            conn.execute('ALTER TABLE channels ADD COLUMN user_token TEXT')
        if 'user_refresh' not in cols:
            conn.execute('ALTER TABLE channels ADD COLUMN user_refresh TEXT')
        if 'osu_id' not in cols:
            conn.execute('ALTER TABLE channels ADD COLUMN osu_id TEXT')
        if 'osu_username' not in cols:
            conn.execute('ALTER TABLE channels ADD COLUMN osu_username TEXT')
        if 'so_template' not in cols:
            conn.execute('ALTER TABLE channels ADD COLUMN so_template TEXT')

        # command system: every command a channel has is a row here
        conn.execute('''
            CREATE TABLE IF NOT EXISTS commands (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel     TEXT NOT NULL,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL DEFAULT 'custom',
                kind        TEXT NOT NULL DEFAULT 'text',
                response    TEXT,
                enabled     INTEGER DEFAULT 1,
                permission  TEXT DEFAULT 'anyone',
                cooldown    INTEGER DEFAULT 5,
                UNIQUE(channel, name)
            )
        ''')
        # skins: a channel's showcase of skins (info + link, optional image)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS skins (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel     TEXT NOT NULL,
                title       TEXT NOT NULL,
                description TEXT,
                image       TEXT,
                link        TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # tournament: one per channel (UNIQUE channel)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tourneys (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel    TEXT NOT NULL UNIQUE,
                name       TEXT DEFAULT 'Tournament',
                status     TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # tournament players (signed up via osu! login)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tourney_players (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                channel      TEXT NOT NULL,
                osu_id       TEXT NOT NULL,
                osu_username TEXT,
                rank         INTEGER,
                pp           INTEGER,
                joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel, osu_id)
            )
        ''')
        # tournament mappool
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tourney_maps (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel    TEXT NOT NULL,
                beatmap_id TEXT,
                title      TEXT,
                artist     TEXT,
                version    TEXT,
                stars      TEXT,
                url        TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # beatmap requests queue
        conn.execute('''
            CREATE TABLE IF NOT EXISTS requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                channel      TEXT NOT NULL,
                beatmap_id   TEXT,
                title        TEXT,
                artist       TEXT,
                version      TEXT,
                stars        TEXT,
                url          TEXT,
                requested_by TEXT,
                status       TEXT DEFAULT 'pending',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # seed the default (built-in) commands for every existing channel
        for r in conn.execute('SELECT channel FROM channels').fetchall():
            seed_commands(conn, r['channel'])


# built-in commands seeded for every channel: (name, type, kind, response, enabled, permission)
DEFAULT_COMMANDS = [
    ('skin',     'osu', 'skin',     None, 1, 'anyone'),
    ('rs',       'osu', 'recent',   None, 1, 'anyone'),
    ('stats',    'osu', 'stats',    None, 1, 'anyone'),
    ('8ball',    'fun', '8ball',    None, 1, 'anyone'),
    ('roll',     'fun', 'roll',     None, 1, 'anyone'),
    ('coinflip', 'fun', 'coinflip', None, 1, 'anyone'),
    ('duel',     'fun', 'duel',     None, 1, 'anyone'),
    ('rps',      'fun', 'rps',      None, 1, 'anyone'),
    ('hug',      'fun', 'hug',      None, 1, 'anyone'),
    ('pat',      'fun', 'pat',      None, 1, 'anyone'),
    ('uptime',   'utility', 'uptime',   None, 1, 'anyone'),
    ('so',       'utility', 'shoutout', None, 1, 'mods'),
    ('request',  'utility', 'request',  None, 1, 'anyone'),
    ('tourney',  'utility', 'tourney',  None, 1, 'anyone'),
    ('help',     'utility', 'help',     None, 1, 'anyone'),
]


def seed_commands(conn, channel):
    for name, type_, kind, resp, en, perm in DEFAULT_COMMANDS:
        conn.execute(
            'INSERT OR IGNORE INTO commands '
            '(channel, name, type, kind, response, enabled, permission) '
            'VALUES (?,?,?,?,?,?,?)',
            (channel, name, type_, kind, resp, en, perm))


def get_channel(twitch_id):
    with db() as conn:
        return conn.execute('SELECT * FROM channels WHERE twitch_id=?',
                            (twitch_id,)).fetchone()


# ---------- auth helpers ----------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if 'twitch_id' not in session:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrapper


@app.context_processor
def inject_globals():
    return {'bot_username': BOT_USERNAME,
            'logged_in': 'twitch_id' in session,
            'channel_name': session.get('channel')}


# ---------- osu! app token + beatmap lookup (for the tourney mappool) ----------
_OSU_APP_TOKEN = {'token': None, 'exp': 0}


def osu_app_token():
    """Client-credentials token for public osu! API reads (beatmap info)."""
    import time as _t
    if _OSU_APP_TOKEN['token'] and _t.time() < _OSU_APP_TOKEN['exp'] - 60:
        return _OSU_APP_TOKEN['token']
    try:
        r = requests.post('https://osu.ppy.sh/oauth/token', json={
            'client_id': OSU_CLIENT_ID, 'client_secret': OSU_CLIENT_SECRET,
            'grant_type': 'client_credentials', 'scope': 'public',
        }, timeout=10).json()
    except Exception:
        return None
    if 'access_token' not in r:
        return None
    _OSU_APP_TOKEN['token'] = r['access_token']
    _OSU_APP_TOKEN['exp'] = _t.time() + r.get('expires_in', 3600)
    return _OSU_APP_TOKEN['token']


def parse_beatmap_id(text):
    """Pull a beatmap (difficulty) id out of an osu! link or raw id."""
    text = (text or '').strip()
    m = _re.search(r'#\w+/(\d+)', text)            # beatmapsets/123#osu/456
    if m:
        return m.group(1)
    m = _re.search(r'/b(?:eatmaps)?/(\d+)', text)   # /b/456 or /beatmaps/456
    if m:
        return m.group(1)
    if text.isdigit():
        return text
    m = _re.search(r'(\d+)\D*$', text)
    return m.group(1) if m else None


def osu_beatmap_info(bid):
    tok = osu_app_token()
    if not tok:
        return None
    try:
        b = requests.get(f'https://osu.ppy.sh/api/v2/beatmaps/{bid}',
                         headers={'Authorization': f'Bearer {tok}'},
                         timeout=10).json()
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
        'stars': f"{round(b.get('difficulty_rating', 0) or 0, 2)}",
        'url': b.get('url', f'https://osu.ppy.sh/b/{bid}'),
    }


# ---------- twitch moderator management ----------
_BOT_ID = None

def get_bot_id():
    """Resolve the bot account's user id once (via an app access token)."""
    global _BOT_ID
    if _BOT_ID:
        return _BOT_ID
    app_tok = requests.post(TOKEN_URL, data={
        'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'grant_type': 'client_credentials',
    }, timeout=10).json().get('access_token')
    u = requests.get(USERS_URL, headers={
        'Client-Id': CLIENT_ID, 'Authorization': f'Bearer {app_tok}',
    }, params={'login': BOT_USERNAME}, timeout=10).json()
    _BOT_ID = u['data'][0]['id']
    return _BOT_ID


def refresh_user_token(channel, refresh):
    r = requests.post(TOKEN_URL, data={
        'grant_type': 'refresh_token', 'refresh_token': refresh,
        'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
    }, timeout=10).json()
    if 'access_token' not in r:
        return None
    with db() as conn:
        conn.execute('UPDATE channels SET user_token=?, user_refresh=? WHERE channel=?',
                    (r['access_token'], r.get('refresh_token', refresh), channel))
    return r['access_token']


def unmod_bot(row):
    """Remove the bot as a moderator, acting as the broadcaster. Returns (ok, msg)."""
    token = row['user_token']
    if not token:
        return False, 'reauth'          # logged in before we had the scope
    bot_id = get_bot_id()

    def call(tok):
        return requests.delete('https://api.twitch.tv/helix/moderation/moderators',
            headers={'Client-Id': CLIENT_ID, 'Authorization': f'Bearer {tok}'},
            params={'broadcaster_id': row['twitch_id'], 'user_id': bot_id}, timeout=10)

    resp = call(token)
    if resp.status_code == 401 and row['user_refresh']:      # token expired -> refresh once
        token = refresh_user_token(row['channel'], row['user_refresh'])
        if token:
            resp = call(token)
    # 204 = removed. 400 = it wasn't a mod anyway. Both mean "not modded now".
    if resp.status_code in (204, 400):
        return True, 'ok'
    if resp.status_code == 401:
        return False, 'reauth'
    return False, f'twitch error {resp.status_code}'


# ---------- routes ----------
SITE_URL = 'https://onp.artline-studio.de'


@app.route('/robots.txt')
def robots():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /dashboard\n"
        "Disallow: /settings\n"
        "Disallow: /callback\n"
        "Disallow: /osu/\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap():
    # only real, crawlable pages (anchors like /#features are the same URL)
    pages = ['/']
    urls = ''.join(
        f'  <url><loc>{SITE_URL}{p}</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n'
        for p in pages)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'{urls}'
        '</urlset>\n'
    )
    return Response(xml, mimetype='application/xml')


@app.route('/')
def index():
    return render_template('index.html', placeholders=PLACEHOLDERS)


@app.route('/login')
def login():
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = {
        'client_id': CLIENT_ID,
        'redirect_uri': REDIRECT_URI,
        'response_type': 'code',
        'scope': 'channel:manage:moderators',   # lets us unmod the bot as the user
        'state': state,
    }
    return redirect(f'{AUTH_URL}?{urlencode(params)}')


@app.route('/callback')
def callback():
    if request.args.get('state') != session.pop('oauth_state', None):
        return 'State mismatch, start again.', 400
    code = request.args.get('code')
    if not code:
        return redirect(url_for('index'))

    tok = requests.post(TOKEN_URL, data={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': REDIRECT_URI,
    }, timeout=10).json()
    access = tok.get('access_token')
    refresh = tok.get('refresh_token')
    if not access:
        return 'Login failed, try again.', 400

    user = requests.get(USERS_URL, headers={
        'Client-Id': CLIENT_ID,
        'Authorization': f'Bearer {access}',
    }, timeout=10).json()['data'][0]
    twitch_id, channel = user['id'], user['login']

    # Reconcile by channel: the bot may have already created a bare row for this
    # channel (bot_joined=1, no twitch_id/token) if they modded it before logging in.
    with db() as conn:
        row = conn.execute('SELECT channel FROM channels WHERE channel=?',
                           (channel,)).fetchone()
        if not row:
            conn.execute(
                'INSERT INTO channels (channel, twitch_id, pair_token, np_template) '
                'VALUES (?,?,?,?)',
                (channel, twitch_id, secrets.token_urlsafe(24), DEFAULT_TEMPLATE))
            seed_commands(conn, channel)
        else:
            # fill in the bits the bot couldn't know; don't clobber existing values
            conn.execute(
                'UPDATE channels SET twitch_id=?, '
                'pair_token=COALESCE(pair_token, ?), '
                'np_template=COALESCE(np_template, ?) '
                'WHERE channel=?',
                (twitch_id, secrets.token_urlsafe(24), DEFAULT_TEMPLATE, channel))
        # store the user's OAuth tokens so we can unmod the bot on their behalf
        conn.execute('UPDATE channels SET user_token=?, user_refresh=? WHERE channel=?',
                    (access, refresh, channel))

    session['twitch_id'] = twitch_id
    session['channel'] = channel
    return redirect(url_for('dashboard'))


@app.route('/status')
@login_required
def status():
    row = get_channel(session['twitch_id'])
    joined = bool(row['bot_joined']) if row else False
    return {'bot_joined': joined}


@app.route('/osu/login')
@login_required
def osu_login():
    state = secrets.token_urlsafe(16)
    session['osu_state'] = state
    params = {
        'client_id': OSU_CLIENT_ID,
        'redirect_uri': OSU_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
    }
    return redirect(f'{OSU_AUTH_URL}?{urlencode(params)}')


@app.route('/osu/callback')
def osu_callback():
    if request.args.get('state') != session.pop('osu_state', None):
        return 'State mismatch, start again.', 400
    code = request.args.get('code')
    join_channel = session.pop('tourney_join', None)
    if not code:
        return redirect(url_for('tourney_page', channel=join_channel)
                        if join_channel else url_for('dashboard'))
    if not join_channel and 'twitch_id' not in session:
        return redirect(url_for('login'))

    tok = requests.post(OSU_TOKEN_URL, json={
        'client_id': OSU_CLIENT_ID,
        'client_secret': OSU_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': OSU_REDIRECT_URI,
    }, timeout=10).json()
    access = tok.get('access_token')
    if not access:
        if join_channel:
            return redirect(url_for('tourney_page', channel=join_channel))
        flash('osu! link failed, try again.')
        return redirect(url_for('dashboard'))

    me = requests.get(OSU_ME_URL, headers={
        'Authorization': f'Bearer {access}'}, timeout=10).json()
    osu_id, osu_name = str(me.get('id', '')), me.get('username', '')
    if not osu_id:
        if join_channel:
            return redirect(url_for('tourney_page', channel=join_channel))
        flash('Could not read your osu! account.')
        return redirect(url_for('dashboard'))

    # --- player joining a tournament ---
    if join_channel:
        st = me.get('statistics', {}) or {}
        session['player_osu_id'] = osu_id
        session['player_osu_name'] = osu_name
        t = get_tourney(join_channel)
        if not t or t['status'] != 'open':
            return redirect(url_for('tourney_page', channel=join_channel))
        with db() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO tourney_players '
                '(channel, osu_id, osu_username, rank, pp) VALUES (?,?,?,?,?)',
                (join_channel, osu_id, osu_name,
                 st.get('global_rank'), round(st.get('pp', 0) or 0)))
            conn.commit()
        return redirect(url_for('tourney_page', channel=join_channel))

    with db() as conn:
        conn.execute('UPDATE channels SET osu_id=?, osu_username=? WHERE twitch_id=?',
                    (osu_id, osu_name, session['twitch_id']))
    flash(f'osu! account linked: {osu_name}')
    return redirect(url_for('dashboard'))


@app.route('/osu/unlink', methods=['POST'])
@login_required
def osu_unlink():
    with db() as conn:
        conn.execute('UPDATE channels SET osu_id=NULL, osu_username=NULL WHERE twitch_id=?',
                    (session['twitch_id'],))
    flash('osu! account unlinked.')
    return redirect(url_for('dashboard'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    row = get_channel(session['twitch_id'])
    cmds = []
    if row:
        with db() as conn:
            try:
                cmds = conn.execute(
                    'SELECT name, type, kind, response, enabled, permission, cooldown '
                    'FROM commands WHERE channel=? ORDER BY '
                    "CASE type WHEN 'osu' THEN 0 WHEN 'fun' THEN 1 "
                    "WHEN 'utility' THEN 2 ELSE 3 END, name",
                    (row['channel'],)).fetchall()
            except sqlite3.OperationalError:
                cmds = []
    skins = []
    if row:
        with db() as conn:
            try:
                skins = conn.execute(
                    'SELECT id, title, description, image, link FROM skins '
                    'WHERE channel=? ORDER BY created_at DESC',
                    (row['channel'],)).fetchall()
            except sqlite3.OperationalError:
                skins = []
    reqs = []
    if row:
        with db() as conn:
            try:
                reqs = conn.execute(
                    'SELECT id, beatmap_id, title, artist, version, stars, url, '
                    'requested_by FROM requests '
                    "WHERE channel=? AND status='pending' ORDER BY created_at ASC",
                    (row['channel'],)).fetchall()
            except sqlite3.OperationalError:
                reqs = []
    tourney = get_tourney(row['channel']) if row else None
    tmaps = list_tourney_maps(row['channel']) if row else []
    tplayers = list_tourney_players(row['channel']) if row else []
    return render_template('dashboard.html', row=row,
                           commands=[c for c in cmds if c['type'] != 'custom'],
                           custom_commands=[c for c in cmds if c['type'] == 'custom'],
                           skins=skins, default_so=DEFAULT_SO_TEMPLATE, requests=reqs,
                           tourney=tourney, tmaps=tmaps, tplayers=tplayers)


@app.route('/skins/add', methods=['POST'])
@login_required
def skin_add():
    row = get_channel(session['twitch_id'])
    if not row:
        return redirect(url_for('dashboard'))
    title = (request.form.get('title') or '').strip()[:80]
    if not title:
        flash('A skin needs at least a title.')
        return redirect(url_for('dashboard'))
    description = (request.form.get('description') or '').strip()[:300]
    link = (request.form.get('link') or '').strip()[:400]

    image_rel = None
    file = request.files.get('image')
    if file and file.filename:
        if not _allowed_img(file.filename):
            flash('Image must be a png, jpg, gif or webp.')
            return redirect(url_for('dashboard'))
        ext = file.filename.rsplit('.', 1)[1].lower()
        fname = f"{secrets.token_hex(8)}.{ext}"
        file.save(os.path.join(SKIN_DIR, fname))
        image_rel = f"skins/{fname}"

    with db() as conn:
        conn.execute(
            'INSERT INTO skins (channel, title, description, image, link) '
            'VALUES (?,?,?,?,?)',
            (row['channel'], title, description, image_rel, link))
        conn.commit()
    return redirect(url_for('dashboard') + '#skinopen')


@app.route('/skins/delete', methods=['POST'])
@login_required
def skin_delete():
    row = get_channel(session['twitch_id'])
    sid = request.form.get('id')
    if row and sid:
        with db() as conn:
            s = conn.execute('SELECT image FROM skins WHERE id=? AND channel=?',
                             (sid, row['channel'])).fetchone()
            if s:
                if s['image']:
                    try:
                        os.remove(os.path.join(os.path.dirname(SKIN_DIR),
                                               s['image']))
                    except OSError:
                        pass
                conn.execute('DELETE FROM skins WHERE id=? AND channel=?',
                             (sid, row['channel']))
                conn.commit()
    return redirect(url_for('dashboard'))


@app.route('/command/toggle', methods=['POST'])
@login_required
def command_toggle():
    name = (request.form.get('name') or request.json.get('name') if request.is_json
            else request.form.get('name'))
    if not name:
        return {'error': 'no name'}, 400
    row = get_channel(session['twitch_id'])
    if not row:
        return {'error': 'no channel'}, 403
    with db() as conn:
        cur = conn.execute(
            'SELECT enabled FROM commands WHERE channel=? AND name=?',
            (row['channel'], name)).fetchone()
        if not cur:
            return {'error': 'no command'}, 404
        new = 0 if cur['enabled'] else 1
        conn.execute('UPDATE commands SET enabled=? WHERE channel=? AND name=?',
                     (new, row['channel'], name))
        conn.commit()
    return {'ok': True, 'name': name, 'enabled': bool(new)}


# names that can't be used for custom commands (built-ins + np)
RESERVED_NAMES = {'np', 'skin', 'rs', 'recent', 'stats', '8ball', 'roll', 'coinflip',
                  'uptime', 'so', 'shoutout', 'request', 'duel', 'rps', 'hug', 'pat',
                  'tourney', 'help', 'commands'}


@app.route('/command/add', methods=['POST'])
@login_required
def command_add():
    row = get_channel(session['twitch_id'])
    if not row:
        return redirect(url_for('dashboard'))
    name = (request.form.get('name') or '').strip().lstrip('!').lower()[:25]
    response = (request.form.get('response') or '').strip()[:400]
    permission = request.form.get('permission', 'anyone')
    if permission not in ('anyone', 'subs', 'mods'):
        permission = 'anyone'

    if not name or not _re.fullmatch(r'[a-z0-9_]+', name):
        flash('Command name can only use letters, numbers and underscores.')
        return redirect(url_for('dashboard') + '#custom')
    if name in RESERVED_NAMES:
        flash(f'"!{name}" is a built-in command name — pick another.')
        return redirect(url_for('dashboard') + '#custom')
    if not response:
        flash('A custom command needs a response.')
        return redirect(url_for('dashboard') + '#custom')

    with db() as conn:
        exists = conn.execute(
            'SELECT 1 FROM commands WHERE channel=? AND name=?',
            (row['channel'], name)).fetchone()
        if exists:
            flash(f'You already have a "!{name}" command.')
            return redirect(url_for('dashboard') + '#custom')
        conn.execute(
            'INSERT INTO commands (channel, name, type, kind, response, enabled, permission) '
            "VALUES (?,?,'custom','text',?,1,?)",
            (row['channel'], name, response, permission))
        conn.commit()
    return redirect(url_for('dashboard') + '#custom')


def get_tourney(channel):
    """The channel's tournament row, creating a draft one on first use."""
    with db() as conn:
        try:
            row = conn.execute('SELECT * FROM tourneys WHERE channel=?',
                               (channel,)).fetchone()
            if not row:
                conn.execute('INSERT INTO tourneys (channel) VALUES (?)', (channel,))
                conn.commit()
                row = conn.execute('SELECT * FROM tourneys WHERE channel=?',
                                   (channel,)).fetchone()
            return row
        except sqlite3.OperationalError:
            return None


def list_tourney_maps(channel):
    with db() as conn:
        try:
            return conn.execute(
                'SELECT id, beatmap_id, title, artist, version, stars, url '
                'FROM tourney_maps WHERE channel=? ORDER BY created_at ASC',
                (channel,)).fetchall()
        except sqlite3.OperationalError:
            return []


@app.route('/c/<channel>')
def commands_page(channel):
    """Public list of a channel's commands — what's on and what's off."""
    channel = (channel or '').lower()
    with db() as conn:
        ch = conn.execute('SELECT channel FROM channels WHERE channel=?',
                          (channel,)).fetchone()
        if not ch:
            return render_template('commands.html', notfound=True,
                                   channel=channel), 404
        try:
            cmds = conn.execute(
                'SELECT name, type, kind, response, enabled, permission '
                'FROM commands WHERE channel=? ORDER BY '
                "CASE type WHEN 'osu' THEN 0 WHEN 'fun' THEN 1 "
                "WHEN 'utility' THEN 2 ELSE 3 END, name", (channel,)).fetchall()
            row = conn.execute('SELECT enabled FROM channels WHERE channel=?',
                               (channel,)).fetchone()
        except sqlite3.OperationalError:
            cmds, row = [], None
    return render_template('commands.html', channel=channel, commands=cmds,
                           np_enabled=bool(row['enabled']) if row else False)


@app.route('/t/<channel>')
def tourney_page(channel):
    """Public tournament page — players land here to sign up and see the pool."""
    channel = (channel or '').lower()
    with db() as conn:
        ch = conn.execute('SELECT channel FROM channels WHERE channel=?',
                          (channel,)).fetchone()
    if not ch:
        return render_template('tourney.html', notfound=True, channel=channel), 404
    t = get_tourney(channel)
    players = list_tourney_players(channel)
    me_id = session.get('player_osu_id')
    joined = any(p['osu_id'] == me_id for p in players) if me_id else False
    return render_template('tourney.html', channel=channel, tourney=t,
                           tmaps=list_tourney_maps(channel), players=players,
                           joined=joined, me_name=session.get('player_osu_name'))


@app.route('/t/<channel>/login')
def tourney_login(channel):
    """Send a player through osu! OAuth, remembering which tourney they're joining."""
    channel = (channel or '').lower()
    state = secrets.token_urlsafe(16)
    session['osu_state'] = state
    session['tourney_join'] = channel
    params = {
        'client_id': OSU_CLIENT_ID,
        'redirect_uri': OSU_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
    }
    return redirect(f'{OSU_AUTH_URL}?{urlencode(params)}')


def list_tourney_players(channel):
    with db() as conn:
        try:
            return conn.execute(
                'SELECT id, osu_id, osu_username, rank, pp FROM tourney_players '
                'WHERE channel=? ORDER BY joined_at ASC', (channel,)).fetchall()
        except sqlite3.OperationalError:
            return []


@app.route('/tourney/status', methods=['POST'])
@login_required
def tourney_status():
    row = get_channel(session['twitch_id'])
    status = request.form.get('status')
    if row and status in ('draft', 'open', 'closed'):
        get_tourney(row['channel'])
        with db() as conn:
            conn.execute('UPDATE tourneys SET status=? WHERE channel=?',
                         (status, row['channel']))
            conn.commit()
    return redirect(url_for('dashboard') + '#tourney')


@app.route('/tourney/player/remove', methods=['POST'])
@login_required
def tourney_player_remove():
    row = get_channel(session['twitch_id'])
    pid = request.form.get('id')
    if row and pid:
        with db() as conn:
            conn.execute('DELETE FROM tourney_players WHERE id=? AND channel=?',
                         (pid, row['channel']))
            conn.commit()
    return redirect(url_for('dashboard') + '#tourney')


@app.route('/tourney/map/add', methods=['POST'])
@login_required
def tourney_map_add():
    row = get_channel(session['twitch_id'])
    if not row:
        return redirect(url_for('dashboard'))
    link = (request.form.get('link') or '').strip()
    bid = parse_beatmap_id(link)
    if not bid:
        flash("Couldn't read that beatmap link.")
        return redirect(url_for('dashboard') + '#tourney')
    bm = osu_beatmap_info(bid)
    if not bm:
        flash("Couldn't find that beatmap on osu!.")
        return redirect(url_for('dashboard') + '#tourney')
    with db() as conn:
        dupe = conn.execute(
            'SELECT 1 FROM tourney_maps WHERE channel=? AND beatmap_id=?',
            (row['channel'], bm['beatmap_id'])).fetchone()
        if dupe:
            flash('That map is already in the pool.')
            return redirect(url_for('dashboard') + '#tourney')
        conn.execute(
            'INSERT INTO tourney_maps (channel, beatmap_id, title, artist, '
            'version, stars, url) VALUES (?,?,?,?,?,?,?)',
            (row['channel'], bm['beatmap_id'], bm['title'], bm['artist'],
             bm['version'], bm['stars'], bm['url']))
        conn.commit()
    return redirect(url_for('dashboard') + '#tourney')


@app.route('/tourney/map/delete', methods=['POST'])
@login_required
def tourney_map_delete():
    row = get_channel(session['twitch_id'])
    mid = request.form.get('id')
    if row and mid:
        with db() as conn:
            conn.execute('DELETE FROM tourney_maps WHERE id=? AND channel=?',
                         (mid, row['channel']))
            conn.commit()
    return redirect(url_for('dashboard') + '#tourney')


@app.route('/tourney/rename', methods=['POST'])
@login_required
def tourney_rename():
    row = get_channel(session['twitch_id'])
    if row:
        name = (request.form.get('name') or '').strip()[:80] or 'Tournament'
        get_tourney(row['channel'])
        with db() as conn:
            conn.execute('UPDATE tourneys SET name=? WHERE channel=?',
                         (name, row['channel']))
            conn.commit()
    return redirect(url_for('dashboard') + '#tourney')


@app.route('/requests/list')
@login_required
def requests_list():
    row = get_channel(session['twitch_id'])
    if not row:
        return {'requests': []}
    with db() as conn:
        try:
            rows = conn.execute(
                'SELECT id, beatmap_id, title, artist, version, stars, url, '
                'requested_by, created_at FROM requests '
                "WHERE channel=? AND status='pending' ORDER BY created_at ASC",
                (row['channel'],)).fetchall()
        except sqlite3.OperationalError:
            return {'requests': []}
    return {'requests': [dict(r) for r in rows]}


@app.route('/requests/done', methods=['POST'])
@login_required
def requests_done():
    row = get_channel(session['twitch_id'])
    rid = request.form.get('id')
    if row and rid:
        with db() as conn:
            conn.execute("UPDATE requests SET status='done' WHERE id=? AND channel=?",
                         (rid, row['channel']))
            conn.commit()
    return {'ok': True}


@app.route('/requests/clear', methods=['POST'])
@login_required
def requests_clear():
    row = get_channel(session['twitch_id'])
    if row:
        with db() as conn:
            conn.execute("UPDATE requests SET status='done' WHERE channel=? AND status='pending'",
                         (row['channel'],))
            conn.commit()
    return {'ok': True}


@app.route('/command/settings', methods=['POST'])
@login_required
def command_settings():
    row = get_channel(session['twitch_id'])
    if not row:
        return {'error': 'no channel'}, 403
    name = (request.form.get('name') or '').strip()
    permission = request.form.get('permission', 'anyone')
    if permission not in ('anyone', 'subs', 'mods'):
        permission = 'anyone'
    try:
        cooldown = max(0, min(int(request.form.get('cooldown', 5)), 3600))
    except (TypeError, ValueError):
        cooldown = 5
    with db() as conn:
        cur = conn.execute('SELECT 1 FROM commands WHERE channel=? AND name=?',
                           (row['channel'], name)).fetchone()
        if not cur:
            return {'error': 'no command'}, 404
        conn.execute('UPDATE commands SET permission=?, cooldown=? WHERE channel=? AND name=?',
                     (permission, cooldown, row['channel'], name))
        conn.commit()
    return {'ok': True, 'name': name, 'permission': permission, 'cooldown': cooldown}


@app.route('/command/so-template', methods=['POST'])
@login_required
def so_template_save():
    row = get_channel(session['twitch_id'])
    if not row:
        return redirect(url_for('dashboard'))
    tpl = (request.form.get('so_template') or '').strip()[:400] or DEFAULT_SO_TEMPLATE
    with db() as conn:
        conn.execute('UPDATE channels SET so_template=? WHERE twitch_id=?',
                     (tpl, session['twitch_id']))
        conn.commit()
    return redirect(url_for('dashboard') + '#soopen')


@app.route('/command/delete', methods=['POST'])
@login_required
def command_delete():
    row = get_channel(session['twitch_id'])
    name = (request.form.get('name') or '').strip()
    if row and name and name not in RESERVED_NAMES:
        with db() as conn:
            # only allow deleting custom commands, never built-ins
            conn.execute(
                "DELETE FROM commands WHERE channel=? AND name=? AND type='custom'",
                (row['channel'], name))
            conn.commit()
    return redirect(url_for('dashboard') + '#custom')


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    row = get_channel(session['twitch_id'])
    if request.method == 'POST':
        template = request.form.get('np_template', '').strip() or DEFAULT_TEMPLATE
        enabled = 1 if request.form.get('enabled') == 'on' else 0
        with db() as conn:
            conn.execute('UPDATE channels SET np_template=?, enabled=? WHERE twitch_id=?',
                        (template[:400], enabled, session['twitch_id']))
        flash('Saved')
        return redirect(url_for('settings'))
    return render_template('settings.html', row=row,
                           placeholders=PLACEHOLDERS, default_template=DEFAULT_TEMPLATE)


@app.route('/remove-bot', methods=['POST'])
@login_required
def remove_bot():
    row = get_channel(session['twitch_id'])
    ok, msg = unmod_bot(row)
    with db() as conn:
        conn.execute('UPDATE channels SET removed=1, bot_joined=0 WHERE twitch_id=?',
                    (session['twitch_id'],))
    if ok:
        flash('Bot removed and unmodded from your channel.')
    elif msg == 'reauth':
        flash('Bot removed. To auto-unmod, log out and back in once to grant permission, '
              'then it\'ll unmod itself next time.')
    else:
        flash(f'Bot removed, but auto-unmod failed ({msg}). You can /unmod it manually.')
    return redirect(url_for('dashboard'))


@app.route('/readd-bot', methods=['POST'])
@login_required
def readd_bot():
    with db() as conn:
        conn.execute('UPDATE channels SET removed=0 WHERE twitch_id=?',
                    (session['twitch_id'],))
    flash('Adding the bot back — hang tight, it\'ll rejoin shortly.')
    return redirect(url_for('dashboard'))


@app.route('/regenerate', methods=['POST'])
@login_required
def regenerate():
    with db() as conn:
        conn.execute('UPDATE channels SET pair_token=? WHERE twitch_id=?',
                    (secrets.token_urlsafe(24), session['twitch_id']))
    flash('New token generated — update it in your agent.')
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    init_db()
    app.run(host='127.0.0.1', port=5000, debug=True)