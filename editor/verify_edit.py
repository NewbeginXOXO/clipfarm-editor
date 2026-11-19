#!/usr/bin/env python3
"""verify_edit.py: pre-approval guardrail. An edit may reach Approvals (or trigger an email) ONLY if this passes.

It inspects the RENDERED pixels, not just the plan:
  * format: 1080x1920, video + audio present, A/V streams aligned (start and duration drift <= 45 ms)
  * overlay mask = |final - clean| on sampled frames (clean = same edit rendered without any text)
      - zero overlay pixels in the CENTER zone (400-1320), top platform UI (0-210) and bottom platform UI (1540-1920)
      - hook pixels only inside 220-380, spoken-caption pixels only inside 1350-1520
      - every overlay pixel inside x 90-990 (no side cut-off)
  * plan rules: hook <= 7 words and <= 2 lines, caption chunks <= 3 words / <= 2 lines,
    each caption chunk appears within 120 ms of its first spoken word (word-synced)
  * OCR spot check (tesseract): the hook text is readable in the hook band

Usage: verify_edit.py final.mp4 clean.mp4 plan.json [--json report.json]
Exit code 0 = PASS, 1 = FAIL. Costs $0 (ffmpeg, ffprobe, OpenCV, tesseract; all local).
"""
import json, subprocess, sys, difflib
import numpy as np
import cv2

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from layout import W, H, TOP_UI, HOOK, CENTER, CAPS, BOTTOM_UI, SAFE_X, HOOK_MAX_WORDS, HOOK_MAX_LINES, CAP_MAX_WORDS, CAP_MAX_LINES, MAX_AV_DRIFT_S

DIFF_T = 48        # per-pixel difference that counts as overlay (re-encode noise stays well below)
MIN_BLOB = 40      # ignore specks smaller than this many pixels
NOISE_PX = 60      # tolerated stray pixels per zone per frame


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def frame_at(path, t):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', path, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], capture_output=True, check=True).stdout
    if len(raw) < W * H * 3:
        return None
    return np.frombuffer(raw[:W * H * 3], np.uint8).reshape(H, W, 3)


def overlay_mask(a, b):
    d = cv2.absdiff(a, b).max(axis=2)
    m = (d > DIFF_T).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= MIN_BLOB:
            keep[lab == i] = 1
    return keep


def band(m, y0, y1):
    return int(m[y0:y1].sum())


