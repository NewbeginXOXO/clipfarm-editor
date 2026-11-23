"""thumbs.py: Thumbnail Option A + Option B for every verified edit ($0: FFmpeg + Pillow, no AI).

Thumbnail law (v111):
  * NEVER any spoken/talking captions or subtitle lines. Frames come from the CLEAN 9:16 composition (same facecam +
    gameplay layout as the edit, zero text), never from the final MP4 (which has the word-synced captions burned in).
  * ONE punchy hook only, at the top, inside the hook band y 220-380 and x 90-990: below the platform's top UI (0-210),
    above the facecam / GTA 6 gameplay zone (400-1320), so it never covers faces, reactions, the action or the HUD.
  * Sharp and clean: full 1080x1920 JPG, high-contrast text, nothing overlapping.
Option A and Option B differ in moment (best frame in the first half vs the second half of the clip) and in look
(A: yellow hook with a heavy outline, the same look as the video hook; B: white hook on a solid black plate).
"""
import json, os, re, subprocess
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

import layout as L

BAND = L.HOOK                      # (220, 380): the only place thumbnail text may go
MAX_WORDS = 7


def duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', path], capture_output=True, text=True, timeout=60)
    return float(json.loads(r.stdout or '{}').get('format', {}).get('duration') or 0)


def frame(src, t, out, fc=None):
    """One full-size 1080x1920 frame with NO text. src is the clean edit (fc None) or the raw source window (fc = base_filter)."""
    cmd = ['ffmpeg', '-y', '-v', 'error', '-ss', f'{max(0.0, t):.2f}', '-i', src]
    cmd += (['-filter_complex', fc, '-map', '[base]'] if fc else ['-vf', f'scale={L.W}:{L.H}'])
    subprocess.run(cmd + ['-frames:v', '1', out], check=True, timeout=180)
    return Image.open(out).convert('RGB')


def score(im):
    """Sharp, busy, contrasty action in the center zone scores high; blurry or flat frames score low."""
    c = im.crop((0, L.CENTER[0], L.W, L.CENTER[1])).resize((360, 307)).convert('L')
    return ImageStat.Stat(c.filter(ImageFilter.FIND_EDGES)).mean[0] + 0.35 * ImageStat.Stat(c).stddev[0]


def pick_two(src, dur, work, fc=None, n=10):
    """Best frame in the first half (A) and in the second half (B), at least 25% of the clip apart."""
    lo, hi = 0.8, max(1.0, dur - 0.8)
    ts = [lo + (hi - lo) * k / (n - 1) for k in range(n)]
    cand = []
    for k, t in enumerate(ts):
        p = os.path.join(work, f'cand{k:02d}.png')
        try:
            im = frame(src, t, p, fc); cand.append((score(im), t, p))
        except Exception as ex:
            print('thumb frame skipped', t, ex)
    if not cand:
        raise RuntimeError('no frames')
    half = dur / 2
    a = max([c for c in cand if c[1] <= half] or cand)
    far = [c for c in cand if c[1] > half and abs(c[1] - a[1]) >= max(2.0, dur * 0.25)] or [c for c in cand if c is not a] or [a]
    b = max(far)
    return a, b


# v149 (Newbegin, Nov 22 2026): a hook cut to 7 words must not end on a dangling word ("...GOES COMPLETELY WRONG IN").
END_STOP = {'in', 'on', 'at', 'of', 'to', 'for', 'with', 'from', 'by', 'into', 'onto', 'as', 'and', 'or', 'but', 'so', 'the', 'a', 'an',
            'is', 'are', 'was', 'were', 'be', 'his', 'her', 'their', 'my', 'your', 'our', 'its', 'this', 'that', 'than', 'then', 'when', 'while', 'after', 'before', 'gta'}


def tidy_end(words, cut):
    """Drops trailing filler/prepositions (and a lone 'GTA' whose '6' was cut off) so the hook reads as a complete phrase."""
    words = list(words)
    while len(words) > 2 and words[-1].strip(',.;:-').lower() in END_STOP and (cut or words[-1].strip(',.;:-').lower() != 'gta'):
        words.pop()
    if words:
        words[-1] = words[-1].rstrip(',;:-')
    return words


