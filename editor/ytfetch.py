"""ytfetch.py: one place for every YouTube call (renders, thumbnails, moment scans, diag). $0, no AI.

YouTube's "Sign in to confirm you're not a bot" wall hits datacenter IPs such as GitHub Actions runners. Each call walks a ladder:
  1. default clients + cookies
  2. tv / web_safari clients + cookies
  3. mweb client + cookies (PO token from the bgutil provider when it is running)
  4. tv client WITHOUT cookies (a flagged cookie session can make things worse)
Every attempt uses IPv4, a short pause between requests and the JS challenge solver. It only moves to the next rung on a
bot / sign-in / 403 / unavailable style error; any other error returns at once. The last attempt's result is returned.
"""
import os, re, subprocess, time

BLOCK = re.compile(r'sign in to confirm|not a bot|login_required|http error 403|forbidden|this content isn.t available|video unavailable|po token|precondition', re.I)
COMMON = ['--force-ipv4', '--sleep-requests', '1', '--extractor-retries', '3', '--remote-components', 'ejs:github']
LADDER = [('default', [], True), ('tv+web_safari', ['--extractor-args', 'youtube:player_client=tv,web_safari'], True),
          ('mweb', ['--extractor-args', 'youtube:player_client=mweb'], True), ('tv, no cookies', ['--extractor-args', 'youtube:player_client=tv'], False)]
LAST = {'rung': '', 'tried': []}


def run(args, timeout, url=None):
    """args: yt-dlp options (without the URL); url appended last. Returns the CompletedProcess of the first success or of the last attempt."""
    cookies = os.path.exists('cookies.txt')
    LAST['tried'] = []
    r = None
    for i, (name, extra, use_cookies) in enumerate(LADDER):
        cmd = ['yt-dlp'] + COMMON + extra + (['--cookies', 'cookies.txt'] if (use_cookies and cookies) else []) + list(args) + ([url] if url else [])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as ex:
            r = subprocess.CompletedProcess(cmd, 124, '', f'ERROR: timeout after {timeout}s')
        err = ([l for l in (r.stderr or '').splitlines() if 'ERROR' in l] or [''])[-1][:200]
        LAST['tried'].append({'rung': name, 'ok': r.returncode == 0, 'error': err})
        if r.returncode == 0:
            LAST['rung'] = name
            print(f'ytfetch: ok via {name}')
            return r
        print(f'ytfetch: {name} failed: {err}')
        if not BLOCK.search(r.stderr or ''):
            break
        time.sleep(6 + 4 * i)
    LAST['rung'] = ''
    return r