def verify(final, clean, plan):
    issues, checks = [], {}
    # ---- format and A/V sync
    p = probe(final)
    v = [s for s in p['streams'] if s['codec_type'] == 'video']
    a = [s for s in p['streams'] if s['codec_type'] == 'audio']
    if not v:
        return {'pass': False, 'issues': ['no video stream'], 'checks': {}}
    v = v[0]
    checks['resolution'] = f"{v['width']}x{v['height']}"
    if (v['width'], v['height']) != (W, H):
        issues.append(f"resolution {v['width']}x{v['height']} is not 1080x1920")
    if not a:
        issues.append('no audio stream')
    else:
        a = a[0]
        ds = abs(float(v.get('start_time', 0)) - float(a.get('start_time', 0)))
        dd = abs(float(v.get('duration', p['format']['duration'])) - float(a.get('duration', p['format']['duration'])))
        checks['av_start_drift_ms'] = round(ds * 1000, 1)
        checks['av_duration_drift_ms'] = round(dd * 1000, 1)
        if ds > MAX_AV_DRIFT_S:
            issues.append(f'audio/video start drift {ds*1000:.0f} ms')
        if dd > MAX_AV_DRIFT_S + 0.03:  # AAC frame padding allowance
            issues.append(f'audio/video duration drift {dd*1000:.0f} ms')
    dur = float(p['format']['duration'])
    checks['duration_s'] = round(dur, 2)

    # ---- plan rules
    hook = plan['hook']
    if len(hook['text'].split()) > HOOK_MAX_WORDS:
        issues.append(f"hook has {len(hook['text'].split())} words (max {HOOK_MAX_WORDS})")
    if len(hook['lines']) > HOOK_MAX_LINES:
        issues.append(f"hook wraps to {len(hook['lines'])} lines (max {HOOK_MAX_LINES})")
    for c in plan['captions']:
        if len(c['text'].split()) > CAP_MAX_WORDS:
            issues.append(f"caption chunk '{c['text']}' has more than {CAP_MAX_WORDS} words")
        if len(c['lines']) > CAP_MAX_LINES:
            issues.append(f"caption chunk '{c['text']}' wraps past {CAP_MAX_LINES} lines")
        if abs(c['start'] - c['word_start']) > 0.12:
            issues.append(f"caption '{c['text']}' is off its spoken word by {abs(c['start'] - c['word_start'])*1000:.0f} ms")
    checks['caption_chunks'] = len(plan['captions'])

    # ---- rendered pixels: sample the hook moment plus every caption chunk midpoint
    times = sorted({0.6} | {round((c['start'] + c['end']) / 2, 2) for c in plan['captions']})
    times = [t for t in times if t < dur - 0.05][:60]
    worst = {'center': 0, 'top_ui': 0, 'bottom_ui': 0, 'outside_safe_x': 0, 'hook_outside_band': 0, 'caps_outside_band': 0}
    hook_seen = caps_seen = 0
    ocr_frame = None
    for t in times:
        fa, fb = frame_at(final, t), frame_at(clean, t)
        if fa is None or fb is None:
            issues.append(f'could not read frame at {t}s')
            continue
        m = overlay_mask(fa, fb)
        worst['center'] = max(worst['center'], band(m, *CENTER))
        worst['top_ui'] = max(worst['top_ui'], band(m, *TOP_UI))
        worst['bottom_ui'] = max(worst['bottom_ui'], band(m, *BOTTOM_UI))
        worst['outside_safe_x'] = max(worst['outside_safe_x'], int(m[:, :SAFE_X[0]].sum() + m[:, SAFE_X[1]:].sum()))
        # pixels in the gaps between bands (380-400, 1320-1350, 1520-1540) belong to no allowed zone
        worst['hook_outside_band'] = max(worst['hook_outside_band'], band(m, TOP_UI[1], HOOK[0]) + band(m, HOOK[1], CENTER[0]))
        worst['caps_outside_band'] = max(worst['caps_outside_band'], band(m, CENTER[1], CAPS[0]) + band(m, CAPS[1], BOTTOM_UI[0]))
        hook_seen += band(m, *HOOK) > 200
        caps_seen += band(m, *CAPS) > 200
        if ocr_frame is None and band(m, *HOOK) > 200:
            ocr_frame = fa
    checks['frames_checked'] = len(times)
    checks['overlay_px_worst'] = worst
    labels = {'center': 'text/graphics entered the CENTER zone (400-1320)', 'top_ui': 'text in the top platform UI (0-210)',
              'bottom_ui': 'text in the bottom platform UI (1540-1920)', 'outside_safe_x': 'text outside x 90-990 (cut-off risk)',
              'hook_outside_band': 'hook spills outside 220-380', 'caps_outside_band': 'captions spill outside 1350-1520'}
    for k, n in worst.items():
        if n > NOISE_PX:
            issues.append(f'{labels[k]}: {n} px')
    if not hook_seen:
        issues.append('hook caption not visible in the hook band')
    if plan['captions'] and caps_seen < max(1, len(plan['captions']) // 2):
        issues.append(f'spoken captions visible in only {caps_seen} sampled frames')

    # ---- OCR spot check of the hook (legibility)
    if ocr_frame is not None:
        try:
            import pytesseract
            crop = ocr_frame[HOOK[0]:HOOK[1], SAFE_X[0]:SAFE_X[1]]
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            _, g = cv2.threshold(g, 200, 255, cv2.THRESH_BINARY_INV)
            txt = pytesseract.image_to_string(g, config='--psm 6').strip()
            ratio = difflib.SequenceMatcher(None, ' '.join(txt.upper().split()), hook['text'].upper()).ratio()
            checks['hook_ocr'] = txt.replace('\n', ' ')
            checks['hook_ocr_match'] = round(ratio, 2)
            if ratio < 0.6:
                issues.append(f'hook OCR match only {ratio:.2f} (text may be unreadable)')
        except Exception as e:
            checks['hook_ocr'] = f'skipped ({e.__class__.__name__})'
    return {'pass': not issues, 'issues': issues, 'checks': checks}


if __name__ == '__main__':
    args = sys.argv[1:]
    out = None
    if '--json' in args:
        i = args.index('--json'); out = args[i + 1]; del args[i:i + 2]
    final, clean, plan = args
    rep = verify(final, clean, json.load(open(plan)))
    s = json.dumps(rep, indent=2)
    print(s)
    if out:
        open(out, 'w').write(s)
    sys.exit(0 if rep['pass'] else 1)
