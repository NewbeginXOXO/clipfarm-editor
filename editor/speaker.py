"""speaker.py: keep spoken captions for the STREAMER only (v148, Newbegin, Nov 22 2026). $0, no AI: numpy + OpenCV + ffmpeg.

Rule: talking captions show what the streamer says, never game characters (NPC lines, cutscene dialogue, phone calls, radio).

How a phrase (words with no long gap between them) is judged:
  1. Facecam on screen: the streamer's mouth region must be moving while the phrase is spoken (lip-motion z-score).
     Phrases the mouth clearly says also teach a voice fingerprint (log-mel profile), so a line said while the head is turned
     still counts if it sounds like the streamer and not like the phrases spoken while the mouth was still.
  2. No facecam: the phrases are split into two voices by their log-mel fingerprint. The streamer is the voice that talks the
     most and loudest (the mic sits right in front of them; game voices are mixed under the gameplay). If the phrases do not
     separate into two voices, they are all one voice and are kept.
Unsure phrases are dropped: a missing caption is better than putting an NPC's words under the streamer.
"""
import subprocess
import numpy as np

SR = 16000


def _audio(path):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', str(SR), '-f', 's16le', '-'], capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def _mel_fb(n_fft=512, n_mels=40):
    f = np.linspace(0, SR / 2, n_fft // 2 + 1)
    mel = lambda hz: 2595 * np.log10(1 + hz / 700.0)
    imel = lambda m: 700 * (10 ** (m / 2595.0) - 1)
    pts = imel(np.linspace(mel(80), mel(7600), n_mels + 2))
    fb = np.zeros((n_mels, len(f)))
    for i in range(n_mels):
        l, c, r = pts[i], pts[i + 1], pts[i + 2]
        fb[i] = np.clip(np.minimum((f - l) / (c - l), (r - f) / (r - c)), 0, None)
    return fb


def _embed(a, s, e, fb):
    x = a[int(s * SR):int(e * SR)]
    if len(x) < SR * 0.15:
        return None, 0.0
    hop, n = 160, 512
    fr = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    lm = np.log(spec @ fb.T + 1e-8)
    lm = lm - lm.mean(axis=1, keepdims=True)  # loudness-independent spectral shape (timbre)
    return np.concatenate([lm.mean(0), lm.std(0)]), float(np.sqrt((x ** 2).mean()))


def _mouth_motion(path, cam, fps=15):
    x, y, w, h = cam
    my, mh = y + int(h * 0.45), int(h * 0.5)  # lower half of the webcam box: mouth and jaw
    mx, mw = x + int(w * 0.2), int(w * 0.6)
    W = 96
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf', f'crop={mw}:{mh}:{mx}:{my},scale={W}:-2,format=gray', '-r', str(fps), '-f', 'rawvideo', '-'], capture_output=True).stdout
    H = int(round(mh * W / mw / 2)) * 2
    if not raw or H <= 0:
        return None
    fr = np.frombuffer(raw, np.uint8)[: (len(raw) // (W * H)) * W * H].reshape(-1, H, W).astype(np.float32)
    if len(fr) < 3:
        return None
    mot = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    mot = np.concatenate([[mot[0]], mot])
    k = np.ones(3) / 3
    mot = np.convolve(mot, k, mode='same')
    return (mot - mot.mean()) / (mot.std() + 1e-6), fps


def phrases_of(words, gap=0.55):
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1]['s'] - w['e'] if i + 1 < len(words) else 9
        if nxt > gap or w['w'].endswith(('.', '!', '?')):
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return out


def _cos(a, b):
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def streamer_only(media, words, cam=None):
    """Returns (kept_words, info). info = {'method', 'phrases', 'kept', 'dropped', 'dropped_text'}."""
    if not words:
        return words, {'method': 'no speech', 'phrases': 0, 'kept': 0, 'dropped': 0}
    a, fb = _audio(media), _mel_fb()
    ph = phrases_of(words)
    feats = []
    for p in ph:
        s, e = words[p[0]]['s'], words[p[-1]]['e']
        emb, rms = _embed(a, s, max(e, s + 0.2), fb)
        feats.append({'idx': p, 's': s, 'e': e, 'emb': emb, 'rms': rms, 'dur': max(0.05, e - s)})
    keep = [False] * len(ph)
    method = ''
    mm = _mouth_motion(media, cam) if cam else None
    if mm is not None:
        z, fps = mm
        for f in feats:
            seg = z[int(f['s'] * fps):max(int(f['s'] * fps) + 1, int(f['e'] * fps) + 1)]
            f['lip'] = float(seg.mean()) if len(seg) else -9
        talk = [f for f in feats if f['lip'] >= 0.5 and f['emb'] is not None]
        still = [f for f in feats if f['lip'] <= -0.1 and f['emb'] is not None]
        rs = np.mean([f['emb'] for f in talk], axis=0) if talk else None
        rg = np.mean([f['emb'] for f in still], axis=0) if still else None
        for i, f in enumerate(feats):
            if f['lip'] >= 0.0:
                keep[i] = True
            elif rs is not None and f['emb'] is not None and f['lip'] > -0.4:
                keep[i] = _cos(f['emb'], rs) - (_cos(f['emb'], rg) if rg is not None else 0.9) > 0.03
        method = f'facecam lip motion + voice match ({len(talk)} clear streamer phrases)'
    else:
        ok = [i for i, f in enumerate(feats) if f['emb'] is not None]
        if len(ok) < 2:
            keep = [f['emb'] is not None for f in feats]
            method = 'one phrase, kept'
        else:
            X = np.array([feats[i]['emb'] for i in ok]); X = (X - X.mean(0)) / (X.std(0) + 1e-6)
            c = X[[int(np.argmax(np.linalg.norm(X - X.mean(0), axis=1)))]][0]
            c2 = X[int(np.argmax(np.linalg.norm(X - c, axis=1)))]
            C = np.array([c, c2])
            for _ in range(20):
                lab = np.argmin(((X[:, None, :] - C[None]) ** 2).sum(-1), axis=1)
                C = np.array([X[lab == k].mean(0) if (lab == k).any() else C[k] for k in (0, 1)])
            sep = float(np.linalg.norm(C[0] - C[1]) / np.sqrt(X.shape[1]))
            if sep < 0.9 or min((lab == 0).sum(), (lab == 1).sum()) == 0:
                keep = [f['emb'] is not None for f in feats]
                method = f'voice clustering: one voice (separation {sep:.2f})'
            else:
                score = []
                for k in (0, 1):
                    m = [feats[ok[j]] for j in range(len(ok)) if lab[j] == k]
                    score.append(sum(f['dur'] for f in m) * (np.mean([f['rms'] for f in m]) + 1e-6))
                sk = int(np.argmax(score))
                for j, i in enumerate(ok):
                    keep[i] = lab[j] == sk
                method = f'voice clustering: 2 voices (separation {sep:.2f}), kept the main mic voice'
    keep = [bool(k) for k in keep]
    kept = [words[i] for p, k in zip(ph, keep) if k for i in p]
    dropped = [' '.join(words[i]['w'] for i in p) for p, k in zip(ph, keep) if not k]
    keep = [bool(k) for k in keep]
    return kept, {'method': method, 'phrases': len(ph), 'kept': int(sum(keep)), 'dropped': len(dropped), 'dropped_text': ' | '.join(dropped)[:300]}
