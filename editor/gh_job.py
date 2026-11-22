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


STAGE = os.environ.get('CLIPFARM_STAGE', '')  # 'fetch' = home PC (residential IP, only YouTube downloads); 'render' = GitHub runner; '' = both in one job
FETCHED = os.path.abspath('fetched')


def fetched_path(source_url, start, end):
    import hashlib
    return os.path.join(FETCHED, hashlib.sha1(f'{source_url}|{float(start)}|{float(end)}'.encode()).hexdigest()[:16] + '.mp4')


SYNC = {}  # v147: source A/V sync measurements for the callback (logged on the Queue row)
MAX_SRC_SYNC_S = 0.08  # source window: audio and video must start together and run the same length (keyframe-snap offsets show up here)


def _ff():
    """ffmpeg binary: PATH first, else the free imageio-ffmpeg build (home PC on Windows)."""
    import shutil
    if shutil.which('ffmpeg'):
        return 'ffmpeg'
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


def src_sync(path):
    """v147 (Newbegin, Nov 22 2026): measures the downloaded window. Returns (start_diff_s, dur_diff_s) or None when ffprobe is missing.
    yt-dlp --download-sections cuts the DASH video and audio streams separately; a cut that snaps the video to an earlier keyframe
    leaves both streams starting at 0 but the picture runs ahead of (or behind) the sound. That shows up as a duration gap."""
    import shutil
    if not shutil.which('ffprobe'):
        return None
    try:
        p = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,start_time,duration', '-show_entries', 'format=duration',
                                       '-of', 'json', path], capture_output=True, text=True, timeout=60).stdout)
        v = next(x for x in p['streams'] if x['codec_type'] == 'video'); a = next(x for x in p['streams'] if x['codec_type'] == 'audio')
        fd = float(p['format']['duration'])
        return (abs(float(v.get('start_time') or 0) - float(a.get('start_time') or 0)),
                abs(float(v.get('duration') or fd) - float(a.get('duration') or fd)))
    except Exception as ex:
        print('src_sync: could not probe', ex)
        return (9.9, 9.9)


def sync_ok(path, label):
    r = src_sync(path)
    if r is None:
        return True
    SYNC.update({'source': label, 'src_av_start_ms': round(r[0] * 1000, 1), 'src_av_duration_ms': round(r[1] * 1000, 1)})
    ok = r[0] <= MAX_SRC_SYNC_S and r[1] <= MAX_SRC_SYNC_S
    print(f'source A/V sync ({label}): start {r[0]*1000:.0f} ms, length {r[1]*1000:.0f} ms ->', 'OK' if ok else 'MISALIGNED')
    return ok


def fetch_exact(source_url, s, e, out):
    """v147: frame-accurate window. yt-dlp only resolves the stream URLs (same bot-wall ladder); ONE ffmpeg pass then seeks the video
    and the audio to the SAME instant and re-encodes both (accurate seek, constant 30 fps), so sound and picture cannot drift apart."""
    import ytfetch
    r = ytfetch.run(['-q', '--no-warnings', '-f', 'bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b', '-j'], 600, source_url)
    if r.returncode != 0 or not (r.stdout or '').strip():
        return False, ([l for l in (r.stderr or '').splitlines() if 'ERROR' in l] or ['could not resolve stream URLs'])[-1]
    info = json.loads(r.stdout.strip().splitlines()[-1])
    fmts = info.get('requested_formats') or [info]
    vf = next((f for f in fmts if f.get('vcodec') not in (None, 'none')), None)
    af = next((f for f in fmts if f.get('acodec') not in (None, 'none')), None)
    if not vf or not af:
        return False, 'no separate video/audio formats'
    dur = (e + 1) - s
    cmd, idx = [_ff(), '-y', '-v', 'error'], {}
    for k, f in (('v', vf), ('a', af)):
        if k == 'a' and af is vf:
            idx['a'] = idx['v']; continue
        hdr = ''.join(f'{hk}: {hv}\r\n' for hk, hv in (f.get('http_headers') or {}).items())
        cmd += (['-headers', hdr] if hdr and str(f['url']).startswith('http') else []) + ['-ss', f'{s:.3f}', '-t', f'{dur:.3f}', '-i', f['url']]
        idx[k] = len(idx)
    cmd += ['-map', f"{idx['v']}:v:0", '-map', f"{idx['a']}:a:0", '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '14', '-pix_fmt', 'yuv420p',
            '-fps_mode', 'cfr', '-r', '30', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-af', 'aresample=async=1:first_pts=0',
            '-t', f'{dur:.3f}', '-movflags', '+faststart', out]
    try:
        x = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
    except subprocess.TimeoutExpired:
        return False, 'exact fetch timed out'
    if x.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 10000:
        return False, ('exact fetch failed: ' + (x.stderr or '')[-300:]).strip()
    return True, 'exact'


