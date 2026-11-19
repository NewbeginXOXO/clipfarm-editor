---
name: gta6-precision-editor
description: Render and self-verify a GTA 6 vertical clip (1080x1920) with a top hook and word-synced bottom captions in fixed safe zones, using only local FFmpeg, faster-whisper and Python. Use for any Clip Farm edit, re-render or layout fix before it can enter Approvals.
---

# GTA 6 Precision Editor

Follow `CLAUDE.md` at the project root. The layout law is in `editor/layout.py`.

## When to use
- A Honeycutt-cleared GTA 6 clip needs its finished edit.
- An edit was flagged for text overlap, cut-off captions or text over gameplay/faces.
- Re-rendering an older OpusClip edit under the layout law.

## Steps
1. **Confirm the clip is eligible:** GTA 6 footage, Honeycutt Cleared, 12 to 75 s, no isolated cutscene or ending, no audible radio or licensed music. If not, stop and report. Do not render.
2. **Hook:** max 7 words, accurate, no spoilers. If the queue hook is longer, shorten it while keeping its meaning (e.g. "Can you do barrel rolls in GTA 6?" (8 words) -> "Can you barrel roll in GTA 6?" (7 words)).
3. **Get the source window:** the licensed VOD from the campaign source, `start_s - 1.5` to `end_s`.
4. **Render:**
   ```
   python editor/render_edit.py --source vod.mp4 --start <s> --end <e> \
     --hook "<7-word hook>" --clip-id <clip_id> --out out/ [--facecam x,y,w,h]
   ```
   Whisper (`small.en`, CPU int8) transcribes with word timestamps. The facecam is auto-detected; pass `--facecam` when the streamer's box is known (see `training_examples/layouts/`).
5. **Verify:** `render_edit.py` runs `verify_edit.py` itself and auto-revises up to 4 times. Exit 0 with `READY FOR APPROVALS` is the only success. Exit 1 or 2 means rejected: report the issues and never deliver the file.
6. **Deliver:** upload the MP4 and `<clip_id>.report.json`, then call the Callbacks webhook with `editor=farm` so the clip enters Approvals and the email fires.
7. **Learn:** copy the report into `training_examples/approved/` or `training_examples/rejected/` once the user decides. Note what changed if they asked for a revision.

## Never
- Put any text inside Y 400 to 1320, or outside X 90 to 990.
- Ship an edit whose verifier report is not `"pass": true`.
- Use paid APIs, strip watermarks, or post live. DRY_RUN stays true until the user types "POST".
