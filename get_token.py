import os
import sys
import http.server
import urllib.parse
import webbrowser
import requests

# --- tiny .env loader (no extra deps) ---
def load_env(path='.env'):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()
CLIENT_ID     = os.environ['TWITCH_CLIENT_ID']
CLIENT_SECRET = os.environ['TWITCH_CLIENT_SECRET']
REDIRECT      = 'http://localhost:3000'
SCOPES        = 'chat:read chat:edit user:read:moderated_channels'

auth_url = 'https://id.twitch.tv/oauth2/authorize?' + urllib.parse.urlencode({
    'client_id': CLIENT_ID,
    'redirect_uri': REDIRECT,
    'response_type': 'code',
    'scope': SCOPES,
    'force_verify': 'true',      # always show the login/authorize screen
})

result = {}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get('code', [None])[0]
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        if code:
            result['code'] = code
            self.wfile.write(b'<h2>Got it. Close this tab and go back to the terminal.</h2>')
        else:
            self.wfile.write(b'<h2>No code in the URL. Check the terminal.</h2>')

    def log_message(self, *a):
        pass

def main():
    print('\n1) A browser tab is opening.')
    print('2) MAKE SURE you are logged in as the BOT (alt) account, not your main!')
    print('   If your main is logged in, open the URL below in an incognito window')
    print('   and log in as the bot there:\n')
    print(auth_url, '\n')

    server = http.server.HTTPServer(('localhost', 3000), Handler)
    webbrowser.open(auth_url)
    while 'code' not in result:
        server.handle_request()

    tok = requests.post('https://id.twitch.tv/oauth2/token', data={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': result['code'],
        'grant_type': 'authorization_code',
        'redirect_uri': REDIRECT,
    }, timeout=10).json()

    access, refresh = tok.get('access_token'), tok.get('refresh_token')
    if not access:
        print('❌ Token exchange failed:', tok)
        sys.exit(1)

    # confirm which account this token actually belongs to
    who = requests.get('https://api.twitch.tv/helix/users', headers={
        'Client-Id': CLIENT_ID,
        'Authorization': f'Bearer {access}',
    }, timeout=10).json()
    login = who['data'][0]['login']

    print('\n✅ Success! This token belongs to Twitch account:', login)
    print('   -> If that is NOT your bot account, delete these lines from .env and run again.\n')
    print('BOT_TOKEN=' + access)
    print('BOT_REFRESH=' + refresh)

    with open('.env', 'a') as f:
        f.write(f'\nBOT_TOKEN={access}\nBOT_REFRESH={refresh}\n')
    print('\nAppended BOT_TOKEN and BOT_REFRESH to .env ✅')

if __name__ == '__main__':
    main()