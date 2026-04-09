"""
Interview Automation Tool
Automates creation of Google Drive folders and documents from Granola notes + YouTube livestreams
"""

import os
import json
import threading
import webbrowser
from datetime import datetime
from flask import Flask, request, jsonify, redirect, url_for, session, Response

# Google OAuth
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import requests as http_requests

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY environment variable is not set. See .env.example.")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False  # localhost only
app.config["PERMANENT_SESSION_LIFETIME"] = 86400  # 24 hours

# ─── Config ──────────────────────────────────────────────────────────────────
MAIN_FOLDER_ID = os.environ.get("MAIN_FOLDER_ID")
RECORDINGS_FOLDER_ID = os.environ.get("RECORDINGS_FOLDER_ID")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID")
GRANOLA_API_BASE = "https://api.granola.ai/v1"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/youtube.readonly",
]

ARCADE_TEAM = ["Alex", "Vivian", "Mariam", "Savannah", "Sarah"]

# ─── State ───────────────────────────────────────────────────────────────────
progress_log = []
current_job = {"running": False, "done": False, "error": None}


def log(msg, level="info"):
    entry = {"msg": msg, "level": level, "time": datetime.now().strftime("%H:%M:%S")}
    progress_log.append(entry)
    print(f"[{level.upper()}] {msg}")


# ─── Google Auth ─────────────────────────────────────────────────────────────
# Global token store: populated from request context before thread launch
_g_token_data = {}
_g_anthropic_key = {}

def get_google_creds(token_data=None):
    """Build Google creds from explicit token_data, global store, or Flask session."""
    td = token_data or _g_token_data.get("token") or None
    if td is None:
        try:
            td = session.get("google_token")
        except RuntimeError:
            return None
    if not td:
        return None
    return Credentials(
        token=td.get("token"),
        refresh_token=td.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=td.get("client_id"),
        client_secret=td.get("client_secret"),
        scopes=SCOPES,
    )


def get_drive_service():
    return build("drive", "v3", credentials=get_google_creds())


def get_docs_service():
    return build("docs", "v1", credentials=get_google_creds())


def get_sheets_service():
    return build("sheets", "v4", credentials=get_google_creds())


# ─── Granola API ─────────────────────────────────────────────────────────────
def fetch_granola_notes(api_key):
    """Fetch all notes from Granola API"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = http_requests.get(f"{GRANOLA_API_BASE}/documents", headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        # Try alternative endpoint
        resp2 = http_requests.get(f"{GRANOLA_API_BASE}/notes", headers=headers, timeout=15)
        if resp2.status_code == 200:
            return resp2.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def fetch_granola_note_detail(api_key, note_id):
    """Fetch full detail for a single Granola note including transcript"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = http_requests.get(f"{GRANOLA_API_BASE}/documents/{note_id}", headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


# ─── YouTube ─────────────────────────────────────────────────────────────────
def fetch_youtube_livestreams(api_key_or_cookies):
    """Fetch all videos from the channel using YouTube Data API (including unlisted)"""
    if not api_key_or_cookies:
        return []
    try:
        # Use OAuth credentials if available (needed for unlisted videos)
        from google.oauth2.credentials import Credentials as GCreds
        creds = get_google_creds()
        if creds:
            yt = build("youtube", "v3", credentials=creds)
        else:
            yt = build("youtube", "v3", developerKey=api_key_or_cookies)
        videos = []

        # First try: search for completed livestreams
        try:
            r1 = yt.search().list(
                channelId=YOUTUBE_CHANNEL_ID,
                part="snippet,id",
                type="video",
                eventType="completed",
                maxResults=50,
                order="date",
            ).execute()
            for item in r1.get("items", []):
                video_id = item["id"].get("videoId", "")
                snippet = item.get("snippet", {})
                if video_id:
                    videos.append({
                        "id": video_id,
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "type": "livestream",
                    })
        except Exception as e:
            log(f"Livestream search error: {e}", "warn")

        # Second: get ALL uploads from the channel via playlist
        try:
            # Get the uploads playlist ID
            channel_resp = yt.channels().list(
                id=YOUTUBE_CHANNEL_ID,
                part="contentDetails"
            ).execute()
            uploads_playlist = channel_resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

            # Page through all uploads
            existing_ids = {v["id"] for v in videos}
            next_page = None
            while True:
                pl_req = yt.playlistItems().list(
                    playlistId=uploads_playlist,
                    part="snippet",
                    maxResults=50,
                    pageToken=next_page,
                )
                pl_resp = pl_req.execute()
                for item in pl_resp.get("items", []):
                    snippet = item.get("snippet", {})
                    video_id = snippet.get("resourceId", {}).get("videoId", "")
                    if video_id and video_id not in existing_ids:
                        existing_ids.add(video_id)
                        videos.append({
                            "id": video_id,
                            "title": snippet.get("title", ""),
                            "description": snippet.get("description", ""),
                            "published_at": snippet.get("publishedAt", ""),
                            "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "type": "upload",
                        })
                next_page = pl_resp.get("nextPageToken")
                if not next_page:
                    break
        except Exception as e:
            log(f"Uploads playlist error: {e}", "warn")

        # Sort by date descending
        videos.sort(key=lambda v: v.get("published_at", ""), reverse=True)
        log(f"Found {len(videos)} total videos on channel")
        return videos
    except Exception as e:
        log(f"YouTube API error: {e}", "warn")
        return []


def get_youtube_transcript(video_id):
    """Legacy plain-text transcript (no timestamps). Kept for backward compat."""
    segments = get_youtube_transcript_timestamped(video_id)
    return " ".join(s["text"] for s in segments)


