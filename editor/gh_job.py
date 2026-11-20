#!/usr/bin/env python3
"""gh_job.py: one render job inside GitHub Actions (render.yml). $0 on a public repo.

Input: the repository_dispatch client_payload (from n8n) or workflow_dispatch inputs (manual test):
  {clip_id, source_url, start, end, title, lang, callback_url}
Steps: yt-dlp downloads ONLY the licensed window -> render_edit.py (render + verify_edit.py + auto-revision)
  PASS -> MP4 uploaded to the "renders" release -> POST {"status":"completed","clips":[...]} to callback_url (n8n Callbacks, editor=farm)
  FAIL -> POST {"status":"failed","error":...}  (never reaches Approvals)
Every verified render also gets TWO thumbnail options (thumbs.py): Option A and Option B, cut from the clean, text-free 9:16
composition (never from the captioned MP4: thumbnails carry NO spoken captions) with one hook at the top in the safe band.
Thumbs mode backfills or REVISES those options for edits already in the "renders" release without re-rendering the video:
it re-downloads only the licensed window, rebuilds the same clean layout and cuts the frames (no AI).
Nothing here posts to social media. DRY_RUN lives in n8n and stays true until the user says "POST".
"""
import json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from farm_server import hook_from, post  # same hook rules and callback format as the self-hosted server


def payload():
    ev = json.load(open(os.environ['GITHUB_EVENT_PATH']))
    p = ev.get('client_payload') or ev.get('inputs') or {}
    if p.get('mode') in ('moments', 'thumbs', 'diag'):
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


def download_window(source_url, start, end, work):
    """yt-dlp downloads ONLY the licensed window [start-1.5, end+1]. Returns (path, s, e) or raises RuntimeError(reason)."""
    s = max(0.0, float(start) - 1.5); e = float(end)
    src = os.path.join(work, 'src.mp4')
    cmd = ['yt-dlp', '-q', '--no-progress', '-f', 'bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b', '--merge-output-format', 'mp4', '--remote-components', 'ejs:github',
           '--download-sections', f'*{s}-{e + 1}', '--force-keyframes-at-cuts', '-o', src]
    if os.path.exists('cookies.txt'):
        cmd += ['--cookies', 'cookies.txt']
    dl = subprocess.run(cmd + [source_url], capture_output=True, text=True, timeout=1200)
    if dl.returncode != 0 or not os.path.exists(src):
        errs = [l for l in (dl.stderr or '').splitlines() if 'ERROR' in l] or (dl.stderr or 'no output').splitlines()[-1:]
        why = errs[-1] if errs else 'unknown'
        hint = ' | fix: refresh the YT_COOKIES secret (cookies expired or signed out)' if re.search(r'sign in|cookies|bot', why, re.I) else ''
        print((dl.stderr or '')[-3000:])
        raise RuntimeError(('Download failed: ' + why.replace('ERROR: ', ''))[:330] + hint)
    return src, s, e


def clean_source(item, work):
    """Text-free 9:16 composition for an edit already in the release: the same window, facecam box and base layout as render_edit.py."""
    from render_edit import base_filter, detect_facecam, probe_size
    src, s, e = download_window(item['source_url'], item['start'], item['end'], work)
    seg = os.path.join(work, 'seg.mp4')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', '0', '-to', str(e - s), '-i', src, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '14', '-an', seg], check=True, timeout=600)
    sw, sh_ = probe_size(seg)
    return seg, base_filter(detect_facecam(seg, sw, sh_), sw, sh_)


def upload(paths):
    for p in paths:
        subprocess.run(['gh', 'release', 'upload', 'renders', p, '--clobber'], check=True, timeout=300)


def ensure_release():
    subprocess.run(['gh', 'release', 'view', 'renders'], capture_output=True).returncode == 0 or \
        subprocess.run(['gh', 'release', 'create', 'renders', '--title', 'Clip Farm renders', '--notes', 'Verified precision edits (dry run: not posted).'], check=True)


