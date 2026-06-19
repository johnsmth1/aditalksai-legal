from __future__ import annotations

import cgi
import hashlib
import html
import json
import math
import mimetypes
import os
import secrets
import string
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_ROOT.parent
SECRETS_DIR = APP_ROOT / ".secrets"
CREDENTIALS_FILE = SECRETS_DIR / "tiktok_credentials.txt"
FLOW_FILE = SECRETS_DIR / "oauth_flow.json"
TOKEN_FILE = SECRETS_DIR / "tiktok_tokens.json"
LAST_UPLOAD_FILE = SECRETS_DIR / "last_upload.json"
UPLOADS_DIR = SECRETS_DIR / "uploads"

DEFAULT_VIDEO = WORKSPACE_ROOT / "What is AI Hallucination Short" / "short_captioned.mp4"

HOST = "127.0.0.1"
PORT = 8501
REDIRECT_URI = "http://localhost:8501/"
CLIENT_SCOPES = "user.info.basic,video.upload"

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
UPLOAD_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"


class AppError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def read_credentials() -> dict[str, str]:
    if not CREDENTIALS_FILE.exists():
        return {}

    lines = [line.strip() for line in CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    credentials: dict[str, str] = {}
    for index, line in enumerate(lines):
        key = line.lower()
        if key == "client key" and index + 1 < len(lines):
            credentials["client_key"] = lines[index + 1]
        elif key == "client secret" and index + 1 < len(lines):
            credentials["client_secret"] = lines[index + 1]
    return credentials


def random_pkce_string(length: int = 64) -> str:
    alphabet = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def code_challenge(verifier: str) -> str:
    return hashlib.sha256(verifier.encode("utf-8")).hexdigest()


def start_oauth_url(client_key: str) -> str:
    state = secrets.token_urlsafe(32)
    verifier = random_pkce_string()
    write_json(
        FLOW_FILE,
        {
            "state": state,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
            "scope": CLIENT_SCOPES,
        },
    )

    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": CLIENT_SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def request_json(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        status = error.code
    except urllib.error.URLError as error:
        raise AppError(f"Network error calling TikTok: {error}") from error

    try:
        payload: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"raw": body}

    if status >= 400:
        raise AppError(f"TikTok HTTP {status}: {payload}")

    api_error = payload.get("error")
    if isinstance(api_error, dict) and api_error.get("code") not in (None, "ok"):
        raise AppError(f"TikTok API error: {payload}")

    return payload


def exchange_code_for_token(client_key: str, client_secret: str, code: str, state: str) -> dict[str, Any]:
    flow = read_json(FLOW_FILE)
    if not flow:
        raise AppError("Missing OAuth state. Click Connect with TikTok again.")
    if state != flow.get("state"):
        raise AppError("OAuth state mismatch. Start a fresh TikTok connection.")

    body = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": flow["redirect_uri"],
            "code_verifier": flow["code_verifier"],
        }
    ).encode("utf-8")
    payload = request_json(
        TOKEN_URL,
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
    )
    if "access_token" not in payload:
        raise AppError(f"TikTok did not return an access token: {payload}")
    write_json(TOKEN_FILE, payload)
    return payload


def choose_chunk_size(video_size: int) -> int:
    five_mb = 5 * 1024 * 1024
    sixty_four_mb = 64 * 1024 * 1024
    if video_size <= 0:
        raise AppError("Selected video is empty.")
    if video_size < five_mb:
        return video_size
    return min(sixty_four_mb, video_size)


def init_inbox_upload(access_token: str, video_size: int) -> dict[str, Any]:
    chunk_size = choose_chunk_size(video_size)
    body = json.dumps(
        {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": chunk_size,
                "total_chunk_count": math.ceil(video_size / chunk_size),
            }
        }
    ).encode("utf-8")
    return request_json(
        UPLOAD_INIT_URL,
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )


