"""
ONP Agent — live osu! now-playing bridge for Twitch.

Runs on the streamer's PC next to tosu. Reads the map you're currently on and
pushes it to the ONP bot so !np can show your live map mid-run (instead of your
last completed play).

Usage: paste your pair token (from your ONP dashboard), press Start.
"""
import os
import sys
import json
import time
import threading

import requests

try:
    import webview
except ImportError:
    sys.exit("Missing dependency. Run: pip install pywebview requests pystray pillow")

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
UPDATE_URL = "https://onp.artline-studio.de/update"   # ONP bot's /update endpoint
TOSU_URL   = "http://127.0.0.1:24050/json"            # tosu (gosumemory-compatible)
DASHBOARD  = "https://onp.artline-studio.de/dashboard"
TUTORIAL   = "https://onp.artline-studio.de/#how"
TOSU_SITE  = "https://github.com/tosuapp/tosu"
POLL_SECS  = 2                                         # how often to read + push

CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), ".onp_agent.json")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# the worker: read tosu -> transform -> push to the bot
# ---------------------------------------------------------------------------
class Worker:
    def __init__(self):
        self.thread = None
        self.running = False
        self.token = ""
        self.status = {"state": "idle", "detail": "Not running.", "map": ""}

    def start(self, token):
        token = (token or "").strip()
        if not token:
            self.status = {"state": "error", "detail": "Paste your pair token first.", "map": ""}
            return self.status
        self.token = token
        save_config({"token": token})
        if self.running:
            return self.status
        self.running = True
        self.status = {"state": "connecting", "detail": "Looking for tosu...", "map": ""}
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self.status

    def stop(self):
        self.running = False
        self.status = {"state": "idle", "detail": "Stopped.", "map": ""}
        return self.status

    def get_status(self):
        return self.status

    def _loop(self):
        while self.running:
            try:
                play = self._read_tosu()
                if play is None:
                    self.status = {"state": "no_tosu",
                                   "detail": "tosu not found. Is it running?", "map": ""}
                else:
                    ok = self._push(play)
                    if ok:
                        self.status = {
                            "state": "live",
                            "detail": "Connected - sending your map to chat.",
                            "map": f"{play['artist']} - {play['title']} [{play['diff']}]"}
                    else:
                        self.status = {"state": "error",
                                       "detail": "Bad token, or the bot rejected the update.",
                                       "map": ""}
            except requests.exceptions.ConnectionError:
                self.status = {"state": "no_tosu",
                               "detail": "tosu not found. Is it running?", "map": ""}
            except Exception as e:
                self.status = {"state": "error", "detail": f"Error: {e}", "map": ""}
            for _ in range(POLL_SECS * 2):
                if not self.running:
                    return
                time.sleep(0.5)

    def _read_tosu(self):
        """Poll tosu's /json and map it to the fields the bot expects."""
        d = requests.get(TOSU_URL, timeout=3).json()
        bm = d.get("menu", {}).get("bm", {})
        meta = bm.get("metadata", {})
        stats = bm.get("stats", {})
        mods = d.get("menu", {}).get("mods", {}).get("str", "")
        title = meta.get("title") or meta.get("titleOriginal")
        if not title:
            return None
        bmid = bm.get("id", "")
        sr = stats.get("SR") or stats.get("fullSR") or ""
        return {
            "artist":  meta.get("artist") or meta.get("artistOriginal") or "?",
            "title":   title,
            "diff":    meta.get("difficulty") or "?",
            "creator": meta.get("mapper") or "?",
            "sr":  sr,
            "ar":  stats.get("AR", ""),
            "cs":  stats.get("CS", ""),
            "od":  stats.get("OD", ""),
            "hp":  stats.get("HP", ""),
            "bpm": stats.get("BPM", ""),
            "mods": mods or "None",
            "id":  bmid,
            "url": f"https://osu.ppy.sh/beatmaps/{bmid}" if bmid else "",
        }

    def _push(self, play):
        r = requests.post(UPDATE_URL, json=play,
                          headers={"X-Pair-Token": self.token}, timeout=5)
        return r.status_code == 200


# ---------------------------------------------------------------------------
# bridge exposed to the window's JavaScript
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, worker):
        self.worker = worker

    def load_token(self):
        return load_config().get("token", "")

    def start(self, token):
        return self.worker.start(token)

    def stop(self):
        return self.worker.stop()

    def status(self):
        return self.worker.get_status()

    def open(self, which):
        url = {"dashboard": DASHBOARD, "tutorial": TUTORIAL, "tosu": TOSU_SITE}.get(which)
        if url:
            import webbrowser
            webbrowser.open(url)

    def minimize(self):
        webview.windows[0].minimize()

    def hide(self):
        # hide to tray; the tray icon's Show brings it back, Quit exits
        webview.windows[0].hide()