def get_youtube_transcript_timestamped(video_id):
    """Return YT auto-caption segments as [{'start': float_sec, 'text': str}, ...]

    Preserving timestamps is what lets us correlate each line with a video frame
    for speaker attribution.
    """
    try:
        import subprocess
        subprocess.run(
            ["yt-dlp", "--skip-download", "--write-auto-sub", "--sub-format", "json3",
             "--output", f"/tmp/yt_{video_id}", f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=120
        )
        import glob
        files = glob.glob(f"/tmp/yt_{video_id}*.json3")
        if not files:
            return []
        with open(files[0]) as f:
            data = json.load(f)
        segments = []
        for ev in data.get("events", []):
            start_ms = ev.get("tStartMs")
            if start_ms is None:
                continue
            text = "".join(s.get("utf8", "") for s in ev.get("segs", [])).strip()
            if not text:
                continue
            segments.append({"start": start_ms / 1000.0, "text": text})
        return segments
    except Exception as e:
        log(f"Transcript fetch failed for {video_id}: {e}", "warn")
        return []


def _format_timestamp(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


# ─── Video-assisted speaker diarization ─────────────────────────────────────
def download_youtube_video(video_id, out_dir="/tmp"):
    """Download a low-res MP4 for frame extraction. Returns the file path."""
    import subprocess
    out_template = os.path.join(out_dir, f"vid_{video_id}.%(ext)s")
    # 360p or lower — we only need faces, not pixels.
    subprocess.run(
        ["yt-dlp", "-f", "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=480]",
         "--merge-output-format", "mp4", "-o", out_template,
         f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, timeout=600, check=True,
    )
    import glob
    matches = glob.glob(os.path.join(out_dir, f"vid_{video_id}.*"))
    matches = [m for m in matches if not m.endswith(".part")]
    if not matches:
        raise RuntimeError("yt-dlp produced no video file")
    return matches[0]


def extract_video_frames(video_path, frame_interval_sec=60, out_dir=None):
    """Extract 1 frame per `frame_interval_sec` using ffmpeg.

    Returns a list of {"t": seconds_offset, "path": "/tmp/...jpg"}.
    """
    import subprocess
    import glob as _glob

    if out_dir is None:
        out_dir = f"/tmp/frames_{os.path.basename(video_path)}"
    os.makedirs(out_dir, exist_ok=True)

    # fps = 1/interval → one frame every N seconds.
    fps_filter = f"fps=1/{frame_interval_sec}"
    out_pattern = os.path.join(out_dir, "f_%04d.jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vf", fps_filter,
         "-q:v", "5", out_pattern],
        capture_output=True, text=True, timeout=600, check=True,
    )
    paths = sorted(_glob.glob(os.path.join(out_dir, "f_*.jpg")))
    # Frame i (0-indexed) represents timestamp i * interval.
    return [{"t": i * frame_interval_sec, "path": p} for i, p in enumerate(paths)]


def diarize_transcript_with_video(video_id, segments, video_title, max_frames=60):
    """Use Claude vision to produce a speaker-attributed transcript.

    Strategy:
      1. Download video (low-res) and extract up to `max_frames` still frames
         spread evenly across the interview.
      2. Send frames + the timestamped transcript to Claude with instructions
         to assign each transcript segment to a speaker based on who is
         visibly speaking (mouth movement, active speaker focus) at that time.

    Returns a plain-text transcript string with speaker prefixes like:
        [0:12] Alex: Can you walk me through ...
        [0:35] Participant (guest): Well, usually I ...
    Falls back to the raw transcript on any error.
    """
    if not segments:
        return ""

    # Enforce ENABLE_VIDEO_DIARIZATION env gate upstream; this function assumes
    # caller decided to try.
    api_key = _get_anthropic_key()
    if not api_key:
        log("  ⚠️ No Anthropic key; cannot run video diarization", "warn")
        return "\n".join(f"[{_format_timestamp(s['start'])}] {s['text']}" for s in segments)

    try:
        log(f"  🎞️  Downloading video for diarization: {video_id}")
        video_path = download_youtube_video(video_id)
    except Exception as e:
        log(f"  ⚠️ Video download failed, falling back to text-only: {e}", "warn")
        return "\n".join(f"[{_format_timestamp(s['start'])}] {s['text']}" for s in segments)

    # Pick a frame interval that keeps us under max_frames.
    duration = segments[-1]["start"] if segments else 0
    interval = max(30, int(duration // max_frames) + 1) if duration > 0 else 60

    try:
        log(f"  🖼️  Extracting 1 frame per {interval}s from {_format_timestamp(duration)} video...")
        frames = extract_video_frames(video_path, frame_interval_sec=interval)
    except Exception as e:
        log(f"  ⚠️ Frame extraction failed, falling back to text-only: {e}", "warn")
        try:
            os.remove(video_path)
        except Exception:
            pass
        return "\n".join(f"[{_format_timestamp(s['start'])}] {s['text']}" for s in segments)

    # Cap frames just in case.
    frames = frames[:max_frames]
    log(f"  🖼️  {len(frames)} frames extracted; sending to Claude for diarization")

    # Build the timestamped transcript block.
    transcript_lines = [f"[{_format_timestamp(s['start'])}] {s['text']}" for s in segments]
    transcript_block = "\n".join(transcript_lines)
    transcript_block = _clip(transcript_block, _TRANSCRIPT_CHAR_CAP)

    prompt_text = f"""You are analyzing a user-research interview to assign speaker labels to each line of transcript.

<video_title>{video_title}</video_title>

## Arcade team (interviewers)
The following people are Arcade team members and are interviewers: Alex, Vivian, Mariam, Savannah, Sarah. Anyone else is an interviewee / research participant.

## Input 1: Video frames
I am providing {len(frames)} still frames sampled one every {interval} seconds. Frame N corresponds to timestamp {interval * 0}s + N × {interval}s (i.e. frame 1 = 0:00, frame 2 = {_format_timestamp(interval)}, etc.). In each frame, identify which tile / person appears to be actively speaking (look for mouth movement, active-speaker highlight, or obvious focus). Track individuals consistently — a person in tile position X at 2:00 is likely the same person at 2:30.

## Input 2: Timestamped transcript
Each line is prefixed with its start time.
```
{transcript_block}
```

## Your task
Produce a speaker-attributed transcript. Rules:
1. For each transcript line, determine which speaker said it by looking at the nearest frame(s) in time and seeing who was speaking.
2. Use the Arcade team member's first name when you can identify them. Use "Participant" (or "Participant 2", "Participant 3" if there are multiple) for interviewees.
3. If you genuinely cannot tell who is speaking for a line, use "Unknown". Do NOT guess.
4. Merge consecutive lines from the same speaker into one paragraph, keeping the earliest timestamp.
5. Output format (plain text, no markdown, no preamble):

[mm:ss] SpeakerName: the spoken text
[mm:ss] SpeakerName: the next spoken text

Begin the output immediately with the first speaker line. Do not include any explanation before or after."""

    # Build Anthropic multimodal message.
    try:
        from anthropic import Anthropic
        import base64
        client = Anthropic(api_key=api_key)

        content = []
        for f in frames:
            try:
                with open(f["path"], "rb") as fh:
                    b64 = base64.standard_b64encode(fh.read()).decode("ascii")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                })
            except Exception as e:
                log(f"  ⚠️ Could not read frame {f['path']}: {e}", "warn")
        content.append({"type": "text", "text": prompt_text})

        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": content}],
        )
        diarized = msg.content[0].text.strip()
        log(f"  ✅ Diarization produced {len(diarized)} chars")
        return diarized
    except Exception as e:
        log(f"  ⚠️ Video diarization call failed: {e}", "warn")
        return "\n".join(transcript_lines)
    finally:
        # Best-effort cleanup of temp files
        try:
            os.remove(video_path)
        except Exception:
            pass


# ─── AI Synthesis ─────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"


def llm_synthesize(prompt, api_key, max_tokens=4000):
    """Call Claude API for synthesis tasks"""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def _get_anthropic_key():
    try:
        return (
            session.get("anthropic_api_key", "")
            or _g_anthropic_key.get("key", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
    except RuntimeError:
        return _g_anthropic_key.get("key", "") or os.environ.get("ANTHROPIC_API_KEY", "")


# Send the full transcript. Claude's context window is large enough to hold
# a multi-hour interview — truncation was the single biggest reason details
# and Q&A alignment were being dropped.
_TRANSCRIPT_CHAR_CAP = 400_000  # safety rail, not a quality knob
_SUMMARY_CHAR_CAP = 50_000


def _clip(text, cap):
    if not text:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap] + "\n\n[... truncated at safety cap ...]"


def extract_interview_data(transcript, granola_summary, video_title):
    """Parse a user research interview into strictly-aligned Q&A per participant.

    Key correctness rules (to fix the debrief-sheet misalignment):
      - Every response must be a VERBATIM or near-verbatim quote from the transcript.
      - A response is only attributed to a named person if the transcript/summary
        makes that attribution unambiguous. Otherwise use "Unattributed".
      - Questions are returned in the order they were actually asked in the session.
      - Do not invent questions that weren't asked. Do not merge distinct questions.
    """
    transcript_clean = _clip(transcript, _TRANSCRIPT_CHAR_CAP)
    summary_clean = _clip(granola_summary, _SUMMARY_CHAR_CAP)

    prompt = f"""You are analyzing a user research interview transcript. Your job is to produce a faithful, strictly-aligned Q&A structure so it can be dropped into a debrief spreadsheet. Accuracy of attribution matters MORE than coverage.

<video_title>{video_title}</video_title>

<granola_summary>
{summary_clean if summary_clean else "(not available)"}
</granola_summary>

<transcript>
{transcript_clean if transcript_clean else "(not available)"}
</transcript>

## Team context
Arcade team members who may be interviewers: Alex, Vivian, Mariam, Savannah, Sarah.
Any other speaker is an interviewee (user research participant).

## Hard rules — READ CAREFULLY
1. **Preserve question order.** Return questions in the order they were actually asked in the session. Do NOT reorder, merge, or paraphrase into higher-level themes.
2. **Only include questions that were actually asked aloud** by someone in the session. Do not include questions from the interview guide that were skipped.
3. **Attribution must be unambiguous.** Only attribute a response to a specific named person if the transcript or summary clearly indicates who said it (e.g. speaker label, "Sarah said...", explicit context). If you cannot tell who said a response, use the key `"Unattributed"` — do NOT guess.
4. **Responses must be quote-grounded.** Each response value should be a direct quote or a very close paraphrase. If you need to shorten, wrap the quoted part in quotation marks and add ellipses. Never fabricate specifics.
5. **One row = one question.** If the same question was re-asked, merge into one row and combine responses per person.
6. **Don't over-cluster.** If an interviewer asked 12 distinct questions, return 12 rows. Don't collapse to 5.
7. **If a participant didn't answer a question, leave their cell out of `responses`.** Do not emit empty strings.
8. **If you cannot extract reliable Q&A at all** (e.g. transcript too noisy, no speaker labels, summary lacks detail), return an empty `questions` array and set `extraction_confidence` to `"low"` with a brief `extraction_notes` explanation. This is BETTER than making things up.

## Output format — return ONLY this JSON, no prose, no code fences
{{
  "participants": ["Name1", "Name2", "..."],
  "arcade_members": ["Name1"],
  "interviewees": ["Name1"],
  "extraction_confidence": "high" | "medium" | "low",
  "extraction_notes": "One sentence explaining confidence (e.g., 'Transcript had clear speaker labels' or 'No speaker labels in YT transcript; most responses unattributed').",
  "questions": [
    {{
      "question": "The verbatim (or near-verbatim) question asked.",
      "asked_by": "Name of the interviewer who asked it, or 'Unknown'",
      "responses": {{
        "InterviewName": "Direct quote or tight paraphrase of their response.",
        "Unattributed": "A response that was given but cannot be attributed to a specific person."
      }}
    }}
  ],
  "interview_guide_questions": ["Q1", "Q2", "..."]
}}
"""

    try:
        from anthropic import Anthropic
        api_key = _get_anthropic_key()
        if not api_key:
            return None
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        confidence = data.get("extraction_confidence", "unknown")
        notes = data.get("extraction_notes", "")
        log(f"  🧠 Extraction confidence: {confidence}. {notes}")
        return data
    except Exception as e:
        log(f"AI synthesis error: {e}", "warn")
        return None


def synthesize_high_level_summary(granola_summary, transcript, interview_data, video_title):
    """Generate a rich, structured product takeaways document.

    Returns a single plain-text string with section headings, ready to drop into
    a Google Doc. Designed to replace both the old verbatim Granola dump and the
    old 1-paragraph strategist blurb — hence it intentionally includes the
    Granola summary near the top.
    """
    transcript_clean = _clip(transcript, _TRANSCRIPT_CHAR_CAP)
    summary_clean = _clip(granola_summary, _SUMMARY_CHAR_CAP)
    questions_json = json.dumps(interview_data.get("questions", []), indent=2) if interview_data else "N/A"

    prompt = f"""You are a senior product strategist and user researcher working with the Arcade team. You have just watched a user interview and need to produce a single takeaways document that a PM, designer, or eng lead can read in 3 minutes and walk away with clear, evidence-backed product direction.

<interview_title>{video_title}</interview_title>

<granola_summary>
{summary_clean if summary_clean else "(not available)"}
</granola_summary>

<full_transcript>
{transcript_clean if transcript_clean else "(not available)"}
</full_transcript>

<structured_qa>
{questions_json}
</structured_qa>

## Output requirements
Produce a plain-text document using the EXACT section headings and order below. Use short paragraphs and bullet lists (start bullets with "• "). No markdown bold or italics — this will be rendered in a Google Doc as plain text.

Rules:
- **Ground every claim in evidence.** When possible, include a direct quote in quotation marks, attributed to a participant (e.g., "I just gave up at that point" — Participant).
- **Be specific.** Name the feature, the flow, the word the user used. Avoid vague language like "users struggled with onboarding" — instead say what specifically broke.
- **Distinguish signal from noise.** If only one participant said something, label it "(single data point)". If multiple said the same thing, say so.
- **No invented detail.** If the transcript doesn't say it, don't claim it. If you're uncertain, say "Unclear from the transcript".
- **Write for decision-makers.** The goal is: a reader should finish this doc knowing what to change in the product.

=== SECTIONS ===

TL;DR
One tight paragraph (3-4 sentences) naming the single most important thing the team should take from this session. This is the only section that should read as pure prose.

Key Insights
5-8 bullets. Each bullet should be a concrete insight (not a restatement of a question), followed by a supporting quote or observation. Format: "• Insight statement. Evidence: quote or paraphrase."

Pain Points & Friction
What specifically frustrated users, where in the product, and why. 3-6 bullets. Include the step or feature name and the specific failure mode.

What's Working
Things users liked or praised. Be honest — if there were none, say so. 2-5 bullets with evidence.

Feature Requests & Desires
What users explicitly asked for OR strongly implied they wanted. 2-5 bullets. Label each as (explicit ask) or (implied). Include a quote when available.

Surprising or Contrarian Moments
Anything that contradicted the team's assumptions, was counterintuitive, or would change a PM's roadmap. 1-4 bullets. If nothing qualifies, write "(nothing notable)".

Direct Quotes Worth Keeping
5-10 verbatim quotes that capture the user's actual voice. Format: "Quote text" — Participant name (or Participant if unattributed). Pick quotes a designer or marketer would actually use in a readout.

Recommended Next Steps
3-6 concrete, actionable next steps for the product/design/eng team. Each should be something a PM could turn into a ticket tomorrow. Format: "• [Team] Action — rationale."

Open Questions for Follow-up Research
2-5 questions that came out of this session but weren't answered. These feed the next round of research.

Begin the document now, starting with the line "TL;DR" (no preamble, no title)."""

    try:
        from anthropic import Anthropic
        api_key = _get_anthropic_key()
        if not api_key:
            return "High-level summary could not be generated (no Anthropic API key)."
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=6000,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Summary generation failed: {e}"


# ─── Google Drive / Docs Helpers ──────────────────────────────────────────────
def create_drive_folder(name, parent_id):
    drive = get_drive_service()
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive.files().create(body=meta, fields="id").execute()
    return folder["id"]


def create_google_doc(title, content_requests, parent_folder_id):
    """Create a Google Doc with title and batch update content"""
    docs = get_docs_service()
    drive = get_drive_service()

    # Create empty doc
    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    # Move to folder
    file = drive.files().get(fileId=doc_id, fields="parents").execute()
    drive.files().update(
        fileId=doc_id,
        addParents=parent_folder_id,
        removeParents=",".join(file.get("parents", [])),
        fields="id, parents",
    ).execute()

    # Apply content
    if content_requests:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": content_requests}
        ).execute()

    return doc_id, f"https://docs.google.com/document/d/{doc_id}"


def create_google_sheet(title, headers, rows, parent_folder_id):
    """Create a Google Sheet with headers and data rows"""
    sheets = get_sheets_service()
    drive = get_drive_service()

    spreadsheet = sheets.spreadsheets().create(body={"properties": {"title": title}}).execute()
    sheet_id = spreadsheet["spreadsheetId"]

    # Move to folder
    file = drive.files().get(fileId=sheet_id, fields="parents").execute()
    drive.files().update(
        fileId=sheet_id,
        addParents=parent_folder_id,
        removeParents=",".join(file.get("parents", [])),
        fields="id, parents",
    ).execute()

    # Write data
    all_rows = [headers] + rows
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="Sheet1!A1",
        valueInputOption="RAW",
        body={"values": all_rows},
    ).execute()

    # Bold the header row
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}}},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"
            }
        }]}
    ).execute()

    return sheet_id, f"https://docs.google.com/spreadsheets/d/{sheet_id}"


