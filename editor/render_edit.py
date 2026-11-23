#!/usr/bin/env python3
"""render_edit.py: Clip Farm precision editor ($0: FFmpeg, ffprobe, faster-whisper, OpenCV, Pillow).

  cut -> transcribe (faster-whisper, word timestamps) -> facecam detect -> compose 9:16 base
  -> render hook + word-synced captions in their safe bands -> verify_edit.py
  -> on failure: shrink fonts / rewrap / shorter chunks and re-render (auto-revision)
  -> PASS: final.mp4 + report.json (ready for Approvals)   FAIL after all revisions: rejected, never shown.

Example:
  python render_edit.py --source vod.mp4 --start 338 --end 372 \
      --hook "Can you do barrel rolls in GTA 6?" --clip-id XRo2L3077NI-338 --out out/
Options:
  --facecam x,y,w,h   facecam box in SOURCE pixels (else auto-detected; none found = gameplay-only layout)
  --transcript f.json [{"w":"word","s":0.0,"e":0.4},...] seconds relative to --start (skips Whisper; tests/offline)
  --whisper-model small.en   any faster-whisper model name or local path (CPU int8 by default)
"""
import argparse, json, os, re, subprocess, sys, shutil
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout as L
from verify_edit import verify

GTA_RX = re.compile(r'\bgta ?(6|vi)\b|grand theft auto (6|vi)\b|vice city|leonida', re.I)