def thumbs(p):
    """Backfill or revise Thumbnail Option A / B for finished edits already in the "renders" release. Never posts.
    Item: {clip_id, url (the edit's MP4), source_url, start, end, hook, thumb_b, option ('A'|'B'|'' = both), t, text, rev}."""
    from thumbs import make_options
    cb = p['callback_url']
    print(f'::add-mask::{cb}')
    repo = os.environ['GITHUB_REPOSITORY']
    pre = f'https://github.com/{repo}/releases/download/renders/'
    os.makedirs('thumbs', exist_ok=True); ensure_release()
    out = []
    for it in (p.get('items') or [])[:10]:
        url, cid = str(it.get('url', '')), re.sub(r'[^\w-]', '_', str(it.get('clip_id', '')))
        name = url[len(pre):] if url.startswith(pre) else ''
        if not cid or not re.fullmatch(r'[\w.-]+\.mp4', name):
            out.append({'clip_id': it.get('clip_id'), 'ok': False, 'error': 'not a renders-release MP4'}); continue
        if not re.match(r'^https://(www\.)?(youtube\.com|youtu\.be)/', str(it.get('source_url', ''))) or it.get('start') in (None, '') or it.get('end') in (None, ''):
            out.append({'clip_id': it.get('clip_id'), 'ok': False, 'error': 'needs the licensed source window (thumbnails are never cut from the captioned MP4)'}); continue
        try:
            rev = int(it.get('rev') or 0); rev = rev if 0 < rev < 1000 else 0
            opt = str(it.get('option') or '').upper(); opt = opt if opt in ('A', 'B') else None
            t = it.get('t'); t = min(max(float(t), 0.0), 180.0) if t not in (None, '') else None
            text = re.sub(r"[^\w\s?!'.,&#-]", '', str(it.get('text') or ''))[:60].strip()
            work = os.path.abspath(os.path.join('thumbs', cid)); os.makedirs(work, exist_ok=True)
            seg, fc = clean_source(it, work)
            res = make_options(seg, os.path.join('thumbs', name[:-4]), str(it.get('hook') or ''), str(it.get('thumb_b') or ''), work, fc=fc, only=opt, t=t, text=text, suffix=f'-r{rev}' if rev else '')
            if not res: raise RuntimeError('no thumbnail made')
            upload([v[0] for v in res.values()])
            o = {'clip_id': it.get('clip_id'), 'ok': True, 'rev': rev}
            for k, (jpg, tt, tx) in res.items():
                o['thumb_' + k.lower()] = pre + os.path.basename(jpg); o['t_' + k.lower()] = tt; o['text_' + k.lower()] = tx
            out.append(o)
        except Exception as ex:
            out.append({'clip_id': it.get('clip_id'), 'ok': False, 'error': f'{ex.__class__.__name__}: {ex}'[:200]})
    post(cb, {'status': 'thumbs', 'items': out})
    print(json.dumps(out))


def diag(p):
    """Cookie health check (no download, no posting). Prints cookie NAMES, domains and expiry dates only, never values."""
    info = {'cookies_file': os.path.exists('cookies.txt')}
    if info['cookies_file']:
        raw = open('cookies.txt', encoding='utf-8', errors='replace').read()
        rows = [l.split('\t') for l in raw.splitlines() if l.strip() and not l.startswith('#') or l.startswith('#HttpOnly_')]
        rows = [r for r in rows if len(r) >= 7]
        names = {r[5]: r for r in rows}
        key = ['LOGIN_INFO', 'SID', 'HSID', 'SSID', 'SAPISID', '__Secure-1PSID', '__Secure-3PSID', '__Secure-3PAPISID', '__Secure-1PSIDTS', '__Secure-3PSIDTS']
        info.update({'header_ok': raw.lstrip().startswith('# Netscape HTTP Cookie File') or raw.lstrip().startswith('# HTTP Cookie File'),
                     'lines': len(rows), 'tabs_ok': bool(rows), 'domains': sorted({r[0].replace('#HttpOnly_', '') for r in rows})[:12],
                     'auth_cookies': {k: time.strftime('%Y-%m-%d', time.gmtime(int(names[k][4]))) if names[k][4].isdigit() and int(names[k][4]) > 0 else 'session' for k in key if k in names},
                     'missing': [k for k in ('LOGIN_INFO', 'SAPISID', '__Secure-3PSID') if k not in names]})
    vid = re.sub(r'[^\w-]', '', str(p.get('video_id') or 'NxgYtzq_n70'))[:11]
    cmd = ['yt-dlp', '--simulate', '--no-warnings', '--remote-components', 'ejs:github', '-f', 'bv*[height<=1080]+ba/b'] + (['--cookies', 'cookies.txt'] if info['cookies_file'] else []) + ['https://www.youtube.com/watch?v=' + vid]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    info['yt_dlp_ok'] = r.returncode == 0
    info['yt_dlp_error'] = ([l for l in (r.stderr or '').splitlines() if 'ERROR' in l] or [''])[-1][:300]
    print('DIAG ' + json.dumps(info))