def copy_file_to_folder(file_id, new_name, dest_folder_id):
    """Copy a Drive file to another folder"""
    drive = get_drive_service()
    copied = drive.files().copy(
        fileId=file_id,
        body={"name": new_name, "parents": [dest_folder_id]}
    ).execute()
    return copied["id"]


def doc_text_requests(text):
    """Simple helper to insert plain text into a doc"""
    return [{"insertText": {"location": {"index": 1}, "text": text}}]


def build_debrief_sheet(interview_data):
    """Turn extracted interview_data into (headers, rows) for the debrief sheet.

    Always puts Arcade interviewers last (after interviewees). Includes an
    "Unattributed" column if the extractor flagged unknown-speaker responses,
    so the team knows that signal exists without attributing to the wrong name.
    """
    if not interview_data or not interview_data.get("questions"):
        return (
            ["Question", "Asked By", "(Response)", "Extraction notes"],
            [[
                "(Transcript not available or extraction confidence too low — add manually)",
                "",
                "",
                (interview_data or {}).get("extraction_notes", ""),
            ]],
        )

    questions_data = interview_data.get("questions", [])
    arcade_members = interview_data.get("arcade_members", []) or []
    interviewees = interview_data.get("interviewees", []) or []
    declared_participants = interview_data.get("participants", []) or []

    # Discover any speaker names that actually appear in responses so nothing
    # gets silently dropped — this is the core fix for misalignment.
    seen_speakers = set()
    for q in questions_data:
        for name in (q.get("responses") or {}).keys():
            seen_speakers.add(name)

    ordered_speakers = []
    # Interviewees first (most important column for a debrief).
    for name in interviewees:
        if name in seen_speakers and name not in ordered_speakers:
            ordered_speakers.append(name)
    # Then arcade interviewers who actually spoke.
    for name in arcade_members:
        if name in seen_speakers and name not in ordered_speakers:
            ordered_speakers.append(name)
    # Anyone from the declared participant list we haven't placed yet.
    for name in declared_participants:
        if name in seen_speakers and name not in ordered_speakers:
            ordered_speakers.append(name)
    # Anything else observed in responses (e.g., Participant 2, Unattributed).
    for name in sorted(seen_speakers):
        if name not in ordered_speakers:
            ordered_speakers.append(name)

    headers = ["#", "Question", "Asked By"] + ordered_speakers
    rows = []
    for i, q in enumerate(questions_data, 1):
        responses = q.get("responses") or {}
        row = [str(i), q.get("question", ""), q.get("asked_by", "")]
        for name in ordered_speakers:
            row.append(responses.get(name, ""))
        rows.append(row)
    return headers, rows