# ---------------------------------------------------------------------------
# UI  (kawaii palette to match onp.artline-studio.de)
# ---------------------------------------------------------------------------
HTML = r"""
<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600&family=Nunito:wght@600;700;800&display=swap" rel="stylesheet">
<style>
  :root{
    --wine:#67003F; --rasp:#D52C5E; --pink:#ff9ec4; --pink-lite:#ff5c8f;
    --ink:#150410; --bg:#1e0a18; --panel:#2a1220; --panel-2:#341829;
    --line:rgba(255,170,205,.14); --line-2:rgba(255,170,205,.26);
    --soft:#fbeef4; --muted:#cba6bc; --ok:#6fe0a8; --warn:#ffcf8a;
    --grad:linear-gradient(120deg,#67003F,#D52C5E 62%,#ff6fa5);
  }
  *{box-sizing:border-box;}
  html,body{margin:0;height:100%;}
  body{
    font-family:'Nunito',system-ui,sans-serif; color:var(--soft);
    background:
      radial-gradient(600px 380px at 88% -10%,rgba(213,44,94,.28),transparent 60%),
      radial-gradient(520px 400px at -8% 6%,rgba(103,0,63,.5),transparent 56%),
      var(--bg);
    user-select:none; -webkit-user-select:none;
    border:1px solid var(--line-2); border-radius:14px; overflow:hidden;
    height:100vh; display:flex; flex-direction:column;
  }
  /* custom frameless title bar */
  .titlebar{display:flex;align-items:center;height:42px;padding-left:14px;
    border-bottom:1px solid var(--line);background:rgba(0,0,0,.15);flex:0 0 auto;}
  .tb-drag{flex:1;display:flex;align-items:center;gap:8px;height:100%;
    font-family:'Fredoka',sans-serif;font-weight:600;font-size:.9rem;color:var(--soft);}
  .ring-sm{width:15px;height:15px;border-radius:50%;background:var(--grad);flex:0 0 auto;
    box-shadow:0 0 0 3px rgba(255,110,165,.16);}
  .tb-btns{display:flex;height:100%;}
  .tb-btn{width:44px;height:100%;border:0;background:transparent;color:var(--muted);
    font-size:.95rem;cursor:pointer;transition:background .15s,color .15s;}
  .tb-btn:hover{background:rgba(255,255,255,.06);color:var(--soft);}
  .tb-btn.close:hover{background:var(--rasp);color:#fff;}
  .content{flex:1;padding:20px;overflow:auto;}
  h1,.btn{font-family:'Fredoka',sans-serif;}
  .top{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
  .ring{width:22px;height:22px;border-radius:50%;background:var(--grad);
    box-shadow:0 0 0 4px rgba(255,110,165,.16),0 8px 22px -8px rgba(213,44,94,.6);}
  h1{font-size:1.15rem;font-weight:600;margin:0;letter-spacing:-.01em;}
  h1 .sub{color:var(--muted);font-family:'Nunito';font-weight:700;font-size:.72rem;
    text-transform:uppercase;letter-spacing:.14em;display:block;margin-top:1px;}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel-2));
    border:1px solid var(--line);border-radius:16px;padding:16px;}
  .status{display:flex;align-items:center;gap:10px;margin-bottom:14px;
    background:var(--ink);border:1px solid var(--line);border-radius:12px;padding:11px 13px;}
  .dot{width:11px;height:11px;border-radius:50%;background:var(--muted);flex:0 0 auto;}
  .dot.live{background:var(--ok);box-shadow:0 0 10px var(--ok);animation:pulse 1.4s infinite;}
  .dot.connecting{background:var(--warn);animation:pulse 1.4s infinite;}
  .dot.no_tosu,.dot.error{background:var(--pink-lite);}
  @keyframes pulse{0%,100%{opacity:1;}50%{opacity:.4;}}
  .status .txt{font-size:.9rem;font-weight:700;}
  .status .map{font-size:.76rem;color:var(--muted);margin-top:2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  label{display:block;font-weight:800;font-size:.8rem;margin:2px 0 6px;}
  .tokrow{display:flex;gap:8px;}
  input{flex:1;min-width:0;background:var(--ink);border:1px solid var(--line-2);color:var(--soft);
    border-radius:10px;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:.82rem;}
  input:focus{outline:none;border-color:var(--pink);box-shadow:0 0 0 3px rgba(255,158,196,.18);}
  .eye{background:var(--panel);border:1px solid var(--line-2);color:var(--soft);border-radius:10px;
    padding:0 12px;cursor:pointer;font-size:.8rem;font-weight:700;}
  .eye:hover{border-color:var(--pink);}
  .btn{width:100%;margin-top:14px;border:0;border-radius:999px;padding:12px;font-size:1rem;
    font-weight:600;cursor:pointer;color:#fff;background:var(--grad);
    box-shadow:0 10px 26px -10px rgba(213,44,94,.6);transition:transform .1s,filter .2s;}
  .btn:hover{transform:translateY(-1px);filter:brightness(1.07);}
  .btn.stop{background:var(--panel);border:1px solid var(--line-2);box-shadow:none;color:var(--soft);}
  .btn.stop:hover{border-color:var(--pink);filter:none;}
  .hint{font-size:.74rem;color:var(--muted);margin:10px 2px 0;line-height:1.5;}
  .links{display:flex;gap:14px;justify-content:center;margin-top:16px;}
  .links a{color:var(--muted);font-size:.78rem;font-weight:700;text-decoration:none;cursor:pointer;}
  .links a:hover{color:var(--pink);}
</style></head>
<body>
  <div class="titlebar">
    <div class="tb-drag pywebview-drag-region"><span class="ring-sm"></span> ONP Agent</div>
    <div class="tb-btns">
      <button class="tb-btn" onclick="pywebview.api.minimize()" title="Minimize">&#8211;</button>
      <button class="tb-btn close" onclick="pywebview.api.hide()" title="Hide to tray">&#10005;</button>
    </div>
  </div>
  <div class="content">
  <div class="top">
    <span class="ring"></span>
    <h1>ONP Agent<span class="sub">live osu! &#8594; twitch</span></h1>
  </div>

  <div class="card">
    <div class="status">
      <span class="dot" id="dot"></span>
      <div style="min-width:0;">
        <div class="txt" id="statusText">Not running.</div>
        <div class="map" id="mapText"></div>
      </div>
    </div>

    <label for="tok">Pair token</label>
    <div class="tokrow">
      <input id="tok" type="password" placeholder="paste from your ONP dashboard" spellcheck="false">
      <button class="eye" id="eye" onclick="toggleEye()">Show</button>
    </div>

    <button class="btn" id="go" onclick="go()">Start</button>
    <p class="hint">Make sure <b>tosu</b> is running first. Your token lives only on
      this PC &mdash; find it in the dashboard under &ldquo;Live mode &amp; agent token.&rdquo;</p>
  </div>

  <div class="links">
    <a onclick="pywebview.api.open('dashboard')">Dashboard</a>
    <a onclick="pywebview.api.open('tutorial')">How it works</a>
    <a onclick="pywebview.api.open('tosu')">Get tosu</a>
  </div>
  </div><!-- /.content -->

<script>
  let running = false;

  function toggleEye(){
    const i=document.getElementById('tok'), b=document.getElementById('eye');
    if(i.type==='password'){i.type='text';b.textContent='Hide';}
    else{i.type='password';b.textContent='Show';}
  }

  function paint(s){
    const dot=document.getElementById('dot');
    dot.className='dot '+s.state;
    document.getElementById('statusText').textContent=s.detail||'';
    document.getElementById('mapText').textContent=s.map||'';
    const active = (s.state==='live'||s.state==='connecting'||s.state==='no_tosu');
    const btn=document.getElementById('go');
    btn.textContent = active ? 'Stop' : 'Start';
    btn.className = 'btn' + (active ? ' stop' : '');
    running = active;
  }

  async function go(){
    if(running){ paint(await pywebview.api.stop()); return; }
    const t=document.getElementById('tok').value;
    paint(await pywebview.api.start(t));
  }

  async function poll(){
    try{ paint(await pywebview.api.status()); }catch(e){}
  }

  window.addEventListener('pywebviewready', async ()=>{
    const saved = await pywebview.api.load_token();
    if(saved) document.getElementById('tok').value = saved;
    setInterval(poll, 1000);
    poll();
  });
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# tray icon (optional - degrades gracefully if pystray/Pillow are missing)
# ---------------------------------------------------------------------------
def start_tray(window):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=(213, 44, 94, 255))
    d.ellipse((24, 24, 40, 40), fill=(255, 255, 255, 255))

    def show(icon, item):
        window.show()

    def hide(icon, item):
        window.hide()

    def quit_(icon, item):
        icon.stop()
        window.destroy()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Show", show, default=True),
        pystray.MenuItem("Hide", hide),
        pystray.MenuItem("Quit", quit_),
    )
    icon = pystray.Icon("ONP Agent", img, "ONP Agent", menu)
    threading.Thread(target=icon.run, daemon=True).start()


def main():
    worker = Worker()
    api = Api(worker)
    window = webview.create_window(
        "ONP Agent", html=HTML, js_api=api,
        width=420, height=580, resizable=False,
        frameless=True, easy_drag=False,
        background_color="#1e0a18",
    )
    start_tray(window)
    webview.start()


if __name__ == "__main__":
    main()