def sh(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(' '.join(cmd[:6]) + ' ...\n' + r.stderr[-1500:])
    return r.stdout


def probe_size(path):
    s = json.loads(sh(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', path]))['streams'][0]
    return s['width'], s['height']


# ---------------- transcription
def transcribe(seg, model_name):
    from faster_whisper import WhisperModel  # local, free
    model = WhisperModel(model_name, device='auto', compute_type='int8')
    segs, _ = model.transcribe(seg, word_timestamps=True, vad_filter=True, language='en')
    words = []
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append({'w': t, 's': round(w.start, 3), 'e': round(w.end, 3)})
    return words


def chunk_words(words, max_words):
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1]['s'] - w['e'] if i + 1 < len(words) else 9
        if len(cur) >= max_words or gap > 0.35 or re.search(r'[.!?,]$', w['w']):
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    out = []
    for j, c in enumerate(chunks):
        nxt = chunks[j + 1][0]['s'] if j + 1 < len(chunks) else c[-1]['e'] + 0.4
        out.append({'text': ' '.join(x['w'] for x in c).upper(), 'word_start': c[0]['s'], 'start': c[0]['s'], 'end': min(nxt, c[-1]['e'] + 0.4)})
    return out


# ---------------- facecam
def detect_facecam(seg, sw, sh_):
    import cv2, numpy as np
    try:
        casc = cv2.CascadeClassifier(os.path.join(getattr(getattr(cv2, 'data', None), 'haarcascades', ''), 'haarcascade_frontalface_default.xml'))
    except Exception:
        casc = None
    if casc is None or casc.empty():
        print('[render] face detector unavailable: gameplay-only layout (pass --facecam or add training_examples/layouts/<channel>.json)', flush=True)
        return None
    hits = []
    cap = cv2.VideoCapture(seg)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    for k in range(6):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (k + 0.5) / 6))
        ok, f = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        fs = casc.detectMultiScale(g, 1.1, 6, minSize=(int(sh_ * 0.06),) * 2)
        if len(fs):
            hits.append(max(fs, key=lambda b: b[2] * b[3]))
    cap.release()
    if len(hits) < 4:
        return None
    # A streamer webcam sits still in one corner. In-game faces (NPCs, cutscenes) move around or sit mid-frame:
    # require the detections to agree within 6% of the frame and the median face to be outside the central 40%.
    cx = [b[0] + b[2] / 2 for b in hits]; cy = [b[1] + b[3] / 2 for b in hits]
    if (max(cx) - min(cx)) > 0.06 * sw or (max(cy) - min(cy)) > 0.06 * sh_:
        return None
    mx, my = float(np.median(cx)), float(np.median(cy))
    if abs(mx - sw / 2) < 0.2 * sw and abs(my - sh_ / 2) < 0.2 * sh_:
        return None
    x, y, w, h = [int(np.median([b[i] for b in hits])) for i in range(4)]
    # webcam box ~ 2.6x the face, 16:9, clamped to the frame: keeps eyes and mouth well inside
    cw = int(w * 2.6); ch = int(cw * 9 / 16)
    cx, cy = x + w // 2, y + h // 2
    x0 = max(0, min(sw - cw, cx - cw // 2)); y0 = max(0, min(sh_ - ch, cy - ch // 2))
    return [x0, y0, min(cw, sw), min(ch, sh_)]


# ---------------- text layout (Pillow)
def wrap(text, font, max_w, max_lines):
    words, lines, cur = text.split(), [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if font.getlength(t) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    ok = len(lines) <= max_lines and all(font.getlength(l) <= max_w for l in lines)
    return lines, ok


def fit_text(text, zone, start_px, max_lines, stroke_ratio=0.09):
    """Largest font (<= start_px) whose wrapped block, INCLUDING stroke, fits inside zone and x 90-990."""
    px = start_px
    while px >= L.MIN_FONT_PX:
        font = ImageFont.truetype(L.FONT_BOLD, px)
        stroke = max(3, int(px * stroke_ratio))
        max_w = (L.SAFE_X[1] - L.SAFE_X[0]) - 2 * stroke - 8
        lines, ok = wrap(text, font, max_w, max_lines)
        lh = int(px * 1.12)
        block_h = lh * len(lines) + 2 * stroke
        if ok and block_h <= (zone[1] - zone[0]) - 6:
            return {'px': px, 'stroke': stroke, 'lines': lines, 'lh': lh, 'block_h': block_h}
        px -= 2
    return None


def draw_text_png(path, fit, zone, fill=(255, 255, 255), stroke_fill=(0, 0, 0), badge=None, plate=None):
    img = Image.new('RGBA', (L.W, L.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(L.FONT_BOLD, fit['px'])
    top = zone[0] + ((zone[1] - zone[0]) - fit['block_h']) // 2 + fit['stroke']
    if plate:  # Style B: solid hook plate, clamped inside the hook band and x 90-990 (verify_edit.py checks every pixel)
        bx = [d.textbbox((L.W // 2, top + i * fit['lh']), ln, font=font, anchor='ma', stroke_width=fit['stroke']) for i, ln in enumerate(fit['lines'])]
        x0, x1 = max(L.SAFE_X[0] + 2, min(b[0] for b in bx) - 22), min(L.SAFE_X[1] - 2, max(b[2] for b in bx) + 22)
        y0, y1 = max(L.HOOK[0] + 2, min(b[1] for b in bx) - 12), min(L.HOOK[1] - 2, max(b[3] for b in bx) + 12)
        d.rounded_rectangle((x0, y0, x1, y1), radius=18, fill=plate)
    boxes = []
    for i, line in enumerate(fit['lines']):
        y = top + i * fit['lh']
        d.text((L.W // 2, y), line, font=font, fill=fill, anchor='ma', stroke_width=fit['stroke'], stroke_fill=stroke_fill)
        boxes.append(d.textbbox((L.W // 2, y), line, font=font, anchor='ma', stroke_width=fit['stroke']))
    if badge:  # on-screen paid-partnership disclosure, whole clip, right side of the badge strip (never over gameplay)
        bf = ImageFont.truetype(L.FONT_BOLD, 22); tw = int(bf.getlength(badge)); x1 = L.SAFE_X[1] - 4; x0 = x1 - tw - 24
        d.rounded_rectangle((x0, L.BADGE[0] + 1, x1, L.BADGE[1] - 1), radius=8, fill=(0, 0, 0, 200), outline=(255, 255, 255), width=2)
        d.text(((x0 + x1) // 2, (L.BADGE[0] + L.BADGE[1]) // 2), badge, font=bf, fill=(255, 255, 255), anchor='mm')
        boxes.append((x0, L.BADGE[0] + 1, x1, L.BADGE[1] - 1))
    img.save(path)
    return boxes


# ---------------- composition
def base_filter(cam, sw, sh_, style='A', fx=None):
    if style == 'X':
        return base_filter_x(cam, sw, sh_, fx or {})
    if style == 'B':
        return base_filter_b(cam, sw, sh_)
    """Background: blurred, darkened gameplay fill. CENTER zone (400-1320): facecam on top + full 16:9 gameplay (HUD intact)."""
    zone_h = L.CENTER[1] - L.CENTER[0]               # 920
    game_h = int(L.W * 9 / 16) // 2 * 2              # 1080x606 keeps the whole HUD/minimap
    f = [f"[0:v]scale={L.W}:{L.H}:force_original_aspect_ratio=increase,crop={L.W}:{L.H},boxblur=28:2,eq=brightness=-0.32[bg]",
         f"[0:v]scale={L.W}:{game_h}[game]"]
    if cam:
        x, y, w, h = cam
        cam_h = (zone_h - game_h) // 2 * 2            # 314 px for the facecam strip
        cam_w = min(L.W, int(cam_h * w / h)) // 2 * 2
        f.append(f"[0:v]crop={w}:{h}:{x}:{y},scale={cam_w}:{cam_h}[cam]")
        f.append(f"[bg][cam]overlay=x=({L.W}-{cam_w})/2:y={L.CENTER[0]}[b1]")
        f.append(f"[b1][game]overlay=x=0:y={L.CENTER[0] + cam_h}[base]")
    else:
        f.append(f"[bg][game]overlay=x=0:y={L.CENTER[0] + (zone_h - game_h) // 2}[base]")
    return ';'.join(f)


def base_filter_b(cam, sw, sh_):
    """Style B 'Punch Zoom' (v150): a completely different look from Style A. Dark solid backdrop, gameplay ZOOMED to fill the
    whole CENTER zone (1080x920, centre of the action, bigger on a phone), facecam as a white-bordered inset in the top-right of
    the gameplay. Everything stays inside CENTER 400-1320; no text there (verify_edit.py checks)."""
    zone_h = L.CENTER[1] - L.CENTER[0]
    gw = max(L.W, int(round(sw * zone_h / sh_ / 2)) * 2)
    f = [f"[0:v]scale={L.W}:{L.H}:force_original_aspect_ratio=increase,crop={L.W}:{L.H},boxblur=40:3,eq=brightness=-0.62:saturation=0.35[bg]",
         f"[0:v]scale={gw}:{zone_h},crop={L.W}:{zone_h}[game]",
         f"[bg][game]overlay=x=0:y={L.CENTER[0]}[b1]"]
    if cam:
        x, y, w, h = cam
        cw = 360; ch = int(cw * h / w) // 2 * 2
        if ch > 330:
            ch = 330; cw = int(ch * w / h) // 2 * 2
        f.append(f"[0:v]crop={w}:{h}:{x}:{y},scale={cw}:{ch},pad={cw + 12}:{ch + 12}:6:6:white[cam]")
        f.append(f"[b1][cam]overlay=x={L.W - cw - 12 - 28}:y={L.CENTER[0] + 28}[base]")
    else:
        f[-1] = f[-1].replace('[b1]', '[base]')
    return ';'.join(f)


def audio_peaks(seg, dur, max_n=6, gap=3.5):
    """v175 Hype FX: the loudest moments (streamer yelling, explosions, crashes) from the clip's own audio. Returns hit times
    (seconds, loudest first). $0, no AI: 50 ms RMS loudness, smoothed, local maxima above the 88th percentile, at least `gap` s apart."""
    try:
        import numpy as np
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', seg, '-ac', '1', '-ar', '8000', '-f', 's16le', '-'], capture_output=True).stdout
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        win = 400; n = a.size // win
        if n < 40:
            return []
        db = 20 * np.log10(np.sqrt((a[:n * win].reshape(n, win) ** 2).mean(1)) + 1)
        sm = np.convolve(db, np.ones(4) / 4, 'same')
        thr = max(float(np.percentile(sm, 88)), float(np.median(sm)) + 6)
        def onset(i):  # hit on the moment it gets loud, not the middle of the yell
            while i > 0 and sm[i - 1] >= thr:
                i -= 1
            return i * 0.05
        cand = sorted(((float(sm[i]), onset(i)) for i in range(1, n - 1) if sm[i] >= thr and sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1]), reverse=True)
        picks = []
        for v, t in cand:
            if 0.9 <= t <= dur - 1.2 and all(abs(t - p) >= gap for p in picks):
                picks.append(round(max(0.0, t - 0.04), 2))
            if len(picks) >= max_n:
                break
        return picks
    except Exception as ex:
        print('[render] audio peaks skipped:', ex, flush=True)
        return []


def _env(p, rise=0.09, hold=0.5, fall=0.35):
    return f"clip((it-{p})/{rise},0,1)*(1-clip((it-{p}-{hold})/{fall},0,1))"


def base_filter_x(cam, sw, sh_, fx):
    """v175 'Hype FX' (Richard, Nov 23 2026): ONE high-energy edit per clip. Gameplay fills the CENTER zone, graded (contrast, colour, sharpen),
    an opening punch-out, audio-reactive punch-in zooms on every big moment, camera shake + a quick white flash on the two biggest hits, and the
    facecam as a white-bordered inset. All effects live in the base picture, inside CENTER 400-1320; text stays in its safe bands (verify_edit.py)."""
    zone_h = L.CENTER[1] - L.CENTER[0]
    peaks = list(fx.get('peaks') or [])[:6]
    big = peaks[:2]
    zoom = '1+0.10*(1-clip(it/0.45,0,1))' + ''.join(f"+0.15*{_env(p)}" for p in peaks)
    shk = '+'.join(_env(p, hold=0.35, fall=0.25) for p in big) or '0'
    flash = '+'.join(f"0.22*between(t,{p},{p + 0.1})*(1-(t-{p})/0.1)" for p in big) or '0'
    gw = max(L.W, int(round(sw * zone_h / sh_ / 2)) * 2)
    up = 1.5
    f = [f"[0:v]scale={L.W}:{L.H}:force_original_aspect_ratio=increase,crop={L.W}:{L.H},boxblur=40:3,eq=brightness=-0.6:saturation=0.4,fps={L.FPS}[bg]",
         f"[0:v]fps={L.FPS},scale={int(gw * up) // 2 * 2}:{int(zone_h * up) // 2 * 2},crop={int(L.W * up) // 2 * 2}:{int(zone_h * up) // 2 * 2},"
         f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)+({shk})*22*sin(it*57)':y='ih/2-(ih/zoom/2)+({shk})*16*cos(it*49)':d=1:s={L.W}x{zone_h}:fps={L.FPS},"
         f"eq=contrast=1.1:saturation=1.3,unsharp=5:5:0.55[game]",
         f"[bg][game]overlay=x=0:y={L.CENTER[0]}[b1]"]
    last = 'b1'
    if cam:
        x, y, w, h = cam
        cw = 360; ch = int(cw * h / w) // 2 * 2
        if ch > 330:
            ch = 330; cw = int(ch * w / h) // 2 * 2
        f.append(f"[0:v]fps={L.FPS},crop={w}:{h}:{x}:{y},scale={cw}:{ch},eq=contrast=1.06:saturation=1.15,pad={cw + 12}:{ch + 12}:6:6:white[cam]")
        f.append(f"[b1][cam]overlay=x={L.W - cw - 12 - 28}:y={L.CENTER[0] + 28}[b2]")
        last = 'b2'
    f.append(f"[{last}]eq=brightness='{flash}':eval=frame[base]")
    return ';'.join(f)


# The two editor styles sent together for every clip (v150, Newbegin): completely different looks to choose from.
STYLES = {
    'A': {'name': 'Clean Safe-Zone', 'hook_fill': (255, 230, 0), 'plate': None, 'cap_fill': (255, 255, 255), 'cap_px': L.CAP_FONT_PX, 'cap_words': L.CAP_MAX_WORDS},
    'B': {'name': 'Punch Zoom', 'hook_fill': (255, 255, 255), 'plate': (220, 20, 60, 255), 'cap_fill': (255, 230, 0), 'cap_px': 88, 'cap_words': 2},
    # v175 (Richard): ONE edit per clip from now on, the high-level effects style.
    'X': {'name': 'Hype FX', 'hook_fill': (255, 255, 255), 'plate': (220, 20, 60, 255), 'cap_fill': (255, 230, 0), 'cap_px': 88, 'cap_words': 2},
}


def render(seg, cam, sw, sh_, hook_png, caps, clean, final, style='A', fx=None):
    fc = base_filter(cam, sw, sh_, style, fx)
    enc = ['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-r', str(L.FPS), '-c:a', 'aac', '-b:a', '160k', '-ar', '48000']
    # clean = identical edit with no text (verifier diffs against it); near-lossless so only our text differs
    sh(['ffmpeg', '-y', '-v', 'error', '-i', seg, '-filter_complex', fc, '-map', '[base]', '-map', '0:a', '-c:v', 'libx264', '-qp', '0', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-r', str(L.FPS), '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', clean])
    ins, chain, last = ['-i', clean, '-loop', '1', '-i', hook_png], [f"[0:v][1:v]overlay=0:0:shortest=1[v1]"], 'v1'
    for i, c in enumerate(caps):
        ins += ['-loop', '1', '-i', c['png']]
        chain.append(f"[{last}][{i + 2}:v]overlay=0:0:shortest=1:enable='between(t,{c['start']:.3f},{c['end'] - 0.001:.3f})'[v{i + 2}]")
        last = f'v{i + 2}'
    sh(['ffmpeg', '-y', '-v', 'error'] + ins + ['-filter_complex', ';'.join(chain), '-map', f'[{last}]', '-map', '0:a', '-crf', '17'] + enc + ['-movflags', '+faststart', final])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True); ap.add_argument('--start', type=float, required=True); ap.add_argument('--end', type=float, required=True)
    ap.add_argument('--hook', required=True); ap.add_argument('--clip-id', default='clip'); ap.add_argument('--out', default='out')
    ap.add_argument('--facecam'); ap.add_argument('--transcript'); ap.add_argument('--whisper-model', default='small.en')
    ap.add_argument('--title', default=''); ap.add_argument('--max-revisions', type=int, default=4); ap.add_argument('--allow-non-gta', action='store_true')
    ap.add_argument('--style', default='A', choices=sorted(STYLES)); ap.add_argument('--words-out')
    a = ap.parse_args()
    ST = STYLES[a.style]
    os.makedirs(a.out, exist_ok=True)
    work = os.path.join(a.out, 'work'); os.makedirs(work, exist_ok=True)
    log = lambda *m: print('[render]', *m, flush=True)

    if not a.allow_non_gta and not GTA_RX.search(a.hook + ' ' + a.title):
        log('REJECTED: hook/title does not identify GTA 6 footage (GTA 6 only).'); sys.exit(2)
    hook_words = a.hook.split()
    if len(hook_words) > L.HOOK_MAX_WORDS:
        log(f'REJECTED: hook has {len(hook_words)} words (max {L.HOOK_MAX_WORDS}). Shorten it upstream.'); sys.exit(2)

    seg = os.path.join(work, 'seg.mp4')
    sh(['ffmpeg', '-y', '-v', 'error', '-ss', str(a.start), '-to', str(a.end), '-i', a.source, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '14', '-c:a', 'aac', '-ar', '48000', '-af', 'aresample=async=1:first_pts=0', '-avoid_negative_ts', 'make_zero', seg])
    sw, sh_ = probe_size(seg)
    words = json.load(open(a.transcript)) if a.transcript else transcribe(seg, a.whisper_model)
    log(f'{len(words)} spoken words')
    cam = [int(v) for v in a.facecam.split(',')] if a.facecam else detect_facecam(seg, sw, sh_)
    log('facecam', cam or 'none (gameplay-only layout)')
    # v148 (Newbegin, Nov 22 2026): talking captions are the STREAMER's words only, never game characters (speaker.py, $0, no AI).
    spk = {'method': 'not run', 'phrases': 0, 'kept': 0, 'dropped': 0}
    if not a.transcript:
        try:
            from speaker import streamer_only
            words, spk = streamer_only(seg, words, cam)
        except Exception as ex:  # never caption unverified speech: on a failure the clip gets no talking captions
            log('speaker filter failed, no talking captions:', ex); words, spk = [], {'method': 'failed: ' + str(ex)[:80], 'phrases': 0, 'kept': 0, 'dropped': 0}
    if a.words_out:  # Style B reuses the streamer-only words (same seg), so both styles caption the same speech
        json.dump(words, open(a.words_out, 'w'))
    log(f"style {a.style} ({ST['name']})")
    log(f"captions: streamer only · {spk['method']} · kept {spk['kept']}/{spk['phrases']} phrases" + (f" · dropped: {spk.get('dropped_text', '')}" if spk.get('dropped') else ''))

    fx = {}
    if a.style == 'X':
        fx['peaks'] = audio_peaks(seg, a.end - a.start)
        log('hype fx: punch-ins at', fx['peaks'] or 'none (flat audio)', '| shake + flash at', fx['peaks'][:2] or 'none')
    hook_px, cap_px, max_words = L.HOOK_FONT_PX, ST['cap_px'], ST['cap_words']
    final, clean = os.path.join(a.out, f'{a.clip_id}.mp4'), os.path.join(work, 'clean.mp4')
    report = None
    for attempt in range(1, a.max_revisions + 2):
        # v108: no paid-partnership badge (no ad partnership). With no badge the hook uses the whole 220-380 band.
        # Set CLIPFARM_AD_BADGE (e.g. "#AD · PAID PARTNERSHIP") only for a real paid partnership; verify_edit.py then checks it on every frame.
        badge = os.environ.get('CLIPFARM_AD_BADGE', '').strip() or None
        hook_zone = L.HOOK_TEXT if badge else L.HOOK
        hf = fit_text(a.hook.upper(), (hook_zone[0] + 14, hook_zone[1] - 14) if ST['plate'] else hook_zone, hook_px, L.HOOK_MAX_LINES)
        if not hf:
            log('REJECTED: hook cannot fit 220-380 inside x 90-990'); sys.exit(2)
        hook_png = os.path.join(work, 'hook.png')
        hook_boxes = draw_text_png(hook_png, hf, hook_zone, fill=ST['hook_fill'], badge=badge, plate=ST['plate'])
        caps = chunk_words(words, max_words)
        for i, c in enumerate(caps):
            cf = fit_text(c['text'], L.CAPS, cap_px, 2)
            if not cf:
                cf = fit_text(c['text'], L.CAPS, L.MIN_FONT_PX, 2)
            if not cf:
                log(f'REJECTED: caption "{c["text"]}" cannot fit'); sys.exit(2)
            c['png'] = os.path.join(work, f'cap{i:03d}.png'); c['lines'] = cf['lines']; c['px'] = cf['px']
            c['boxes'] = draw_text_png(c['png'], cf, L.CAPS, fill=ST['cap_fill'])
        plan = {'clip_id': a.clip_id, 'hook': {'text': a.hook.upper(), 'lines': hf['lines'], 'px': hf['px'], 'boxes': hook_boxes},
                'captions': [{k: c[k] for k in ('text', 'start', 'end', 'word_start', 'lines', 'px', 'boxes')} for c in caps], 'facecam': cam, 'attempt': attempt, 'style': ST['name'], **({'disclosure': badge} if badge else {})}
        json.dump(plan, open(os.path.join(work, 'plan.json'), 'w'), indent=1)
        render(seg, cam, sw, sh_, hook_png, caps, clean, final, a.style, fx)
        report = verify(final, clean, plan)
        report['attempt'] = attempt
        log(f'attempt {attempt}:', 'PASS' if report['pass'] else 'FAIL ' + '; '.join(report['issues']))
        if report['pass']:
            break
        # auto-revision: smaller type, shorter chunks
        hook_px, cap_px = int(hook_px * 0.9), int(cap_px * 0.9)
        max_words = max(2, max_words - 1)
    report.update({'clip_id': a.clip_id, 'final': final, 'hook': a.hook, 'facecam': cam, 'words': len(words)})
    report.setdefault('checks', {}).update({'captions_speaker': 'streamer only: ' + spk['method'], 'caption_phrases_kept': spk['kept'], 'caption_phrases_dropped': spk['dropped']})
    json.dump(report, open(os.path.join(a.out, f'{a.clip_id}.report.json'), 'w'), indent=2)
    if not report['pass']:
        os.replace(final, os.path.join(work, 'rejected.mp4'))
        log('REJECTED after auto-revisions: never sent to Approvals'); sys.exit(1)
    log('READY FOR APPROVALS:', final)


if __name__ == '__main__':
    main()