def build_combined_takeaways_text(date_str, granola_summary, hl_summary):
    """Build the text body for the merged Interview Summary & Takeaways doc.

    Starts with a short title line, then the Granola AI summary (raw, as the
    team trusts this), then the structured synthesized takeaways.
    """
    parts = [f"({date_str}) Interview Summary & Takeaways", ""]
    if granola_summary:
        parts.append("── Granola AI Summary ──")
        parts.append("")
        parts.append(granola_summary.strip())
        parts.append("")
    parts.append("── Product Takeaways ──")
    parts.append("")
    parts.append((hl_summary or "(takeaways not generated)").strip())
    parts.append("")
    return "\n".join(parts)


# ─── Main Workflow ────────────────────────────────────────────────────────────
def run_workflow(config):
    """Main workflow: match YT streams to Granola notes, create folders + docs"""
    global progress_log, current_job
    progress_log = []
    current_job = {"running": True, "done": False, "error": None, "results": []}

    try:
        granola_key = config["granola_api_key"]
        yt_api_key = config.get("youtube_api_key", "")
        manual_streams = config.get("manual_streams", [])  # fallback if no YT API

        log("🚀 Starting Interview Automation Workflow...")

        # 1. Fetch Granola notes
        log("📓 Fetching Granola notes...")
        granola_data = fetch_granola_notes(granola_key)
        if "error" in granola_data:
            log(f"⚠️ Granola API issue: {granola_data['error']}. Will use manual matching.", "warn")
            granola_notes = []
        else:
            granola_notes = granola_data if isinstance(granola_data, list) else granola_data.get("documents", granola_data.get("notes", []))
            log(f"✅ Found {len(granola_notes)} Granola notes")

        # 2. Fetch YouTube livestreams
        log("🎬 Fetching YouTube livestreams...")
        if yt_api_key:
            yt_streams = fetch_youtube_livestreams(yt_api_key)
            log(f"✅ Found {len(yt_streams)} YouTube livestreams")
        else:
            yt_streams = manual_streams
            log(f"📋 Using {len(yt_streams)} manually provided streams")

        if not yt_streams:
            log("⚠️ No YouTube livestreams found. Please provide YouTube Data API key or add streams manually.", "warn")
            current_job["done"] = True
            current_job["running"] = False
            return

        # 3. Match streams to Granola notes
        log("🔗 Matching YouTube streams to Granola notes...")
        results = []

        for stream in yt_streams:
            stream_title = stream.get("title", "")
            stream_date_raw = stream.get("published_at", "")
            video_id = stream.get("id", "")
            video_url = stream.get("url", f"https://www.youtube.com/watch?v={video_id}")

            # Parse date
            try:
                if stream_date_raw:
                    dt = datetime.fromisoformat(stream_date_raw.replace("Z", "+00:00"))
                    date_str = dt.strftime("%-m/%-d/%y")
                else:
                    date_str = stream.get("date", "Unknown Date")
            except Exception:
                date_str = stream.get("date", "Unknown Date")

            log(f"\n📹 Processing: {stream_title} ({date_str})")

            # Fetch timestamped transcript so we can run video-assisted diarization.
            yt_segments = get_youtube_transcript_timestamped(video_id) if video_id else []
            transcript = " ".join(s["text"] for s in yt_segments) if yt_segments else ""
            stream_description = stream.get("description", "")

            if not transcript and not stream_description and not stream_title:
                log(f"⏭️ Skipping '{stream_title}' — no content found (interviewee likely didn't show)", "warn")
                results.append({"stream": stream_title, "date": date_str, "status": "skipped_no_content"})
                continue

            # Find matching Granola note
            matched_note = None
            matched_note_detail = None

            for note in granola_notes:
                note_title = note.get("title", "") or note.get("name", "")
                note_date = note.get("created_at", "") or note.get("date", "")
                # Match by date proximity or title similarity
                if date_str and date_str in note_title:
                    matched_note = note
                    break
                if stream_title.lower()[:15] in note_title.lower():
                    matched_note = note
                    break
                # Try date matching
                try:
                    if note_date and stream_date_raw:
                        note_dt = datetime.fromisoformat(note_date.replace("Z", "+00:00"))
                        stream_dt = datetime.fromisoformat(stream_date_raw.replace("Z", "+00:00"))
                        if abs((note_dt - stream_dt).days) <= 1:
                            matched_note = note
                            break
                except Exception:
                    pass

            granola_summary = None
            granola_transcript = None

            if matched_note:
                note_id = matched_note.get("id", "")
                if note_id:
                    matched_note_detail = fetch_granola_note_detail(granola_key, note_id)
                    if matched_note_detail:
                        granola_summary = (
                            matched_note_detail.get("ai_summary") or
                            matched_note_detail.get("summary") or
                            matched_note_detail.get("content", "")
                        )
                        granola_transcript = (
                            matched_note_detail.get("transcript") or
                            matched_note_detail.get("transcription", "")
                        )
                log(f"✅ Matched Granola note: {matched_note.get('title', 'untitled')}")
            else:
                log(f"⚠️ NO Granola note found for '{stream_title}' on {date_str} — folder will be created without Granola docs", "warn")
                results.append({
                    "stream": stream_title,
                    "date": date_str,
                    "status": "no_granola_note",
                    "warning": f"No Granola note found for {stream_title} ({date_str})"
                })

            # Build the best-available transcript.
            # Priority:
            #   1. Granola transcript if available (often already has speaker labels).
            #   2. Video-assisted diarized YT transcript (if ENABLE_VIDEO_DIARIZATION).
            #   3. Plain YT transcript.
            best_transcript = granola_transcript or ""

            video_diarization_enabled = os.environ.get("ENABLE_VIDEO_DIARIZATION", "").lower() in ("1", "true", "yes")
            if not best_transcript and yt_segments and video_id and video_diarization_enabled:
                log("🎥 Running video-assisted speaker diarization (ENABLE_VIDEO_DIARIZATION=1)...")
                best_transcript = diarize_transcript_with_video(video_id, yt_segments, stream_title)
            elif not best_transcript:
                best_transcript = transcript

            # AI extraction
            log("🤖 Running AI analysis...")
            interview_data = None
            if best_transcript or granola_summary:
                interview_data = extract_interview_data(best_transcript, granola_summary or "", stream_title)

            # Generate folder name
            topic_words = stream_title.replace("Focus Group", "").replace("Interview", "").strip()
            topic_short = " ".join(topic_words.split()[:5]) if topic_words else "Interview"
            folder_name = f"({date_str}) {topic_short}"

            log(f"📁 Creating folder: {folder_name}")
            folder_id = create_drive_folder(folder_name, MAIN_FOLDER_ID)

            doc_links = {}

            # ── Doc 1: Interview Debrief (Google Sheet) ──────────────────────
            log("📊 Creating Interview Debrief sheet...")
            headers, rows = build_debrief_sheet(interview_data)
            sheet_id, sheet_url = create_google_sheet(
                f"Process: Interview Debrief ({date_str})",
                headers, rows, folder_id
            )
            doc_links["debrief"] = sheet_url
            log(f"  ✅ Debrief sheet created")

            # ── Doc 2: Interview Guide (Google Doc) ──────────────────────────
            log("📋 Creating Interview Guide doc...")
            guide_questions = []
            if interview_data:
                guide_questions = interview_data.get("interview_guide_questions", [])
            if not guide_questions and interview_data:
                guide_questions = [q.get("question", "") for q in interview_data.get("questions", [])]

            guide_text = f"User Interview Guide\n{date_str}\n\n"
            guide_text += "Interview Objectives:\n[Add objectives here]\n\n"
            guide_text += "Introduction Script:\n[Welcome interviewee, introduce team, explain format]\n\n"
            guide_text += "Interview Questions:\n\n"
            for i, q in enumerate(guide_questions, 1):
                guide_text += f"{i}. {q}\n\n"
            if not guide_questions:
                guide_text += "1. [Questions not extracted — add from Granola notes]\n"

            _, guide_url = create_google_doc(
                f"User Interview Guide ({date_str})",
                doc_text_requests(guide_text),
                folder_id
            )
            doc_links["guide"] = guide_url
            log(f"  ✅ Interview Guide created")

            # ── Doc 3: Recording Link (Google Doc) — ONLY if a video matched ─
            if video_id and video_url:
                log("🎥 Creating Recording doc...")
                recording_text = (
                    f"({date_str}) Recording\n\n"
                    f"Go to this Link: {video_url}\n\n"
                    f"Focus Group ({date_str})\n\n"
                    f"Ensure you're logged in to Youtube with a heretic.fund account"
                )
                recording_doc_id, recording_url = create_google_doc(
                    f"({date_str}) Recording",
                    doc_text_requests(recording_text),
                    folder_id
                )
                doc_links["recording"] = recording_url
                log(f"  ✅ Recording doc created")

                # Copy recording doc to second folder
                log("📋 Copying Recording doc to recordings folder...")
                copy_file_to_folder(recording_doc_id, f"({date_str}) Recording", RECORDINGS_FOLDER_ID)
                log(f"  ✅ Recording copy added to recordings folder")
            else:
                log("  ⏭️ Skipping Recording doc — no YouTube video matched")

            # ── Doc 4: Interview Summary & Takeaways (single merged doc) ─────
            # This replaces the old "Granola Summary" + "High-Level Product
            # Takeaways" duplicate pair. One doc, richer content.
            log("💡 Generating Interview Summary & Takeaways...")
            hl_summary = synthesize_high_level_summary(
                granola_summary, best_transcript, interview_data, stream_title
            )
            combined_text = build_combined_takeaways_text(date_str, granola_summary, hl_summary)
            _, combined_url = create_google_doc(
                f"({date_str}) Interview Summary & Takeaways",
                doc_text_requests(combined_text),
                folder_id
            )
            doc_links["takeaways"] = combined_url
            log(f"  ✅ Combined Takeaways doc created")

            folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
            log(f"✅ Folder complete: {folder_name} → {folder_url}")

            results.append({
                "stream": stream_title,
                "date": date_str,
                "folder_name": folder_name,
                "folder_url": folder_url,
                "status": "created",
                "has_granola": bool(matched_note),
                "doc_links": doc_links,
            })

        log("\n🎉 Workflow complete!")
        current_job["results"] = results
        current_job["done"] = True
        current_job["running"] = False

    except Exception as e:
        import traceback
        log(f"❌ Fatal error: {e}\n{traceback.format_exc()}", "error")
        current_job["error"] = str(e)
        current_job["done"] = True
        current_job["running"] = False



