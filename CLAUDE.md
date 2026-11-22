# CLAUDE.md: GTA 6 Automated Clipping & Video Editing Rulebook

Permanent project rules for the Clip Farm (SpeedologyClipz + Clip It You're It). Every agent and every edit follows them. No exceptions.

## 1. Layout law (1080x1920, 9:16), enforced on 100% of edits

| Zone | Y range (px) | Allowed |
|---|---|---|
| Platform top UI | 0 to 210 | Nothing of ours |
| TOP HOOK caption | 220 to 380 | Hook only: max 7 words, max 2 lines, centered on X 540 |
| CENTER content | 400 to 1320 | Facecam + GTA 6 gameplay only. ZERO text, captions or graphics |
| BOTTOM spoken captions | 1350 to 1520 | Word-synced captions of what the STREAMER actually says, 2 to 3 word chunks. Never game characters (NPCs, cutscenes, phone calls, radio): `editor/speaker.py` keeps the streamer's phrases only |
| Platform bottom UI | 1540 to 1920 | Nothing of ours |

- Horizontal text safe area: X 90 to 990 on every line (no side cut-off).
- Text never overlaps other text, faces, gameplay, HUD or burned-in stream alerts.
- Facecams are cropped whole (eyes and mouth inside the box) and placed in the center zone, never over the game HUD. Gameplay keeps its full 16:9 frame so the HUD and minimap stay visible.
- Geometry lives in `editor/layout.py`. Change it there only.

## 2. Pre-approval guardrail (single human gate)

- An edit reaches the Approvals tab, or triggers an email, ONLY after `editor/verify_edit.py` passes on the rendered MP4.
- The verifier inspects rendered frames (overlay mask = final minus a text-free render), checks every zone, the X 90 to 990 safe area, hook and caption rules, caption-to-word sync (120 ms) and A/V sync (45 ms), and OCRs the hook for legibility.
- On failure, `editor/render_edit.py` auto-revises (smaller type, rewrap, shorter chunks) and re-renders, up to 4 revisions. Still failing = rejected and never shown.
- The user's only step: review finished MP4s in Approvals.

## 3. Content rules

- GTA 6 only, on both accounts. Reject other games (Zelda, GTA 5/Online, etc.), isolated story cutscenes, mission endings, plot spoilers, and stretches with audible in-game radio or licensed music.
- Prefer viral spikes: crashes, chases, police chaos, stunts, fails, glitches, big reactions. Start 1 to 2 s before the spike.
- Hooks: max 7 words, accurate, no spoilers, never claims to be official.
- SpeedologyClipz: IShowSpeed + verified GTA 6 campaigns only. Clip It You're It: active, verified GTA 6 creator/launch campaigns.
- Block vape, cannabis, betting, crypto and adult brands. Never strip watermarks. Never fabricate rights or trust name-alike channels.

## 4. Cost and safety

- $0 stack: FFmpeg, ffprobe, faster-whisper (local CPU), Python (Pillow, OpenCV, pytesseract). No paid APIs, no paid storage, no LLM calls inside polling or status loops.
- OpusClip auto-layouts ignore these coordinates, so they are not trusted for final edits.
- `DRY_RUN = true` and the Upload-Post node stay disabled until the user explicitly types "POST".
- Live posting also requires `confirmLive: 'POST'`. Never flip either without that word from the user.

## 5. Agent chain

Scout/Recorder (find spikes) -> Honeycutt (legal clearance) -> Lead (pick + style) -> Precision Editor (`render_edit.py` + `verify_edit.py`) -> Callbacks (Approvals + email, GTA 6 only) -> user approves -> dry-run publish until "POST".