def clean_hook(text):
    t = re.sub(r'#\w+', ' ', str(text or ''))
    t = re.sub(r'[^\w\s?!\'.,&-]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip().upper()
    w = t.split()
    return ' '.join(tidy_end(w[:MAX_WORDS], len(w) > MAX_WORDS))


def fit(text, start_px=92, min_px=46, plate=0):
    """Largest font whose <= 2 lines (with outline / plate padding) fit the hook band and x 90-990."""
    words = text.split()
    for px in range(start_px, min_px - 1, -2):
        f = ImageFont.truetype(L.FONT_BOLD, px); stroke = max(4, int(px * 0.11)); pad = plate
        max_w = (L.SAFE_X[1] - L.SAFE_X[0]) - 2 * (stroke + pad) - 8
        lines, cur = [], ''
        for w in words:
            nxt = (cur + ' ' + w).strip()
            if f.getlength(nxt) <= max_w: cur = nxt
            else:
                if cur: lines.append(cur)
                cur = w
        if cur: lines.append(cur)
        lh = int(px * 1.08); block = lh * len(lines) + 2 * (stroke + pad)
        if len(lines) <= 2 and all(f.getlength(l) <= max_w for l in lines) and block <= (BAND[1] - BAND[0]) - 8:
            return f, px, stroke, lines, lh, block
    return None


def draw(im, text, style):
    """Draws the ONE hook. Returns the text boxes so the caller can prove they stay inside the band and safe x."""
    hook = clean_hook(text)
    if not hook:
        return im, []
    d = ImageDraw.Draw(im, 'RGBA')
    plate = 22 if style == 'B' else 0
    got = fit(hook, plate=plate)
    if not got:
        raise RuntimeError('hook cannot fit the 220-380 band')
    f, px, stroke, lines, lh, block = got
    top = BAND[0] + ((BAND[1] - BAND[0]) - block) // 2 + stroke + plate
    cx = L.W // 2
    boxes = [d.textbbox((cx, top + i * lh), l, font=f, anchor='ma', stroke_width=stroke) for i, l in enumerate(lines)]
    if style == 'B':
        x0 = max(L.SAFE_X[0], min(b[0] for b in boxes) - plate); x1 = min(L.SAFE_X[1], max(b[2] for b in boxes) + plate)
        y0 = max(BAND[0], min(b[1] for b in boxes) - plate); y1 = min(BAND[1], max(b[3] for b in boxes) + plate)
        d.rounded_rectangle((x0, y0, x1, y1), radius=22, fill=(8, 8, 10, 240))
        d.rectangle((x0 + 22, y1 - 7, x1 - 22, y1 - 3), fill=(255, 214, 0, 255))   # thin accent line, inside the plate
        boxes.append((x0, y0, x1, y1))
        for i, l in enumerate(lines):
            d.text((cx, top + i * lh), l, font=f, fill=(255, 255, 255), anchor='ma', stroke_width=max(2, stroke // 3), stroke_fill=(0, 0, 0))
    else:
        for i, l in enumerate(lines):  # soft drop shadow, then the outlined hook (the video hook's look)
            d.text((cx + 4, top + i * lh + 5), l, font=f, fill=(0, 0, 0, 150), anchor='ma', stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        for i, l in enumerate(lines):
            d.text((cx, top + i * lh), l, font=f, fill=(255, 230, 0), anchor='ma', stroke_width=stroke, stroke_fill=(0, 0, 0))
    return im, boxes


def check(boxes):
    bad = [b for b in boxes if b[1] < BAND[0] or b[3] > BAND[1] or b[0] < L.SAFE_X[0] or b[2] > L.SAFE_X[1]]
    if bad:
        raise RuntimeError(f'thumbnail text outside the hook band / safe x: {bad}')


def make(src, t, jpg, text, style, work, fc=None):
    im = frame(src, t, os.path.join(work, f'opt{style}.png'), fc)
    im, boxes = draw(im, text, style)
    check(boxes)
    im.save(jpg, quality=92, optimize=True, progressive=True, subsampling=0)
    return os.path.getsize(jpg) > 5000


def pick_new(src, dur, work, avoid, fc=None, n=16):
    """v174 "New image": the best-scoring frame at least 1.5 s away from every frame already used."""
    lo, hi = 0.8, max(1.0, dur - 0.8)
    best = None
    for k in range(n):
        tt = lo + (hi - lo) * k / (n - 1)
        if any(abs(tt - float(a)) < 1.5 for a in avoid):
            continue
        p = os.path.join(work, f'new{k:02d}.png')
        try:
            c = (score(frame(src, tt, p, fc)), tt)
            best = c if best is None or c > best else best
        except Exception as ex:
            print('thumb frame skipped', tt, ex)
    return best[1] if best else None


def make_options(src, out_stem, hook_a, hook_b, work, fc=None, only=None, t=None, text=None, suffix='', avoid=None):
    """Writes <out_stem>-A.jpg and <out_stem>-B.jpg (or just one option when revising). Returns {A: (path, t, text), B: ...}."""
    os.makedirs(work, exist_ok=True)
    dur = duration(src)
    res = {}
    if only in ('A', 'B') and t is not None:
        times = {only: float(t)}
    elif only in ('A', 'B') and avoid is not None:
        (_, ta0, _), (_, tb0, _) = pick_two(src, dur, work, fc)  # the original best frame is never offered again
        tn = pick_new(src, dur, work, list(avoid) + [ta0 if only == 'A' else tb0], fc)
        if tn is None:
            raise RuntimeError('no new frame left (every part of the clip was already used)')
        times = {only: tn}
    else:
        (_, ta, _), (_, tb, _) = pick_two(src, dur, work, fc)
        times = {'A': ta, 'B': tb}
        if only in ('A', 'B'):
            times = {only: times[only]}
    for opt, tt in times.items():
        txt = text if (only == opt and text) else (hook_a if opt == 'A' else (hook_b or hook_a))
        jpg = f'{out_stem}-{opt}{suffix}.jpg'
        if make(src, min(tt, max(0.0, dur - 0.3)), jpg, txt, opt, work, fc):
            res[opt] = (jpg, round(tt, 1), clean_hook(txt))
    return res