# ─── Granola-Direct Workflow (Granola as source of truth + YouTube for transcript) ─
def find_youtube_video_for_date(all_videos, target_date_str):
    """
    Given a list of all channel videos and a date string like '5/16/25',
    find the best matching video by date (within 1 day).
    Returns the video dict or None.
    """
    try:
        dt_target = datetime.strptime(target_date_str, "%m/%d/%y")
    except Exception:
        return None

    best = None
    best_delta = 999
    for v in all_videos:
        pub = v.get("published_at", "")
        if not pub:
            continue
        try:
            dt_v = datetime.fromisoformat(pub.replace("Z", "+00:00")).replace(tzinfo=None)
            delta = abs((dt_v.date() - dt_target.date()).days)
            if delta <= 1 and delta < best_delta:
                best_delta = delta
                best = v
        except Exception:
            continue
    return best


def run_granola_direct_workflow(sessions):
    """
    Create Drive folders + docs from pre-matched session data.
    Granola meeting IDs are the source of truth.
    YouTube videos are fetched once and matched by date for transcript context.

    Each session dict:
      {
        "date": "5/16/25",           # M/D/YY format
        "folder_name": "(5/16/25) User Research PMF Block",
        "topic": "User Research PMF Block",
        "granola_summary": "...",    # Granola AI summary text
        "granola_notes": "...",      # raw Granola private notes
        "meeting_ids": ["uuid1"],    # original Granola meeting IDs
      }
    """
    global progress_log, current_job
    progress_log = []
    current_job = {"running": True, "done": False, "error": None, "results": []}

    try:
        log(f"🚀 Starting Granola-Direct Workflow for {len(sessions)} sessions...")

        # ── Fetch ALL YouTube videos once upfront ─────────────────────────────
        log("🎬 Fetching all YouTube videos from channel (for transcript matching)...")
        all_videos = fetch_youtube_livestreams(None)  # uses OAuth creds
        log(f"  Found {len(all_videos)} videos on channel")

        results = []

        for s in sessions:
            date_str = s["date"]
            folder_name = s["folder_name"]
            granola_summary = s.get("granola_summary", "")
            granola_notes = s.get("granola_notes", "")
            topic = s.get("topic", folder_name)

            log(f"\n📁 Processing: {folder_name}")

            # ── Match YouTube video by date ───────────────────────────────────
            matched_video = find_youtube_video_for_date(all_videos, date_str)
            yt_segments = []
            if matched_video:
                video_url = matched_video["url"]
                video_id = matched_video["id"]
                log(f"  🎬 Matched YouTube video: {matched_video['title']} ({matched_video['published_at'][:10]})")
                log(f"  ⬇️ Fetching YouTube transcript (timestamped)...")
                yt_segments = get_youtube_transcript_timestamped(video_id)
                yt_transcript = " ".join(s["text"] for s in yt_segments)
                if yt_transcript:
                    log(f"  ✅ Got YouTube transcript ({len(yt_transcript)} chars, {len(yt_segments)} segments)")
                else:
                    log(f"  ⚠️ No transcript available for this video", "warn")
            else:
                video_url = ""
                video_id = ""
                yt_transcript = ""
                log(f"  ⚠️ No YouTube video found within 1 day of {date_str}", "warn")

            # Video-assisted diarization to recover speaker attribution.
            video_diarization_enabled = os.environ.get("ENABLE_VIDEO_DIARIZATION", "").lower() in ("1", "true", "yes")
            combined_transcript = yt_transcript
            if yt_segments and video_id and video_diarization_enabled:
                log("🎥 Running video-assisted speaker diarization (ENABLE_VIDEO_DIARIZATION=1)...")
                combined_transcript = diarize_transcript_with_video(video_id, yt_segments, topic) or yt_transcript

            # ── AI extraction using BOTH Granola notes AND (diarized) YT transcript ─
            log("🤖 Running AI analysis (Granola + YouTube)...")
            interview_data = None
            if combined_transcript or granola_summary or granola_notes:
                interview_data = extract_interview_data(
                    combined_transcript,
                    granola_summary or granola_notes,
                    topic
                )

            # ── Create main folder ────────────────────────────────────────────
            log(f"📁 Creating folder: {folder_name}")
            folder_id = create_drive_folder(folder_name, MAIN_FOLDER_ID)
            doc_links = {}

            # ── Doc 1: Interview Debrief (Google Sheet) ───────────────────────
            log("📊 Creating Interview Debrief sheet...")
            headers, rows = build_debrief_sheet(interview_data)
            sheet_id, sheet_url = create_google_sheet(
                f"Process: Interview Debrief ({date_str})",
                headers, rows, folder_id
            )
            doc_links["debrief"] = sheet_url
            log("  ✅ Debrief sheet created")

            # ── Doc 2: User Interview Guide (Google Doc) ──────────────────────
            log("📋 Creating Interview Guide doc...")
            guide_questions = []
            if interview_data:
                guide_questions = interview_data.get("interview_guide_questions", [])
                if not guide_questions:
                    guide_questions = [q.get("question", "") for q in interview_data.get("questions", [])]

            guide_text = f"User Interview Guide\n{date_str}\n\n"
            guide_text += "Interview Objectives:\n[Add objectives here]\n\n"
            guide_text += "Introduction Script:\n[Welcome interviewee, introduce team, explain format]\n\n"
            guide_text += "Interview Questions:\n\n"
            for i, q in enumerate(guide_questions, 1):
                guide_text += f"{i}. {q}\n\n"
            if not guide_questions:
                guide_text += "1. [Questions not extracted — add from Granola notes]\n"

            _, guide_url = create_google_doc(
                f"User Interview Guide ({date_str})",
                doc_text_requests(guide_text),
                folder_id
            )
            doc_links["guide"] = guide_url
            log("  ✅ Interview Guide created")

            # ── Doc 3: Recording Link (Google Doc) — ONLY if a video matched ─
            if video_id and video_url:
                log("🎥 Creating Recording doc...")
                recording_text = (
                    f"({date_str}) Recording\n\n"
                    f"Go to this Link: {video_url}\n\n"
                    f"Focus Group ({date_str})\n\n"
                    f"Ensure you're logged in to Youtube with a heretic.fund account"
                )
                recording_doc_id, recording_url = create_google_doc(
                    f"({date_str}) Recording",
                    doc_text_requests(recording_text),
                    folder_id
                )
                doc_links["recording"] = recording_url
                log("  ✅ Recording doc created")

                # Copy recording doc to recordings folder
                log("📋 Copying Recording doc to recordings folder...")
                copy_file_to_folder(recording_doc_id, f"({date_str}) Recording", RECORDINGS_FOLDER_ID)
                log("  ✅ Recording copy added to recordings folder")
            else:
                log("  ⏭️ Skipping Recording doc — no YouTube video matched")

            # ── Doc 4: Interview Summary & Takeaways (single merged doc) ──────
            log("💡 Generating Interview Summary & Takeaways...")
            hl_summary = synthesize_high_level_summary(
                granola_summary or granola_notes,
                combined_transcript or granola_notes,
                interview_data,
                topic
            )
            combined_text = build_combined_takeaways_text(
                date_str, granola_summary or granola_notes, hl_summary
            )
            _, combined_url = create_google_doc(
                f"({date_str}) Interview Summary & Takeaways",
                doc_text_requests(combined_text),
                folder_id
            )
            doc_links["takeaways"] = combined_url
            log("  ✅ Combined Takeaways doc created")

            folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
            log(f"✅ Folder complete: {folder_name} → {folder_url}")

            results.append({
                "date": date_str,
                "folder_name": folder_name,
                "folder_url": folder_url,
                "status": "created",
                "youtube_matched": bool(matched_video),
                "youtube_url": video_url,
                "doc_links": doc_links,
            })

        log("\n🎉 Workflow complete!")
        current_job["results"] = results
        current_job["done"] = True
        current_job["running"] = False

    except Exception as e:
        import traceback
        log(f"❌ Fatal error: {e}\n{traceback.format_exc()}", "error")
        current_job["error"] = str(e)
        current_job["done"] = True
        current_job["running"] = False


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    # Look for index.html next to app.py first, then templates/
    base = os.path.dirname(os.path.abspath(__file__))
    for path in [os.path.join(base, "index.html"), os.path.join(base, "templates", "index.html")]:
        if os.path.exists(path):
            return Response(open(path).read(), mimetype="text/html")
    return Response("<h2>Error: index.html not found. Place index.html in the same folder as app.py</h2>", mimetype="text/html")


