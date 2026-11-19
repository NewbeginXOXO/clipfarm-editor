# training_examples

Reference material the precision editor and its agents learn from. No AI training runs on it automatically. It's a reviewed record that the Lead and the editor read when picking styles, hooks and layouts.

- `approved/`: one folder per approved edit: `<clip_id>.report.json` (the verifier report), `plan.json`, and a 3-frame contact sheet. Optionally `notes.md` with why it worked (views after posting).
- `rejected/`: edits the verifier or the user rejected, with the report and the reason. Your revision notes go here too (for example "TEXT IS BLOCKING TEXT" on XRo2L3077NI-338).
- `layouts/`: one JSON per streamer with the facecam box in source pixels, so renders skip auto-detection:
  `{"channel": "Tomo", "source_size": [1920, 1080], "facecam": [x, y, w, h], "notes": "..."}`

Keep files small: no MP4s in this folder.
