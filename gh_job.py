#!/usr/bin/env python3
"""gh_job.py: one render job inside GitHub Actions (render.yml). $0 on a public repo.

Input: the repository_dispatch client_payload (from n8n) or workflow_dispatch inputs (manual test):
  {clip_id, source_url, start, end, title, lang, callback_url}
Steps: yt-dlp downloads ONLY the licensed window -> render_edit.py (render + verify_edit.py + auto-revision)
  PASS -> MP4 uploaded to the "renders" release -> POST {"status":"completed","clips":[...]} to callback_url (n8n Callbacks, editor=farm)
  FAIL -> POST {"status":"failed","error":...}  (never reaches Approvals)
Nothing here posts to social media. DRY_RUN lives in n8n and stays true until the user says "POST".
"""
import json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from farm_server import hook_from, post  # same hook rules and callback format as the self-hosted server


def payload():
    ev = json.load(open(os.environ['GITHUB_EVENT_PATH']))
    p = ev.get('client_payload') or ev.get('inputs') or {}
    for k in ('clip_id', 'source_url', 'start', 'end', 'callback_url'):
        if not p.get(k):
            raise SystemExit(f'missing {k} in job payload')
    return p


def main():
    p, t0 = payload(), time.time()
    cid = re.sub(r'[^\w-]', '_', str(p['clip_id']))
    cb = p['callback_url']
    print(f'::add-mask::{cb}')  # the callback URL carries the n8n token: never show it in public logs
    work = os.path.abspath(f'renders/{cid}'); os.makedirs(work, exist_ok=True)
    try:
        s = max(0.0, float(p['start']) - 1.5); e = float(p['end'])
        src = os.path.join(work, 'src.mp4')
        cmd = ['yt-dlp', '-q', '--no-progress', '-f', 'bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b', '--merge-output-format', 'mp4',
               '--download-sections', f'*{s}-{e + 1}', '--force-keyframes-at-cuts', '-o', src]
        if os.path.exists('cookies.txt'):
            cmd += ['--cookies', 'cookies.txt']
        subprocess.run(cmd + [p['source_url']], check=True, timeout=1200)
        hook = hook_from(p.get('title') or '')
        r = subprocess.run([sys.executable, os.path.join(HERE, 'render_edit.py'), '--source', src, '--start', '0', '--end', str(e - s),
                            '--hook', hook, '--title', p.get('title', ''), '--clip-id', cid, '--out', work], text=True)
        rp = os.path.join(work, f'{cid}.report.json')
        rep = json.load(open(rp)) if os.path.exists(rp) else {}
        if r.returncode != 0:
            post(cb, {'status': 'failed', 'error': ('Precision editor rejected: ' + '; '.join(rep.get('issues', []) or ['render error']))[:400]})
            sys.exit(1)
        repo, run = os.environ['GITHUB_REPOSITORY'], os.environ.get('GITHUB_RUN_ID', str(int(t0)))
        name = f'{cid}-{run}.mp4'
        os.rename(os.path.join(work, f'{cid}.mp4'), os.path.join(work, name))
        subprocess.run(['gh', 'release', 'view', 'renders'], capture_output=True).returncode == 0 or \
            subprocess.run(['gh', 'release', 'create', 'renders', '--title', 'Clip Farm renders', '--notes', 'Verified precision edits (dry run: not posted).'], check=True)
        subprocess.run(['gh', 'release', 'upload', 'renders', os.path.join(work, name), '--clobber'], check=True)
        url = f'https://github.com/{repo}/releases/download/renders/{name}'
        post(cb, {'status': 'completed', 'seconds': round(time.time() - t0),
                  'clips': [{'url': url, 'style': 'Precision Safe-Zone', 'title': hook, 'dur': rep.get('checks', {}).get('duration_s')}],
                  'verify': {'pass': True, 'attempt': rep.get('attempt'), 'checks': rep.get('checks')}})
        print('READY FOR APPROVALS:', url)
    except subprocess.CalledProcessError as ex:
        hint = ' (YouTube may be asking GitHub to sign in: add the YT_COOKIES secret)' if 'yt-dlp' in str(ex.cmd) else ''
        post(cb, {'status': 'failed', 'error': f'{os.path.basename(str(ex.cmd[0]))} failed{hint}'[:400]})
        raise


if __name__ == '__main__':
    main()