# In-memory store for OAuth flow data (survives redirect)
_oauth_store = {}

@app.route("/auth/login")
def auth_login():
    """Simple login page — enter Google OAuth credentials then redirect to Google"""
    # If already have creds in session, go straight to Google auth
    client_id = session.get("google_client_id")
    client_secret = session.get("google_client_secret")
    if client_id and client_secret:
        return redirect(f"/auth/google?client_id={client_id}&client_secret={client_secret}")

    return Response("""<!DOCTYPE html>
<html>
<head>
  <title>Login with Google</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 500px; margin: 80px auto; padding: 20px; background: #f9f9f9; }
    h2 { color: #1a1a1a; }
    label { font-size: 13px; color: #555; display: block; margin-top: 16px; margin-bottom: 4px; }
    input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; box-sizing: border-box; }
    button { margin-top: 20px; background: #2563eb; color: white; border: none; padding: 12px 28px; font-size: 15px; border-radius: 8px; cursor: pointer; width: 100%; }
    button:hover { background: #1d4ed8; }
    .hint { font-size: 12px; color: #888; margin-top: 6px; }
    .back { display: block; margin-top: 16px; text-align: center; color: #2563eb; font-size: 13px; }
  </style>
</head>
<body>
  <h2>🔐 Login with Google</h2>
  <p style="color:#555;font-size:14px">Enter your Google OAuth credentials to authenticate with Drive, Docs, and YouTube.</p>

  <label>Google Client ID</label>
  <input type="text" id="clientId" placeholder="...apps.googleusercontent.com" />

  <label>Google Client Secret</label>
  <input type="password" id="clientSecret" placeholder="GOCSPX-..." />
  <div class="hint">Find these in Google Cloud Console → APIs & Services → Credentials</div>

  <button onclick="login()">Continue to Google Login →</button>
  <a class="back" href="/batch">← Back to Batch page</a>

  <script>
    // Pre-fill from localStorage if saved before
    const savedId = localStorage.getItem('google_client_id');
    const savedSecret = localStorage.getItem('google_client_secret');
    if (savedId) document.getElementById('clientId').value = savedId;
    if (savedSecret) document.getElementById('clientSecret').value = savedSecret;

    function login() {
      const id = document.getElementById('clientId').value.trim();
      const secret = document.getElementById('clientSecret').value.trim();
      if (!id || !secret) { alert('Please enter both Client ID and Client Secret'); return; }
      localStorage.setItem('google_client_id', id);
      localStorage.setItem('google_client_secret', secret);
      window.location.href = '/auth/google?client_id=' + encodeURIComponent(id) + '&client_secret=' + encodeURIComponent(secret);
    }

    // Allow Enter key
    document.addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
  </script>
</body>
</html>""", mimetype="text/html")