def download_window(source_url, start, end, work):
    """Downloads ONLY the licensed window [start-1.5, end+1]. Returns (path, s, e) or raises RuntimeError(reason).
    v121: the home PC (fetch stage) downloads it into fetched/ and hands it to the GitHub render stage as an artifact, so YouTube
    only ever sees a home internet connection. If no pre-fetched file exists, the runner tries itself (ytfetch ladder).
    v147: frame-accurate fetch first (fetch_exact); every window must pass the source A/V sync check before it is rendered."""
    s = max(0.0, float(start) - 1.5); e = float(end)
    pre = fetched_path(source_url, start, end)
    if os.path.exists(pre) and os.path.getsize(pre) > 10000 and STAGE != 'fetch':
        if sync_ok(pre, 'home PC'):
            print('using the clip window fetched by the home PC')
            return pre, s, e
        print('home PC window is out of sync: fetching it again here')
    if STAGE == 'fetch':
        os.makedirs(FETCHED, exist_ok=True)
    src = pre if STAGE == 'fetch' else os.path.join(work, 'src.mp4')
    ok, how = fetch_exact(source_url, s, e, src)
    print('exact fetch:', how)
    if ok and sync_ok(src, 'exact fetch'):
        return src, s, e
    import ytfetch  # bot-wall ladder: cookies, other clients, PO token, no-cookie fallback
    dl = ytfetch.run(['-q', '--no-progress', '-f', 'bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b', '--merge-output-format', 'mp4',
                      '--download-sections', f'*{s}-{e + 1}', '--force-keyframes-at-cuts', '-o', src], 1200, source_url)
    if dl.returncode != 0 or not os.path.exists(src):
        errs = [l for l in (dl.stderr or '').splitlines() if 'ERROR' in l] or (dl.stderr or 'no output').splitlines()[-1:]
        why = errs[-1] if errs else 'unknown'
        hint = ' | fix: refresh the YT_COOKIES secret (cookies expired or signed out)' if re.search(r'sign in|cookies|bot', why, re.I) else ''
        print((dl.stderr or '')[-3000:])
        raise RuntimeError(('Download failed: ' + why.replace('ERROR: ', ''))[:330] + hint)
    if not sync_ok(src, 'yt-dlp sections'):
        raise RuntimeError(f"Source audio/video out of sync (start {SYNC.get('src_av_start_ms')} ms, length {SYNC.get('src_av_duration_ms')} ms): not rendered")
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
    import ytfetch
    r = ytfetch.run(['--simulate', '--no-warnings', '-f', 'bv*[height<=1080]+ba/b'], 300, 'https://www.youtube.com/watch?v=' + vid)
    info['ladder'] = ytfetch.LAST['tried']; info['ok_rung'] = ytfetch.LAST['rung']
    try: info['pot_provider'] = subprocess.run(['curl', '-s', '-m', '5', 'http://127.0.0.1:4416/ping'], capture_output=True, text=True).stdout[:120]
    except Exception: info['pot_provider'] = ''
    info['yt_dlp_version'] = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True).stdout.strip()
    info['yt_dlp_ok'] = r.returncode == 0
    info['yt_dlp_error'] = ([l for l in (r.stderr or '').splitlines() if 'ERROR' in l] or [''])[-1][:300]
    print('DIAG ' + json.dumps(info))


