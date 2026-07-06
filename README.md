# ONP — osu! now playing bot for Twitch

ONP is a small Twitch bot that answers the one question every osu! streamer's chat keeps asking: *"what map is this?"*

When a viewer types `!np`, the bot replies with the map you're playing — artist, title, difficulty, star rating, mods, and a link to the beatmap. That's the whole idea. No overlays, no clunky setup, no "download this program and paste that token" ritual. You log in, link your osu! account, mod the bot, and it just works.

There's a hosted version you can use right now at **[onp.artline-studio.de](https://onp.artline-studio.de)** — for most people that's all you need. But the whole thing is open source, so if you'd rather run your own copy, everything you need is below.

---

## What it actually does

The flow is simple:

1. You log in with Twitch and link your osu! account once.
2. You mod the bot in your channel (`/mod <botname>`).
3. From then on, anyone in your chat can type `!np` and the bot posts your current map.

Under the hood there are two ways it figures out what you're playing:

- **Instant mode** (the default) reads your most recent play straight from the osu! API. Nothing to install — as long as your osu! account is linked, it works. This is what almost everyone uses.
- **Live mode** (optional) runs a tiny agent on your PC next to [tosu](https://github.com/tosuapp/tosu), which reads the map you're on in real time and pushes it to the bot. You only need this if you want the exact map *mid-run* instead of your last completed play.

You can also customize exactly what `!np` prints — reorder the fields, add or remove details, change the wording — from your dashboard.

## Commands

| Command | Who can use it | What it does |
| --- | --- | --- |
| `!np` | anyone | Shows the streamer's current osu! map. |
| `!np help` | anyone | Explains what the command is and links to the site. |
| `!np on` / `!np off` | streamer & mods | Turns the command on or off. |

---

## Running your own copy

You don't have to self-host — the hosted version exists for exactly that reason. But if you want to run ONP yourself (to tinker with it, contribute, or just own your setup), here's how.

### What you'll need

- Python 3.10 or newer
- A Twitch account for the bot to run as (separate from your main account is cleanest)
- A [Twitch application](https://dev.twitch.tv/console/apps) (for login + the bot)
- An [osu! OAuth application](https://osu.ppy.sh/home/account/edit) (for reading plays)

### 1. Get the code

```bash
git clone https://github.com/JustKyrix/onp.git
cd onp/server
```

### 2. Set up a virtual environment and install dependencies

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Register your Twitch and osu! apps

**Twitch** ([dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)):
- Create an application.
- Add an OAuth Redirect URL: `http://localhost:5000/callback` (and `http://localhost:3000` for the token step below).
- Note the Client ID and Client Secret.

**osu!** ([osu.ppy.sh/home/account/edit](https://osu.ppy.sh/home/account/edit) → OAuth):
- Create a new OAuth application.
- Set the Application Callback URL to `http://localhost:5000/osu/callback`.
- Note the Client ID and Client Secret.

### 4. Create your `.env`

In the project root (not inside `server/`), create a file called `.env`. This holds your secrets and is ignored by git on purpose — never commit it.

```env
TWITCH_CLIENT_ID=your_twitch_client_id
TWITCH_CLIENT_SECRET=your_twitch_client_secret
BOT_TOKEN=filled_in_by_the_next_step
BOT_REFRESH=filled_in_by_the_next_step
BOT_USERNAME=your_bot_account_name
OSU_CLIENT_ID=your_osu_client_id
OSU_CLIENT_SECRET=your_osu_client_secret
OSU_REDIRECT_URI=http://localhost:5000/osu/callback
WEB_REDIRECT_URI=http://localhost:5000/callback
FLASK_SECRET=any_long_random_string
DB_PATH=onp.db
HELP_URL=http://localhost:5000
```

A couple of notes:
- `FLASK_SECRET` can be anything random. If you want a good one: `python -c "import secrets; print(secrets.token_hex(32))"`.
- `BOT_USERNAME` is the lowercase Twitch username of the account the bot runs as.
- `DB_PATH` can be a relative path like `onp.db` for local use, or an absolute path in production.

### 5. Generate the bot token

The bot needs its own Twitch token to read and send chat. There's a helper script that walks you through it — run it from the project root:

```bash
python get_token.py
```

It opens a browser, asks you to authorize the bot account, and writes the resulting `BOT_TOKEN` and `BOT_REFRESH` values. Make sure you're logged into Twitch as the **bot account** (not your main) when you authorize. The token refreshes itself automatically after that.

### 6. Run it

You need two processes running at the same time — the web app and the bot. Open two terminals (both with the virtual environment active).

Terminal one — the website and dashboard:

```bash
cd server
python web.py
```

Terminal two — the bot:

```bash
cd server
python bot.py
```

Now open `http://localhost:5000`, log in with Twitch, link your osu! account, and mod the bot in your channel. Type `!np` in chat and you should see your map.

---

## Project structure

```
onp/
├── server/
│   ├── web.py            # the website, login, and dashboard (Flask)
│   ├── bot.py            # the Twitch bot (twitchio)
│   ├── requirements.txt
│   ├── templates/        # HTML pages
│   └── static/           # CSS, images
├── agent/                # optional live-mode agent (reads tosu)
├── get_token.py          # one-time helper to generate the bot token
└── .env                  # your secrets (not committed)
```

## How it's built

Nothing exotic — the goal was to keep it approachable:

- **Backend:** Python, [Flask](https://flask.palletsprojects.com/) for the web app, [twitchio](https://twitchio.dev/) for the Twitch chat bot.
- **Data:** SQLite. One file, no database server to manage.
- **Frontend:** plain HTML templates with Bootstrap 5. No build step.
- **osu! data:** the official osu! API v2.

## A note on privacy and hosting

If you host a public instance, keep in mind you become responsible for the data your users trust you with — the app stores each user's Twitch ID and tokens so it can manage the bot on their behalf. Keep your server updated, keep your `.env` and database off the public web, and be upfront with your users about what you store. If you're just running it locally for yourself, none of this really applies.

## Contributing

Issues and pull requests are welcome. If you're planning something bigger, opening an issue first to talk it through is appreciated — saves everyone some wasted effort.

## Contact

Made by kyrix.

- Email — kyrix@artline-studio.de
- osu! — [osu.ppy.sh/users/17022968](https://osu.ppy.sh/users/17022968)
- Twitter/X — [@JKyrix](https://x.com/JKyrix)

## License

Add your license of choice here (MIT is a common, permissive default). If you leave this out, the code is technically "all rights reserved" by default, so pick one if you want others to be able to reuse it.