@app.route("/auth/google")
def auth_google():
    client_id = request.args.get("client_id") or session.get("google_client_id")
    client_secret = request.args.get("client_secret") or session.get("google_client_secret")
    if not client_id or not client_secret:
        return jsonify({"error": "Google OAuth client_id and client_secret required"}), 400

    import secrets, urllib.parse
    state = secrets.token_urlsafe(24)
    _oauth_store[state] = {"client_id": client_id, "client_secret": client_secret}
    session["oauth_state"] = state
    session["google_client_id"] = client_id
    session["google_client_secret"] = client_secret
    session.modified = True

    params = {
        "client_id": client_id,
        "redirect_uri": "http://localhost:5050/auth/callback",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    import urllib.parse
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    error = request.args.get("error", "")

    if error:
        return Response(f"<h3>Auth Error: {error}. <a href='/ '>Go back</a></h3>", mimetype="text/html")

    stored = _oauth_store.get(state, {})
    client_id = stored.get("client_id") or session.get("google_client_id")
    client_secret = stored.get("client_secret") or session.get("google_client_secret")

    if not client_id or not client_secret:
        return Response("<h3>Auth Error: session expired. <a href='/'>Go back</a> and try again.</h3>", mimetype="text/html")

    # Exchange code for token directly via requests (avoids PKCE issues)
    token_resp = http_requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": "http://localhost:5050/auth/callback",
        "grant_type": "authorization_code",
    })
    token_data = token_resp.json()

    if "error" in token_data:
        return Response(f"<h3>Token Error: {token_data}. <a href='/'>Go back</a></h3>", mimetype="text/html")

    session.permanent = True  # persist across browser sessions
    session["google_token"] = {
        "token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    session["google_client_id"] = client_id
    session["google_client_secret"] = client_secret
    session.modified = True
    _oauth_store.pop(state, None)
    return redirect("/batch")


@app.route("/auth/status")
def auth_status():
    return jsonify({"authenticated": bool(session.get("google_token"))})


@app.route("/granola/test", methods=["POST"])
def granola_test():
    data = request.json
    api_key = data.get("api_key", "")
    if not api_key:
        return jsonify({"ok": False, "error": "No API key"})
    result = fetch_granola_notes(api_key)
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]})
    notes = result if isinstance(result, list) else result.get("documents", result.get("notes", []))
    return jsonify({"ok": True, "count": len(notes), "sample": notes[:2] if notes else []})


@app.route("/youtube/test", methods=["POST"])
def youtube_test():
    data = request.json
    api_key = data.get("api_key", "")
    streams = fetch_youtube_livestreams(api_key)
    if streams is None:
        return jsonify({"ok": False, "error": "No API key and not authenticated"})
    return jsonify({"ok": True, "count": len(streams), "streams": streams[:3]})