def put_chunk(upload_url: str, chunk: bytes, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(upload_url, data=chunk, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        status = error.code
        response_headers = dict(error.headers.items())
    except urllib.error.URLError as error:
        raise AppError(f"Network error uploading video to TikTok: {error}") from error

    result = {
        "status_code": status,
        "content_range": response_headers.get("Content-Range"),
        "body": body[:500],
    }
    if status not in (HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.PARTIAL_CONTENT):
        raise AppError(f"Upload failed: {result}")
    return result


def upload_file_to_tiktok(upload_url: str, video_path: Path) -> list[dict[str, Any]]:
    total_size = video_path.stat().st_size
    chunk_size = choose_chunk_size(total_size)
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    responses: list[dict[str, Any]] = []

    with video_path.open("rb") as handle:
        first_byte = 0
        while first_byte < total_size:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            last_byte = first_byte + len(chunk) - 1
            responses.append(
                put_chunk(
                    upload_url,
                    chunk,
                    {
                        "Content-Type": mime_type,
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {first_byte}-{last_byte}/{total_size}",
                    },
                )
            )
            first_byte = last_byte + 1

    return responses


def upload_selected_video(video_path: Path) -> dict[str, Any]:
    token = read_json(TOKEN_FILE)
    access_token = token.get("access_token")
    if not access_token:
        raise AppError("Connect with TikTok before uploading.")
    if not video_path.exists():
        raise AppError(f"Video file not found: {video_path}")

    init_payload = init_inbox_upload(access_token, video_path.stat().st_size)
    data = init_payload.get("data", {}) if isinstance(init_payload, dict) else {}
    upload_url = data.get("upload_url") or init_payload.get("upload_url")
    publish_id = data.get("publish_id") or init_payload.get("publish_id")
    if not upload_url:
        raise AppError(f"TikTok did not return an upload URL: {init_payload}")

    upload_responses = upload_file_to_tiktok(upload_url, video_path)
    result = {
        "video": str(video_path),
        "video_size": video_path.stat().st_size,
        "publish_id": publish_id,
        "init_response": init_payload,
        "upload_responses": upload_responses,
    }
    write_json(LAST_UPLOAD_FILE, result)
    return result


def mask(value: str | None, visible: int = 4) -> str:
    if not value:
        return "not set"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def file_size_label(path: Path) -> str:
    if not path.exists():
        return "missing"
    size = path.stat().st_size
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def render_page(message: str = "", error: str = "") -> str:
    credentials = read_credentials()
    token = read_json(TOKEN_FILE)
    last_upload = read_json(LAST_UPLOAD_FILE)
    connected = bool(token.get("access_token"))
    can_upload = connected and DEFAULT_VIDEO.exists()

    upload_button = "" if can_upload else "disabled"
    status_text = "Connected" if connected else "Not connected"
    status_class = "ok" if connected else "warn"
    upload_summary = ""
    if last_upload:
        upload_summary = f"""
        <section>
          <h2>Last upload</h2>
          <p><strong>Publish ID:</strong> <code>{h(last_upload.get("publish_id", "not returned"))}</code></p>
          <p><strong>Video:</strong> <code>{h(last_upload.get("video"))}</code></p>
          <details><summary>Raw response</summary><pre>{h(json.dumps(last_upload, indent=2))}</pre></details>
        </section>
        """

    alert = ""
    if message:
        alert = f'<div class="alert okbox">{h(message)}</div>'
    if error:
        alert = f'<div class="alert errbox">{h(error)}</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AdiTalksAI TikTok Uploader</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #171923;
      --muted: #606671;
      --line: #d9dde5;
      --accent: #fe2c55;
      --accent-dark: #c81940;
      --ok: #0f8b51;
      --warn: #946200;
      --err: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      width: min(1040px, calc(100vw - 32px));
      margin: 32px auto;
    }}
    header, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 24px;
      margin-bottom: 18px;
      box-shadow: 0 8px 24px rgba(17, 24, 39, 0.05);
    }}
    h1, h2 {{ margin: 0 0 12px; letter-spacing: 0; }}
    p {{ color: var(--muted); line-height: 1.45; }}
    code, pre {{
      background: #f1f3f6;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 2px 6px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    pre {{ padding: 12px; overflow: auto; max-height: 360px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-height: 100%;
    }}
    .badge {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #f7f8fa;
      font-weight: 700;
    }}
    .ok {{ color: var(--ok); }}
    .warn {{ color: var(--warn); }}
    .button {{
      appearance: none;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      display: inline-block;
      font-size: 16px;
      font-weight: 800;
      padding: 12px 18px;
      text-decoration: none;
    }}
    .button:hover {{ background: var(--accent-dark); }}
    .button.secondary {{
      background: #20242d;
    }}
    button:disabled {{
      background: #b8bec8;
      cursor: not-allowed;
    }}
    input[type="file"] {{
      display: block;
      margin: 10px 0 16px;
      width: 100%;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    label {{ color: var(--text); font-weight: 700; }}
    .alert {{
      border-radius: 8px;
      font-weight: 700;
      margin-bottom: 18px;
      padding: 14px 16px;
    }}
    .okbox {{ background: #e8f5ee; color: var(--ok); border: 1px solid #b9e2cc; }}
    .errbox {{ background: #ffeceb; color: var(--err); border: 1px solid #ffbab5; }}
    ol {{ color: var(--muted); line-height: 1.55; }}
    @media (max-width: 760px) {{
      .grid {{ grid-template-columns: 1fr; }}
      main {{ width: min(100vw - 20px, 1040px); margin-top: 12px; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>AdiTalksAI TikTok Uploader</h1>
    <p>Local Desktop Login Kit and Content Posting API flow for uploading your AI Hallucination short as a TikTok draft.</p>
    {alert}
    <p><span class="badge {status_class}">{status_text}</span></p>
  </header>

  <section>
    <h2>Setup</h2>
    <div class="grid">
      <div class="card">
        <p><strong>Client key</strong></p>
        <code>{h(mask(credentials.get("client_key")))}</code>
        <p><strong>Redirect URI to register in TikTok</strong></p>
        <code>{h(REDIRECT_URI)}</code>
      </div>
      <div class="card">
        <p><strong>Scopes</strong></p>
        <code>{h(CLIENT_SCOPES)}</code>
        <p><strong>Default video</strong></p>
        <code>{h(DEFAULT_VIDEO)}</code>
        <p>{h(file_size_label(DEFAULT_VIDEO))}</p>
      </div>
    </div>
    <p>
      <a class="button" href="/connect">Connect with TikTok</a>
      <a class="button secondary" href="/clear-token">Clear local token</a>
    </p>
  </section>

  <section>
    <h2>Upload</h2>
    <form method="post" action="/upload" enctype="multipart/form-data">
      <p>
        <label>
          <input type="checkbox" name="use_default" value="1" checked>
          Use the rendered AI Hallucination short
        </label>
      </p>
      <p>Or choose another local MP4/MOV for a demo or future upload:</p>
      <input type="file" name="video_file" accept="video/mp4,video/quicktime">
      <button class="button" type="submit" {upload_button}>Upload to TikTok draft inbox</button>
    </form>
    <p>The creator finishes captioning, editing, and posting inside TikTok after the draft appears.</p>
  </section>

  {upload_summary}

  <section>
    <h2>Review demo checklist</h2>
    <ol>
      <li>Open this local app at <code>{h(REDIRECT_URI)}</code>.</li>
      <li>Click Connect with TikTok and approve <code>{h(CLIENT_SCOPES)}</code>.</li>
      <li>Return to the local app after OAuth.</li>
      <li>Select or keep the default MP4 video.</li>
      <li>Click Upload to TikTok draft inbox.</li>
      <li>Show the success response and explain that final edit/post happens in TikTok.</li>
    </ol>
  </section>
</main>
</body>
</html>"""


class TikTokUploaderHandler(BaseHTTPRequestHandler):
    server_version = "AdiTalksAIUploader/1.0"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        try:
            if path == "/health":
                self.send_text("ok")
                return
            if path == "/connect":
                credentials = read_credentials()
                client_key = credentials.get("client_key")
                if not client_key:
                    raise AppError(f"Missing credentials file: {CREDENTIALS_FILE}")
                self.redirect(start_oauth_url(client_key))
                return
            if path == "/clear-token":
                if TOKEN_FILE.exists():
                    TOKEN_FILE.unlink()
                self.redirect("/?message=Local%20TikTok%20token%20cleared")
                return
            if query.get("error"):
                raise AppError(query.get("error_description", query.get("error", ["OAuth error"]))[0])
            if query.get("code"):
                credentials = read_credentials()
                exchange_code_for_token(
                    credentials.get("client_key", ""),
                    credentials.get("client_secret", ""),
                    query["code"][0],
                    query.get("state", [""])[0],
                )
                self.redirect("/?message=TikTok%20connected")
                return

            self.send_html(render_page(message=query.get("message", [""])[0]))
        except Exception as error:
            self.send_html(render_page(error=str(error)))

    def do_POST(self) -> None:
        try:
            if urllib.parse.urlparse(self.path).path != "/upload":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            video_path = self.read_video_from_form()
            result = upload_selected_video(video_path)
            publish_id = result.get("publish_id") or "not returned"
            self.send_html(render_page(message=f"Upload completed. Publish ID: {publish_id}"))
        except Exception as error:
            self.send_html(render_page(error=str(error)))

    def read_video_from_form(self) -> Path:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise AppError("Expected multipart form upload.")

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )

        use_default = form.getfirst("use_default")
        file_item = form["video_file"] if "video_file" in form else None
        has_uploaded_file = bool(file_item is not None and getattr(file_item, "filename", ""))

        if use_default and DEFAULT_VIDEO.exists():
            return DEFAULT_VIDEO
        if not has_uploaded_file:
            raise AppError("Select a video file or keep the default AI Hallucination short checked.")

        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(file_item.filename).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=UPLOADS_DIR) as temp:
            while True:
                chunk = file_item.file.read(1024 * 1024)
                if not chunk:
                    break
                temp.write(chunk)
            temp_path = Path(temp.name)
        os.chmod(temp_path, 0o600)
        return temp_path

    def send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_text(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    print(f"AdiTalksAI TikTok uploader running at {REDIRECT_URI}")
    print(f"Default video: {DEFAULT_VIDEO}")
    print("Press Ctrl+C to stop.")
    server = ThreadingHTTPServer((HOST, PORT), TikTokUploaderHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
