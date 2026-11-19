"""Layout law for every Clip Farm edit (1080x1920, 9:16). Single source of truth.

Zones (pixels, top-left origin):
  0    - 210   platform top UI (status bar / search)  -> nothing of ours
  220  - 380   TOP HOOK caption (<= 7 words, <= 2 lines)
  400  - 1320  CENTER CONTENT: facecam + GTA 6 gameplay only, ZERO text/graphics
  1350 - 1520  BOTTOM spoken captions (2-3 word chunks, word synced)
  1540 - 1920  platform bottom UI (description / buttons) -> nothing of ours
  Horizontal text safe area: x 90 - 990 on every text line.
"""

W, H = 1080, 1920
FPS = 30

TOP_UI = (0, 210)
HOOK = (220, 380)
CENTER = (400, 1320)
CAPS = (1350, 1520)
BOTTOM_UI = (1540, 1920)
SAFE_X = (90, 990)

HOOK_MAX_WORDS = 7
HOOK_MAX_LINES = 2
CAP_MAX_WORDS = 3          # words per on-screen chunk
CAP_MAX_LINES = 2

# Starting font sizes; the auto-revision loop shrinks them until the verifier passes.
HOOK_FONT_PX = 68
CAP_FONT_PX = 78
MIN_FONT_PX = 40

import os
FONT_BOLD = os.environ.get("CLIPFARM_FONT", "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf")

# Max A/V drift allowed between the video and audio streams (seconds).
MAX_AV_DRIFT_S = 0.045


def zone_ok(y0, y1, zone):
    return zone[0] <= y0 and y1 <= zone[1]
