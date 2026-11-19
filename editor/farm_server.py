#!/usr/bin/env python3
"""farm_server.py: the "Clip Farm Render" endpoint n8n already calls (Clip Farm Query > Engine Fanout, editor='farm').

  POST /jobs   Authorization: Bearer <FARM_TOKEN>
       {clip_id, source_url, start, end, title, lang, styles, callback_url}
  -> downloads the licensed source window (yt-dlp), runs render_edit.py (render + verify_edit.py + auto-revision),
     serves the MP4 at /files/<clip_id>.mp4 and POSTs the result to callback_url (Clip Farm Callbacks, editor=farm):
       PASS -> {"status":"completed","clips":[{"url","style","title","dur"}],"seconds",...}  -> Approvals + email
       FAIL -> {"status":"failed","error":"<verifier issues>"}                                -> never reaches Approvals

Run:  FARM_TOKEN=... PUBLIC_URL=https://<your-host> python farm_server.py   (port 8787)
Then paste PUBLIC_URL and FARM_TOKEN into Engine Fanout (FARM_URL / FARM_TOKEN). $0: stdlib + local tools.
"""
import json, os, re, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '..', 'renders'); os.makedirs(OUT, exist_ok=True)
TOKEN = os.environ.get('FARM_TOKEN', ''); PUBLIC = os.environ.get('PUBLIC_URL', '').rstrip('/')
LOCK = threading.Semaphore(1)  # one render at a time keeps a laptop/VM responsive


FILLER = {'do', 'the', 'a', 'an', 'really', 'just', 'so', 'actually', 'this', 'that', 'very'}


def hook_from(title):
    """<= 7 words. Drops emoji/hashtags, then filler words, and never drops 'GTA 6' (the renderer rejects non-GTA hooks)."""
    words = [w for w in re.sub(r'#\S+|[^\w\s?!\'.,-]', '', title or '').split() if w]
    while len(words) > 7:
        f = next((i for i, w in enumerate(words) if w.lower() in FILLER), None)
        if f is None:
            break
        del words[f]
    if len(words) > 7:
        keep = [i for i, w in enumerate(words) if re.match(r'(?i)gta|6\??!?$|vi\??$', w)]
        drop = [i for i in range(len(words)) if i not in keep][len(words) - 7:] if False else [i for i in reversed(range(len(words))) if i not in keep][:len(words) - 7]
        words = [w for i, w in enumerate(words) if i not in set(drop)]
    while words and words[-1].lower() in {'and', 'or', 'but', 'so', 'with', 'to', 'of'}:
        words.pop()
    return ' '.join(words) or 'GTA 6 moment'


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    urllib.request.urlopen(req, timeout=60).read()


def run_job(j):
    with LOCK:
        t0 = time.time(); cid = re.sub(r'[^\w-]', '_', str(j['clip_id'])); work = os.path.join(OUT, cid); os.makedirs(work, exist_ok=True)
        try:
            pre = 1.5; s = max(0, float(j['start']) - pre); e = float(j['end'])
            src = os.path.join(work, 'src.mp4')
            subprocess.run(['yt-dlp', '-q', '-f', 'bv*[height<=1080]+ba/b', '--merge-output-format', 'mp4', '--download-sections', f'*{s}-{e + 1}',
                            '--force-keyframes-at-cuts', '-o', src, j['source_url']], check=True, timeout=900)
            hook = hook_from(j.get('title'))
            r = subprocess.run([sys.executable, os.path.join(HERE, 'render_edit.py'), '--source', src, '--start', '0', '--end', str(e - s),
                                '--hook', hook, '--title', j.get('title', ''), '--clip-id', cid, '--out', work], capture_output=True, text=True, timeout=1800)
            rep = json.load(open(os.path.join(work, f'{cid}.report.json'))) if os.path.exists(os.path.join(work, f'{cid}.report.json')) else {}
            if r.returncode != 0:
                post(j['callback_url'], {'status': 'failed', 'error': ('; '.join(rep.get('issues', [])) or r.stdout[-300:]).strip()[:400]})
                return
            post(j['callback_url'], {'status': 'completed', 'seconds': round(time.time() - t0),
                                     'clips': [{'url': f'{PUBLIC}/files/{cid}/{cid}.mp4', 'style': 'Precision Safe-Zone', 'title': hook, 'dur': rep.get('checks', {}).get('duration_s')}],
                                     'verify': {'pass': True, 'attempt': rep.get('attempt'), 'checks': rep.get('checks')}})
        except Exception as ex:
            try:
                post(j['callback_url'], {'status': 'failed', 'error': f'{ex.__class__.__name__}: {ex}'[:400]})
            except Exception:
                pass


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/jobs' or self.headers.get('Authorization') != f'Bearer {TOKEN}' or not TOKEN:
            self.send_response(401); self.end_headers(); return
        j = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        if not all(k in j for k in ('clip_id', 'source_url', 'start', 'end', 'callback_url')):
            self.send_response(400); self.end_headers(); return
        threading.Thread(target=run_job, args=(j,), daemon=True).start()
        self.send_response(202); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps({'ok': True, 'queued': j['clip_id']}).encode())

    def do_GET(self):
        m = re.match(r'^/files/([\w-]+)/([\w-]+\.mp4)$', self.path)
        p = m and os.path.join(OUT, m.group(1), m.group(2))
        if not p or not os.path.exists(p):
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header('Content-Type', 'video/mp4'); self.send_header('Content-Length', str(os.path.getsize(p))); self.end_headers()
        with open(p, 'rb') as f:
            self.wfile.write(f.read())


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8787))), H).serve_forever()
