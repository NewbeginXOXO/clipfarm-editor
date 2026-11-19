# Connect the precision editor (GitHub Actions, $0)

Live posting is not touched by any of this. DRY_RUN stays true and Upload-Post stays disabled until you type "POST".

## 1. Create the repo (about 3 min)
1. Sign in at github.com (a free account is fine).
2. Click **New repository**. Name: `clipfarm-editor`. Visibility: **Public** (Actions minutes are unlimited and free on public repos, and the rendered MP4 links play in your Approvals tab). Tick **Add a README**. Click **Create repository**.
3. Unzip `clipfarm-editor.zip` on your computer.
4. In the repo, click **Add file > Upload files**. Drag **everything inside** the unzipped `clipfarm-editor` folder into the box, including the `.github` and `.claude` folders. Then click **Commit changes**.
   - If Windows hides the dot-folders, turn on **View > Show > Hidden items** in File Explorer first.
   - Check: the repo must show `.github/workflows/render.yml`. If it's missing, use **Add file > Create new file**, type `.github/workflows/render.yml` as the name, paste the file's contents and commit.
5. Open the repo's **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.

## 2. Create the token (n8n uses it only to start renders)
1. Go to GitHub **Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**.
2. Name: `clipfarm-n8n`. Expiration: 90 days. Repository access: **Only select repositories > clipfarm-editor**.
3. Under Permissions > Repository permissions, set **Contents: Read and write** (needed to trigger a render). Metadata: Read-only is added automatically. Leave everything else as No access.
4. Click **Generate token** and copy it once (it starts with `github_pat_`).

**Where the token goes:** only into the n8n credential in step 3. Never paste it into the dashboard, the Google Sheet, a chat, an email or a file in the repo.

## 3. Put the token in n8n (encrypted credential store)
1. In n8n (newbegin.app.n8n.cloud), go to **Overview > Credentials > Create credential > Header Auth**.
2. Credential name: `GitHub Render Token`.
3. Name: `Authorization`. Value: `Bearer github_pat_...` (the word Bearer, one space, then the token, with no line break).
4. Click **Save**.

## 4. Tell Claude "GitHub connected: <your-username>/clipfarm-editor"
Claude then:
- points **Clip Farm Query > Auto-Edit API (Clip Farm Render)** at `https://api.github.com/repos/<you>/clipfarm-editor/dispatches` and attaches the `GitHub Render Token` credential;
- sets `PRECISION_READY = true` in **AI AGENTS CLIP FARM GTA 6 > GTA 6 Only: Lead Filter**;
- the Lead picks up the queued TOMO barrel-roll clip (XRo2L3077NI-338) on its next run, or Claude triggers the run right away.

Then: GitHub renders the clip and runs `verify_edit.py`, auto-revising if needed (5 to 10 min). Only after a PASS does Callbacks put it in **Approvals** and email speedologyclipz@gmail.com.

## If a render fails
- The clip returns to Clip Found with the reason in its note, and the run log is in the repo's Actions tab.
- **"YouTube may be asking GitHub to sign in":** YouTube sometimes blocks cloud runners. Fix: export YouTube cookies (Netscape format) from a browser where you're signed in, then add them as the repo secret **Settings > Secrets and variables > Actions > New repository secret**, named `YT_COOKIES`. Secrets are encrypted and never shown in logs.

## Manual test (optional)
**Actions > render > Run workflow**, then fill in clip_id, source_url, start, end, title and the callback_url Claude gives you.
