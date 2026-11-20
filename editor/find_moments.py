#!/usr/bin/env python3
"""find_moments.py: find viral spike moments in finished GTA 6 streams. $0, no AI.

Signals (both free, read with yt-dlp + the YT_COOKIES secret):
  * live-chat replay: messages per 10 s; a spike is a burst far above the stream's normal chat speed,
    boosted by hype words (LMAO, OMG, W, ?????, clip it, ...). Precise to ~10 s.
  * YouTube "Most replayed" heatmap (when YouTube has built it): normalized 0-1 per segment.
Fallback when neither exists: evenly spaced 30-min windows with a low score, so a stream is never lost.

Output per video: up to TOP moments, each {start_s, end_s, peak_s, score 0-100, signal, chat_rate, heat, sample}
start = peak - 45 s, end = peak + 45 s (one short Gemini request per moment), at least 120 s apart.
"""
import json, math, os, re, subprocess, sys, statistics, glob

TOP = 10
PRE, POST = 45, 45
MIN_GAP = 120
BIN = 10
HYPE = re.compile(r'lmf?ao+|lol+|omg+|w+\b|\?{3,}|!{3,}|no way|clip (it|that)|bro+|holy|insane|crazy|💀|😂|🤣|😭|🔥|kekw|omegalul|pog', re.I)


def ytdlp(args, timeout):
    cmd = ['yt-dlp', '--no-progress', '--remote-components', 'ejs:github'] + (['--cookies', 'cookies.txt'] if os.path.exists('cookies.txt') else []) + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def chat_bins(path):
    bins, text = {}, {}
    with open(path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            r = d.get('replayChatItemAction') or {}
            t = r.get('videoOffsetTimeMsec')
            if t is None:
                continue
            b = int(int(t) / 1000 // BIN)
            for a in r.get('actions', []):
                item = ((a.get('addChatItemAction') or {}).get('item') or {})
                m = item.get('liveChatTextMessageRenderer') or item.get('liveChatPaidMessageRenderer')
                if not m:
                    continue
                msg = ''.join(x.get('text', '') or (x.get('emoji', {}).get('shortcuts') or [''])[0] for x in (m.get('message') or {}).get('runs', []))
                bins[b] = bins.get(b, 0) + 1 + (1.5 if HYPE.search(msg) else 0)
                if HYPE.search(msg) and len(text.setdefault(b, [])) < 4:
                    text[b].append(msg[:60])
    return bins, text


def peaks_from_chat(bins, text, dur):
    if len(bins) < 30:
        return []
    n = int(dur // BIN) + 1
    series = [bins.get(i, 0.0) for i in range(n)]
    # 30 s rolling sum vs the stream's own median: robust to chat that is busy all stream
    roll = [sum(series[max(0, i - 1):i + 2]) for i in range(n)]
    med = statistics.median([x for x in roll if x > 0] or [1])
    mad = statistics.median([abs(x - med) for x in roll if x > 0] or [1]) or 1
    z = [(x - med) / (1.4826 * mad) for x in roll]
    out = []
    for i in sorted(range(n), key=lambda k: -z[k]):
        if z[i] < 3 or len(out) >= TOP * 2:
            break
        p = i * BIN + BIN // 2
        if any(abs(p - o['peak_s']) < MIN_GAP for o in out):
            continue
        out.append({'peak_s': p, 'z': round(z[i], 2), 'chat_rate': round(roll[i] / 3 / BIN * 60, 1), 'sample': ' | '.join(text.get(i, [])[:3])})
    return out


def peaks_from_heat(heat):
    out = []
    for h in sorted(heat or [], key=lambda x: -float(x.get('value') or 0)):
        p = int((float(h['start_time']) + float(h['end_time'])) / 2)
        if float(h.get('value') or 0) < 0.35 or any(abs(p - o['peak_s']) < MIN_GAP for o in out):
            continue
        out.append({'peak_s': p, 'heat': round(float(h['value']), 3)})
        if len(out) >= TOP * 2:
            break
    return out


def score_moments(chat, heat, dur):
    m = []
    for c in chat:  # chat z 3..15 -> 55..95
        s = 55 + min(40, (c['z'] - 3) * 3.4)
        h = next((x['heat'] for x in heat if abs(x['peak_s'] - c['peak_s']) < 150), None)
        if h is not None:
            s = min(100, s + 10 * h)
        m.append({**c, 'heat': h, 'score': round(s), 'signal': 'chat+heatmap' if h is not None else 'chat'})
    for h in heat:
        if any(abs(h['peak_s'] - x['peak_s']) < MIN_GAP for x in m):
            continue
        m.append({**h, 'score': round(40 + 45 * h['heat']), 'signal': 'heatmap', 'chat_rate': None, 'sample': ''})
    if not m:  # no signal yet: even 30-min windows, low priority, so the stream is still covered
        m = [{'peak_s': s + 900, 'score': 20, 'signal': 'even', 'even': True} for s in range(0, int(min(dur, 12 * 3600)), 1800)]
    m.sort(key=lambda x: -x['score'])
    res = []
    for x in m[:TOP]:
        if x.get('even'):
            st, en = x['peak_s'] - 900, min(dur, x['peak_s'] + 900)
        else:
            st, en = max(0, x['peak_s'] - PRE), min(dur, x['peak_s'] + POST)
        res.append({'start_s': int(st), 'end_s': int(en), 'peak_s': int(x['peak_s']), 'score': x['score'], 'signal': x['signal'],
                    'chat_rate': x.get('chat_rate'), 'heat': x.get('heat'), 'sample': (x.get('sample') or '')[:180]})
    return res


def scan(video_id, workdir):
    url = 'https://www.youtube.com/watch?v=' + video_id
    info = ytdlp(['--skip-download', '--dump-single-json', url], 300)
    if info.returncode != 0:
        err = ([l for l in info.stderr.splitlines() if 'ERROR' in l] or ['unknown'])[-1]
        return {'video_id': video_id, 'ok': False, 'error': err.replace('ERROR: ', '')[:300]}
    meta = json.loads(info.stdout)
    dur = float(meta.get('duration') or 0)
    heat = peaks_from_heat(meta.get('heatmap'))
    chat = []
    if meta.get('was_live') or meta.get('live_status') == 'was_live':
        out = os.path.join(workdir, video_id)
        ytdlp(['--skip-download', '--write-subs', '--sub-langs', 'live_chat', '-o', out + '.%(ext)s', url], 900)
        files = glob.glob(out + '*.live_chat.json')
        if files:
            bins, text = chat_bins(files[0])
            chat = peaks_from_chat(bins, text, dur)
    return {'video_id': video_id, 'ok': True, 'title': meta.get('title', ''), 'duration_s': int(dur), 'views': meta.get('view_count'),
            'had_heatmap': bool(meta.get('heatmap')), 'chat_peaks': len(chat), 'moments': score_moments(chat, heat, dur)}


if __name__ == '__main__':
    os.makedirs('moments', exist_ok=True)
    res = [scan(v, 'moments') for v in sys.argv[1:]]
    print(json.dumps(res, indent=1))