def fetch_stage(p):
    """Home PC: download every YouTube window this job needs into fetched/ (no rendering, no AI, never posts). Moment scans and diag
    run here completely, because they only read YouTube and send their own results to n8n."""
    if p.get('mode') in ('moments', 'diag'):
        moments(p) if p.get('mode') == 'moments' else diag(p)
        os.makedirs(FETCHED, exist_ok=True); open(os.path.join(FETCHED, 'scan_done.txt'), 'w').write('ok')  # tells the GitHub stage not to repeat it
        return
    wins = [(it.get('source_url'), it.get('start'), it.get('end')) for it in (p.get('items') or [])[:10]] if p.get('mode') == 'thumbs' else [(p.get('source_url'), p.get('start'), p.get('end'))]
    for u, a, b in wins:
        if not u or a is None or b is None:
            continue
        try:
            download_window(u, a, b, FETCHED)
            print('fetched', fetched_path(u, a, b))
        except Exception as ex:
            print('home fetch failed (the GitHub stage will try itself):', ex)


def main():
    p, t0 = payload(), time.time()
    if STAGE == 'fetch':
        return fetch_stage(p)
    if STAGE == 'render' and p.get('mode') in ('moments', 'diag') and os.path.exists(os.path.join(FETCHED, 'scan_done.txt')):
        return print('moment scan / diag already ran on the home PC')
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
                            '--hook', hook, '--title', ' '.join(x for x in (p.get('title', ''), str(p.get('context', ''))[:200]) if x), '--clip-id', cid, '--out', work,
                            '--style', 'A', '--words-out', os.path.join(work, 'words.json')], text=True)  # context = campaign + source title, so the GTA 6 check sees the footage source
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
        clips = [{'url': url, 'thumb': thumb, 'thumb_b': thumb_b, 'style': 'Style A · Clean Safe-Zone', 'style_key': 'A', 'title': hook, 'dur': rep.get('checks', {}).get('duration_s'), 'facecam': bool(rep.get('facecam'))}]
        # v150 (Newbegin, Nov 22 2026): every job sends TWO completely different styles to choose from. Style B 'Punch Zoom' reuses the
        # same streamer-only words and facecam box and passes the same verify_edit.py safe-zone law. A Style B failure never blocks Style A.
        try:
            wb = os.path.join(work, 'B'); os.makedirs(wb, exist_ok=True)
            cmd = [sys.executable, os.path.join(HERE, 'render_edit.py'), '--source', src, '--start', '0', '--end', str(e - s), '--hook', hook,
                   '--title', ' '.join(x for x in (p.get('title', ''), str(p.get('context', ''))[:200]) if x), '--clip-id', cid, '--out', wb, '--style', 'B',
                   '--transcript', os.path.join(work, 'words.json')]
            if rep.get('facecam'):
                cmd += ['--facecam', ','.join(str(int(v)) for v in rep['facecam'])]
            rb = subprocess.run(cmd, text=True)
            rpb = os.path.join(wb, f'{cid}.report.json'); repb = json.load(open(rpb)) if os.path.exists(rpb) else {}
            if rb.returncode == 0:
                nb = f'{cid}-{run}-b.mp4'; os.rename(os.path.join(wb, f'{cid}.mp4'), os.path.join(wb, nb)); upload([os.path.join(wb, nb)])
                clips.append({'url': f'https://github.com/{repo}/releases/download/renders/{nb}', 'thumb': thumb, 'thumb_b': thumb_b, 'style': 'Style B · Punch Zoom', 'style_key': 'B',
                              'title': hook, 'dur': repb.get('checks', {}).get('duration_s'), 'facecam': bool(rep.get('facecam'))})
            else:
                print('Style B rejected by verify_edit.py:', '; '.join(repb.get('issues', []) or ['render error']))
        except Exception as ex:
            print('Style B skipped:', ex)
        post(cb, {'status': 'completed', 'seconds': round(time.time() - t0),
                  'clips': clips,
                  'verify': {'pass': True, 'attempt': rep.get('attempt'), 'checks': {**(rep.get('checks') or {}), **SYNC}}})
        print('READY FOR APPROVALS:', url)
    except SystemExit:
        raise
    except Exception as ex:  # any crash still reports back, so a clip never sits in Editing forever
        post(cb, {'status': 'failed', 'error': f'Render job crashed: {ex.__class__.__name__}: {ex}'[:400]})
        raise


if __name__ == '__main__':
    main()
