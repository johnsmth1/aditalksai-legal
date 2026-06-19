# AdiTalksAI Legal Site

Static legal pages for TikTok Developer app registration.

## Pages

- `index.html` - public landing page and app description
- `privacy.html/` - Privacy Policy URL
- `terms.html/` - Terms of Service URL

## GitHub Pages Setup

1. Create a new GitHub repository, for example `aditalksai-legal`.
2. Push this folder's contents to the repository root.
3. In GitHub, go to `Settings > Pages`.
4. Set source to `Deploy from a branch`.
5. Choose branch `main` and folder `/root`.
6. GitHub will publish a URL like:

   `https://YOUR_GITHUB_USERNAME.github.io/aditalksai-legal/`

Use these TikTok Developer form URLs:

- Terms of Service URL:
  `https://YOUR_GITHUB_USERNAME.github.io/aditalksai-legal/terms.html/`
- Privacy Policy URL:
  `https://YOUR_GITHUB_USERNAME.github.io/aditalksai-legal/privacy.html/`

## Before Submitting to TikTok

- Replace `YOUR_GITHUB_USERNAME` with the real GitHub username after publishing.
- If you have a public contact email, add it to both policy pages.
- In TikTok Developer URL properties, verify the GitHub Pages URL prefix if TikTok asks for URL verification.

## Local TikTok Uploader

This repo also contains a local desktop uploader app for the AdiTalksAI TikTok workflow. It uses only Python's standard library.

Run it locally:

```bash
./run_tiktok_uploader.sh
```

Open:

```text
http://localhost:8501/
```

TikTok Developer configuration:

- Platform: `Desktop`
- Redirect URI: `http://localhost:8501/`
- Products: `Login Kit`, `Content Posting API`
- Scopes: `user.info.basic`, `video.upload`
- Direct Post: off
- Media transfer: `FILE_UPLOAD`

The app reads local credentials from `.secrets/tiktok_credentials.txt`, which is ignored by git.