def main():
    p, t0 = payload(), time.time()
    if p.get('mode') == 'moments':
        return moments(p)
    if p.get('mode') == 'thumbs':
        return thumbs(p)
    if p.get('mode') == 'diag':
        return diag(p)
    cid = re.sub(r'[^\w-]', '_', str(p['clip_id']))
    cb = p['callback_url']
    print(f'::add-mask::{cb}')  # the callback URL carries the n8n token: never show it in public logs
    work = os.path.abspath(f'renders/{cid}'); os.makedirs(work, exist_ok=True)
    try:
        try:
            src, s, e = download_window(p['source_url'], p['start'], p['end'], work)
        except RuntimeError as ex:
            post(cb, {'status': 'failed', 'error': str(ex)})
            sys.exit(1)
        hook = hook_from(p.get('title') or '')
        r = subprocess.run([sys.executable, os.path.join(HERE, 'render_edit.py'), '--source', src, '--start', '0', '--end', str(e - s),
                            '--hook', hook, '--title', ' '.join(x for x in (p.get('title', ''), str(p.get('context', ''))[:200]) if x), '--clip-id', cid, '--out', work], text=True)  # context = campaign + source title, so the GTA 6 check sees the footage source
        rp = os.path.join(work, f'{cid}.report.json')
        rep = json.load(open(rp)) if os.path.exists(rp) else {}
        if r.returncode != 0:
            post(cb, {'status': 'failed', 'error': ('Precision editor rejected: ' + '; '.join(rep.get('issues', []) or ['render error']))[:400]})
            sys.exit(1)
        repo, run = os.environ['GITHUB_REPOSITORY'], os.environ.get('GITHUB_RUN_ID', str(int(t0)))
        name = f'{cid}-{run}.mp4'
        os.rename(os.path.join(work, f'{cid}.mp4'), os.path.join(work, name))
        ensure_release()
        subprocess.run(['gh', 'release', 'upload', 'renders', os.path.join(work, name), '--clobber'], check=True)
        url = f'https://github.com/{repo}/releases/download/renders/{name}'
        thumb = thumb_b = ''
        try:  # a thumbnail problem never blocks a verified edit. Options come from clean.mp4 (same edit, no text): no talking captions.
            from thumbs import make_options
            res = make_options(os.path.join(work, 'work', 'clean.mp4'), os.path.join(work, name[:-4]), hook, str(p.get('thumb_b') or ''), os.path.join(work, 'thumbs'))
            upload([v[0] for v in res.values()])
            pre = f'https://github.com/{repo}/releases/download/renders/'
            thumb = pre + os.path.basename(res['A'][0]) if 'A' in res else ''
            thumb_b = pre + os.path.basename(res['B'][0]) if 'B' in res else ''
        except Exception as ex:
            print('thumbnail skipped:', ex)
        post(cb, {'status': 'completed', 'seconds': round(time.time() - t0),
                  'clips': [{'url': url, 'thumb': thumb, 'thumb_b': thumb_b, 'style': 'Precision Safe-Zone', 'title': hook, 'dur': rep.get('checks', {}).get('duration_s')}],
                  'verify': {'pass': True, 'attempt': rep.get('attempt'), 'checks': rep.get('checks')}})
        print('READY FOR APPROVALS:', url)
    except SystemExit:
        raise
    except Exception as ex:  # any crash still reports back, so a clip never sits in Editing forever
        post(cb, {'status': 'failed', 'error': f'Render job crashed: {ex.__class__.__name__}: {ex}'[:400]})
        raise


if __name__ == '__main__':
    main()