@app.route("/run", methods=["POST"])
def run():
    if not session.get("google_token"):
        return jsonify({"error": "Not authenticated with Google"}), 401
    if current_job["running"]:
        return jsonify({"error": "Job already running"}), 409

    config = request.json
    session["anthropic_api_key"] = config.get("anthropic_api_key", "")
    # Capture session data before thread launch (threads have no request context)
    _g_token_data["token"] = dict(session.get("google_token", {}))
    _g_anthropic_key["key"] = config.get("anthropic_api_key", "")

    thread = threading.Thread(target=run_workflow, args=(config,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Workflow started"})


@app.route("/run-granola-direct", methods=["POST"])
def run_granola_direct():
    """
    Run the Granola-direct workflow.
    Expects JSON body:
    {
      "anthropic_api_key": "sk-ant-...",
      "sessions": [
        {
          "date": "5/16/25",
          "folder_name": "(5/16/25) User Research PMF Block",
          "topic": "User Research PMF Block",
          "granola_summary": "...",
          "granola_notes": "...",
          "meeting_ids": ["uuid1", "uuid2"]
        },
        ...
      ]
    }
    """
    if not session.get("google_token"):
        return jsonify({"error": "Not authenticated with Google"}), 401
    if current_job["running"]:
        return jsonify({"error": "Job already running"}), 409

    data = request.json
    session["anthropic_api_key"] = data.get("anthropic_api_key", "")
    sessions = data.get("sessions", [])

    if not sessions:
        return jsonify({"error": "No sessions provided"}), 400

    # Capture session data before thread launch (threads have no request context)
    _g_token_data["token"] = dict(session.get("google_token", {}))
    _g_anthropic_key["key"] = data.get("anthropic_api_key", "")

    thread = threading.Thread(
        target=run_granola_direct_workflow,
        args=(sessions,),
        daemon=True
    )
    thread.start()
    return jsonify({"ok": True, "message": f"Granola-direct workflow started for {len(sessions)} sessions"})


@app.route("/status")
def status():
    return jsonify({
        "running": current_job["running"],
        "done": current_job["done"],
        "error": current_job["error"],
        "results": current_job.get("results", []),
        "log": progress_log[-50:],  # last 50 entries
    })


@app.route("/preview/streams", methods=["POST"])
def preview_streams():
    """Preview YouTube streams without running the full workflow"""
    data = request.json
    yt_key = data.get("youtube_api_key", "")
    granola_key = data.get("granola_api_key", "")

    streams = fetch_youtube_livestreams(yt_key) if yt_key else []
    granola_data = fetch_granola_notes(granola_key) if granola_key else {}
    notes = granola_data if isinstance(granola_data, list) else granola_data.get("documents", granola_data.get("notes", []))

    # Match and annotate
    preview = []
    for s in streams:
        has_note = any(
            s.get("title", "").lower()[:10] in n.get("title", "").lower() or
            (s.get("published_at", "") and s.get("published_at", "")[:10] in n.get("created_at", ""))
            for n in notes
        )
        preview.append({
            "title": s.get("title"),
            "date": s.get("published_at", "")[:10],
            "url": s.get("url"),
            "has_granola_note": has_note,
            "thumbnail": s.get("thumbnail"),
        })

    return jsonify({"streams": preview, "total_notes": len(notes)})


@app.route("/run-granola", methods=["POST"])
def run_granola():
    """
    Trigger folder creation from pre-matched Granola sessions.
    Body: { "anthropic_api_key": "...", "sessions": [...] }
    Each session: { date, folder_name, topic, granola_summary, granola_notes, video_url, meeting_ids }
    """
    if not session.get("google_token"):
        return jsonify({"error": "Not authenticated with Google"}), 401
    if current_job["running"]:
        return jsonify({"error": "Job already running"}), 409

    data = request.json
    session["anthropic_api_key"] = data.get("anthropic_api_key", "")
    sessions = data.get("sessions", [])

    if not sessions:
        return jsonify({"error": "No sessions provided"}), 400

    # Capture session data before thread launch (threads have no request context)
    _g_token_data["token"] = dict(session.get("google_token", {}))
    _g_anthropic_key["key"] = data.get("anthropic_api_key", "")

    thread = threading.Thread(target=run_granola_direct_workflow, args=(sessions,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": f"Workflow started for {len(sessions)} sessions"})


# ─── Sessions ────────────────────────────────────────────────────────────────
# Populate this list with your own session data, or load it from a JSON file.
# Each session should contain the fields shown in the examples below.
# granola_summary and granola_notes can be pre-fetched from Granola and pasted here,
# or left empty if you want to rely solely on YouTube transcripts.
#
# To load from a file instead:
#   import json
#   with open("sessions.json") as f:
#       BATCH_SESSIONS = json.load(f)
#
# See sessions.example.json for the expected format.

BATCH_SESSIONS = [
  {
    "date": "1/15/25",
    "folder_name": "(1/15/25) Example Focus Group Session",
    "topic": "Example Focus Group - Product Feedback",
    "meeting_ids": ["xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
    "granola_summary": "Brief AI-generated summary of the session from Granola. Describes key themes, participant reactions, and notable quotes.",
    "granola_notes": "Raw notes from the meeting. Participant names, specific feedback points, feature requests, bugs observed.",
  },
  {
    "date": "1/22/25",
    "folder_name": "(1/22/25) Example 1:1 User Interview",
    "topic": "1:1 User Interview - Onboarding Flow",
    "meeting_ids": ["yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"],
    "granola_summary": "Summary of a 1:1 interview. User struggled with onboarding step 3. Prefers visual walkthroughs over text instructions.",
    "granola_notes": "Participant background. Task completion notes. Usability issues. Follow-up questions to explore.",
  },
  {
    "date": "2/5/25",
    "folder_name": "(2/5/25) Example Design Review",
    "topic": "Design Review - Homepage Variants",
    "meeting_ids": ["zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
    "granola_summary": "Design review session covering two homepage variants. Variant B preferred by 4/5 participants for clarity.",
    "granola_notes": "Prototype ratings. Specific UI feedback per participant. Action items for design team.",
  },
]



@app.route("/batch")
def batch_page():
    """Batch trigger page with baked-in sessions payload"""
    auth_status = "✅ Authenticated with Google" if session.get("google_token") else "❌ Not authenticated — <a href='/auth/login' style='color:#dc2626;font-weight:bold'>Click here to login with Google first</a>"
    is_authed = "true" if session.get("google_token") else "false"
    job_status = "running" if current_job["running"] else ("done" if current_job["done"] else "idle")
    return Response(f"""<!DOCTYPE html>
<html>
<head>
  <title>Batch Interview Folders</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 900px; margin: 40px auto; padding: 20px; background: #f9f9f9; }}
    h1 {{ color: #1a1a1a; }}
    .status {{ padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; background: #fff; border: 1px solid #ddd; }}
    .btn {{ background: #2563eb; color: white; border: none; padding: 14px 32px; font-size: 16px; border-radius: 8px; cursor: pointer; margin-right: 10px; }}
    .btn:hover {{ background: #1d4ed8; }}
    .btn:disabled {{ background: #93c5fd; cursor: not-allowed; }}
    .btn-danger {{ background: #dc2626; }}
    .btn-danger:hover {{ background: #b91c1c; }}
    input[type=text] {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; box-sizing: border-box; margin-bottom: 12px; }}
    label {{ font-size: 13px; color: #555; display: block; margin-bottom: 4px; }}
    #log {{ background: #1e1e1e; color: #d4d4d4; padding: 16px; border-radius: 8px; height: 350px; overflow-y: auto; font-family: monospace; font-size: 13px; white-space: pre-wrap; }}
    #results {{ margin-top: 20px; }}
    .result-item {{ background: white; border: 1px solid #ddd; border-radius: 8px; padding: 12px 16px; margin-bottom: 10px; }}
    .result-item a {{ color: #2563eb; }}
    .session-list {{ background: white; border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin-bottom: 20px; max-height: 200px; overflow-y: auto; font-size: 13px; }}
    .session-list div {{ padding: 3px 0; border-bottom: 1px solid #f0f0f0; }}
    .green {{ color: #16a34a; }} .red {{ color: #dc2626; }} .orange {{ color: #ea580c; }}
  </style>
</head>
<body>
  <h1>📁 Batch Interview Folders</h1>

  <div class="status">
    <strong>Auth:</strong> {auth_status}<br>
    <strong>Job status:</strong> <span id="jobStatus">{job_status}</span>
  </div>

  <div class="session-list">
    <strong>{len(BATCH_SESSIONS)} sessions queued:</strong><br>
    {"".join(f'<div>{s["date"]} — {s["folder_name"]}</div>' for s in BATCH_SESSIONS)}
  </div>

  <label>Anthropic API Key (required for AI synthesis)</label>
  <input type="text" id="anthropicKey" placeholder="sk-ant-..." />

  <div>
    <button class="btn" id="runBtn" onclick="runBatch()">🚀 Run Batch ({len(BATCH_SESSIONS)} folders)</button>
    <button class="btn" style="background:#6b7280" onclick="location.href='/'">← Back to main</button>
  </div>

  <br>
  <div id="log">Waiting to start...</div>
  <div id="results"></div>

  <script>
    let polling = null;

    async function runBatch() {{
      const key = document.getElementById('anthropicKey').value.trim();
      if (!key) {{ alert('Please enter your Anthropic API key'); return; }}

      document.getElementById('runBtn').disabled = true;
      document.getElementById('log').textContent = 'Starting batch...\\n';

      const sessions = {__import__('json').dumps(BATCH_SESSIONS)};

      const resp = await fetch('/run-granola-direct', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ anthropic_api_key: key, sessions: sessions }})
      }});
      const data = await resp.json();
      if (!data.ok) {{
        document.getElementById('log').textContent += 'Error: ' + (data.error || JSON.stringify(data)) + '\\n';
        document.getElementById('runBtn').disabled = false;
        return;
      }}
      document.getElementById('log').textContent += data.message + '\\n';
      startPolling();
    }}

    function startPolling() {{
      if (polling) clearInterval(polling);
      polling = setInterval(pollStatus, 3000);
    }}

    async function pollStatus() {{
      const resp = await fetch('/status');
      const data = await resp.json();
      document.getElementById('jobStatus').textContent = data.running ? 'running' : (data.done ? 'done' : 'idle');

      if (data.log && data.log.length) {{
        document.getElementById('log').textContent = data.log.join('\\n');
        document.getElementById('log').scrollTop = document.getElementById('log').scrollHeight;
      }}

      if (data.done || !data.running) {{
        clearInterval(polling);
        document.getElementById('runBtn').disabled = false;
        if (data.results && data.results.length) {{
          renderResults(data.results);
        }}
        if (data.error) {{
          document.getElementById('log').textContent += '\\n❌ Error: ' + data.error;
        }}
      }}
    }}

    function renderResults(results) {{
      let html = '<h2>Results</h2>';
      for (const r of results) {{
        const yt = r.youtube_matched ? '<span class="green">✅ YouTube matched</span>' : '<span class="orange">⚠️ No YouTube video</span>';
        html += `<div class="result-item">
          <strong>{{r.folder_name}}</strong> ${{yt}}<br>
          <a href="${{r.folder_url}}" target="_blank">📁 Open folder</a>
        </div>`;
      }}
      document.getElementById('results').innerHTML = html;
    }}

    // If already running, start polling immediately
    if ('{job_status}' === 'running') startPolling();

    // Disable run button if not authenticated
    if ({is_authed} === false) {{
      document.getElementById('runBtn').disabled = true;
      document.getElementById('runBtn').title = 'Login with Google first';
    }}
  </script>
</body>
</html>""", mimetype="text/html")


if __name__ == "__main__":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    print("\n🎙️  Interview Automation Tool")
    print("━" * 40)
    print("➡  Open: http://localhost:5050")
    print("━" * 40 + "\n")
    app.run(debug=False, port=5050)
