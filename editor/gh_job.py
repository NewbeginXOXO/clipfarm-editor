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
    if p.get('mode') == 'moments':
        return p
    for k in ('clip_id', 'source_url', 'start', 'end', 'callback_url'):
        if not p.get(k):
            raise SystemExit(f'missing {k} in job payload')
    return p


def moments(p):
    """Moment scan mode: find viral spikes (chat replay + Most replayed) in finished GTA 6 streams and send them to n8n. No rendering, no AI."""
    from find_moments import scan
    cb = p['callback_url']
    print(f'::add-mask::{cb}')
    os.makedirs('moments', exist_ok=True)
    vids = [v for v in (p.get('videos') or []) if re.fullmatch(r'[\w-]{11}', str(v.get('video_id', '')))][:6]
    res = []
    for v in vids:
        try:
            r = scan(v['video_id'], 'moments')
        except Exception as ex:
            r = {'video_id': v['video_id'], 'ok': False, 'error': f'{ex.__class__.__name__}: {ex}'[:300]}
        res.append({**v, **r})
    post(cb, {'status': 'moments', 'results': res})
    print(json.dumps([{k: r.get(k) for k in ('video_id', 'ok', 'had_heatmap', 'chat_peaks', 'error')} for r in res]))


def main():
    p, t0 = payload(), time.time()
    if p.get('mode') == 'moments':
        return moments(p)
    cid = re.sub(r'[^\w-]', '_', str(p['clip_id']))
    cb = p['callback_url']
    print(f'::add-mask::{cb}')  # the callback URL carries the n8n token: never show it in public logs
    work = os.path.abspath(f'renders/{cid}'); os.makedirs(work, exist_ok=True)
    try:
        s = max(0.0, float(p['start']) - 1.5); e = float(p['end'])
        src = os.path.join(work, 'src.mp4')
        cmd = ['yt-dlp', '-q', '--no-progress', '-f', 'bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b', '--merge-output-format', 'mp4', '--remote-components', 'ejs:github',
               '--download-sections', f'*{s}-{e + 1}', '--force-keyframes-at-cuts', '-o', src]
        if os.path.exists('cookies.txt'):
            cmd += ['--cookies', 'cookies.txt']
        dl = subprocess.run(cmd + [p['source_url']], capture_output=True, text=True, timeout=1200)
        if dl.returncode != 0 or not os.path.exists(src):
            errs = [l for l in (dl.stderr or '').splitlines() if 'ERROR' in l] or (dl.stderr or 'no output').splitlines()[-1:]
            why = errs[-1] if errs else 'unknown'
            hint = ' | fix: refresh the YT_COOKIES secret (cookies expired or signed out)' if re.search(r'sign in|cookies|bot', why, re.I) else ''
            print(dl.stderr[-3000:])
            post(cb, {'status': 'failed', 'error': ('Download failed: ' + why.replace('ERROR: ', ''))[:330] + hint})
            sys.exit(1)
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
    except SystemExit:
        raise
    except Exception as ex:  # any crash still reports back, so a clip never sits in Editing forever
        post(cb, {'status': 'failed', 'error': f'Render job crashed: {ex.__class__.__name__}: {ex}'[:400]})
        raise


if __name__ == '__main__':
    main()
