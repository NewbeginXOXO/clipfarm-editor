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


def draw_text_png(path, fit, zone, fill=(255, 255, 255), stroke_fill=(0, 0, 0), badge=None):
    img = Image.new('RGBA', (L.W, L.H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(L.FONT_BOLD, fit['px'])
    top = zone[0] + ((zone[1] - zone[0]) - fit['block_h']) // 2 + fit['stroke']
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
def base_filter(cam, sw, sh_):
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


def render(seg, cam, sw, sh_, hook_png, caps, clean, final):
    fc = base_filter(cam, sw, sh_)
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
    a = ap.parse_args()
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

    hook_px, cap_px, max_words = L.HOOK_FONT_PX, L.CAP_FONT_PX, L.CAP_MAX_WORDS
    final, clean = os.path.join(a.out, f'{a.clip_id}.mp4'), os.path.join(work, 'clean.mp4')
    report = None
    for attempt in range(1, a.max_revisions + 2):
        # v108: no paid-partnership badge (no ad partnership). With no badge the hook uses the whole 220-380 band.
        # Set CLIPFARM_AD_BADGE (e.g. "#AD · PAID PARTNERSHIP") only for a real paid partnership; verify_edit.py then checks it on every frame.
        badge = os.environ.get('CLIPFARM_AD_BADGE', '').strip() or None
        hook_zone = L.HOOK_TEXT if badge else L.HOOK
        hf = fit_text(a.hook.upper(), hook_zone, hook_px, L.HOOK_MAX_LINES)
        if not hf:
            log('REJECTED: hook cannot fit 220-380 inside x 90-990'); sys.exit(2)
        hook_png = os.path.join(work, 'hook.png')
        hook_boxes = draw_text_png(hook_png, hf, hook_zone, fill=(255, 230, 0), badge=badge)
        caps = chunk_words(words, max_words)
        for i, c in enumerate(caps):
            cf = fit_text(c['text'], L.CAPS, cap_px, 2)
            if not cf:
                cf = fit_text(c['text'], L.CAPS, L.MIN_FONT_PX, 2)
            if not cf:
                log(f'REJECTED: caption "{c["text"]}" cannot fit'); sys.exit(2)
            c['png'] = os.path.join(work, f'cap{i:03d}.png'); c['lines'] = cf['lines']; c['px'] = cf['px']
            c['boxes'] = draw_text_png(c['png'], cf, L.CAPS)
        plan = {'clip_id': a.clip_id, 'hook': {'text': a.hook.upper(), 'lines': hf['lines'], 'px': hf['px'], 'boxes': hook_boxes},
                'captions': [{k: c[k] for k in ('text', 'start', 'end', 'word_start', 'lines', 'px', 'boxes')} for c in caps], 'facecam': cam, 'attempt': attempt, **({'disclosure': badge} if badge else {})}
        json.dump(plan, open(os.path.join(work, 'plan.json'), 'w'), indent=1)
        render(seg, cam, sw, sh_, hook_png, caps, clean, final)
        report = verify(final, clean, plan)
        report['attempt'] = attempt
        log(f'attempt {attempt}:', 'PASS' if report['pass'] else 'FAIL ' + '; '.join(report['issues']))
        if report['pass']:
            break
        # auto-revision: smaller type, shorter chunks
        hook_px, cap_px = int(hook_px * 0.9), int(cap_px * 0.9)
        max_words = max(2, max_words - 1)
    report.update({'clip_id': a.clip_id, 'final': final, 'hook': a.hook, 'facecam': cam, 'words': len(words)})
    json.dump(report, open(os.path.join(a.out, f'{a.clip_id}.report.json'), 'w'), indent=2)
    if not report['pass']:
        os.replace(final, os.path.join(work, 'rejected.mp4'))
        log('REJECTED after auto-revisions: never sent to Approvals'); sys.exit(1)
    log('READY FOR APPROVALS:', final)


if __name__ == '__main__':
    main()
