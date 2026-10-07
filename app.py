from flask import Flask, jsonify, request, send_file
from urllib.parse import urlparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

import requests
import yt_dlp
from bs4 import BeautifulSoup

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

app = Flask(__name__)

APP_VERSION = "9.4-cloud2"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36"
)


# ============================================================
# BASIC HELPERS
# ============================================================

def normalize_url(url):
    return str(url or "").strip()


def get_ffmpeg_path():
    if imageio_ffmpeg is None:
        return None

    try:
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass

    return None


def validate_url(url):
    """
    Public Instagram/Facebook URLs only.
    No private-account, login-wall, DRM, or access-control bypass.
    """
    url = normalize_url(url)

    if not url:
        return False

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").lower()

    allowed_exact = {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
        "instagr.am",
        "www.instagr.am",
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "mbasic.facebook.com",
        "fb.watch",
    }

    if host in allowed_exact:
        return True

    return (
        host.endswith(".instagram.com")
        or host.endswith(".instagr.am")
        or host.endswith(".facebook.com")
    )


def detect_platform(url):
    host = (urlparse(normalize_url(url)).hostname or "").lower()

    if (
        host == "instagram.com"
        or host.endswith(".instagram.com")
        or host == "instagr.am"
        or host.endswith(".instagr.am")
    ):
        return "instagram"

    if (
        host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fb.watch"
    ):
        return "facebook"

    return "unknown"


def get_common_ytdl_opts():
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    ffmpeg = get_ffmpeg_path()

    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg

    return options


def get_instagram_ytdl_opts():
    """yt-dlp options for public Instagram media in the cloud.

    Cloud deployments must not read a user's local browser cookies.
    This resolver is limited to media exposed publicly without login.
    """
    options = get_common_ytdl_opts()
    options["extractor_args"] = {
        "instagram": {
            "webpage_skip": ["dash", "hls"]
        }
    }
    return options

def get_facebook_ytdl_opts(use_browser_session=False):
    """yt-dlp options for Facebook.

    Facebook share/reel URLs can sometimes return a soft login/parse response.
    First try the normal public request. If that fails, retry with the local
    Chrome session and Chrome impersonation when curl_cffi is available.
    No passwords or cookie values are stored by the app.
    """
    options = get_common_ytdl_opts()

    if use_browser_session:
        options["cookiesfrombrowser"] = ("chrome", None, None, None)
        try:
            import curl_cffi  # noqa: F401
            options["impersonate"] = "chrome-99"
        except Exception:
            pass

    return options


def extract_facebook_video_urls(html):
    """Best-effort extraction of Facebook video CDN URLs from public HTML."""
    results = []

    patterns = [
        r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',
        r'"playable_url"\s*:\s*"([^"]+)"',
        r'"browser_native_hd_url"\s*:\s*"([^"]+)"',
        r'"browser_native_sd_url"\s*:\s*"([^"]+)"',
        r'"video_url"\s*:\s*"([^"]+)"',
        r'<meta[^>]+property=["\'](?:og:video|og:video:url|og:video:secure_url)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\'](?:og:video|og:video:url|og:video:secure_url)["\']',
    ]

    for pattern in patterns:
        for value in re.findall(pattern, html, flags=re.IGNORECASE):
            value = str(value)
            value = (
                value.replace('\\/', '/')
                     .replace('\\u0025', '%')
                     .replace('\\u0026', '&')
                     .replace('\\u003D', '=')
                     .replace('\\u003d', '=')
                     .replace('\\u002F', '/')
                     .replace('&amp;', '&')
            )
            try:
                value = json.loads('"' + value.replace('"', '\\"') + '"')
            except Exception:
                pass
            if value.startswith(('http://', 'https://')) and ('fbcdn' in value or 'facebook' in value):
                if value not in results:
                    results.append(value)

    return results


def gallery_dl_facebook_urls(url, use_browser_session=True):
    """Ask gallery-dl for Facebook media URLs without downloading them.

    gallery-dl has a maintained Facebook extractor and supports loading the
    user's existing browser cookies. This is used only as a fallback when
    yt-dlp cannot parse Facebook's current page format.
    """
    commands = []

    if use_browser_session:
        commands.append([
            sys.executable,
            "-m",
            "gallery_dl",
            "-g",
            "--cookies-from-browser",
            "chrome",
            url,
        ])

    commands.append([
        sys.executable,
        "-m",
        "gallery_dl",
        "-g",
        url,
    ])

    errors = []
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except Exception as exc:
            errors.append(str(exc) or "gallery-dl could not start")
            continue

        urls = []
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if line.startswith(("http://", "https://")):
                if line not in urls:
                    urls.append(line)

        if urls:
            return urls, ""

        stderr = (completed.stderr or "").strip()
        if stderr:
            errors.append(stderr[-1000:])

    return [], "; ".join(errors[-2:])


def resolve_facebook_with_gallery_dl(url):
    """Resolve one Facebook video URL through gallery-dl as a fallback."""
    urls, error = gallery_dl_facebook_urls(url, use_browser_session=True)
    if urls:
        return {
            "id": "facebook_gallery_dl",
            "title": "Facebook Video",
            "url": urls[0],
            "thumbnail": "",
            "webpage_url": url,
            "_mydownloader_direct": True,
            "_gallery_dl": True,
        }
    if error:
        raise RuntimeError(error)
    raise RuntimeError("gallery-dl did not expose a Facebook media URL.")


def fetch_facebook_html(url):
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": "https://www.facebook.com/",
        },
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text, response.url


def resolve_facebook_share_redirect(url):
    """Resolve Facebook /share/ URLs to their final public URL before yt-dlp.

    Some Facebook share links fail in yt-dlp while the redirected canonical
    video URL is extractable. This only follows normal HTTP redirects.
    """
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.facebook.com/",
            },
            timeout=20,
            allow_redirects=True,
        )
        final_url = (response.url or url).strip()
        return final_url or url
    except Exception:
        return url


def facebook_access_message(url, error_text):
    """Return a friendly message when a Facebook share URL appears restricted."""
    text = (error_text or "").lower()
    path = (urlparse(normalize_url(url)).path or "").lower()
    indicators = (
        "cannot parse data", "bad request", "login", "log in",
        "not available", "private", "permission", "checkpoint",
        "requires authentication", "cookies",
    )
    if "/share/" in path and any(item in text for item in indicators):
        return (
            "This Facebook link appears to be private or login-required. "
            "My Downloader supports publicly accessible Facebook videos, "
            "but it cannot bypass Facebook privacy or access controls. "
            "If you own the post, make it publicly accessible and try again."
        )
    return None


def resolve_facebook_with_fallback(url):
    """Resolve Facebook using extractor, generic HTML, then public page media URLs."""
    errors = []
    final_url = resolve_facebook_share_redirect(url)
    candidates = []
    for candidate in (final_url, url):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    # 1) Normal Facebook extractor on the redirected URL first, then the
    # original share URL, including the Chrome-session retry.
    for candidate in candidates:
        try:
            return resolve_video(candidate)
        except Exception as exc:
            errors.append(str(exc) or "Facebook extractor failed")

    # 2) Force yt-dlp generic extractor on both candidates. This can succeed
    # when Facebook's site-specific parser fails but og:video/direct media
    # metadata is present.
    for candidate in candidates:
        for use_browser_session in (False, True):
            try:
                options = get_facebook_ytdl_opts(use_browser_session)
                options["force_generic_extractor"] = True
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(candidate, download=False)
                if isinstance(info, dict) and (info.get("url") or info.get("formats")):
                    return info
            except Exception as exc:
                errors.append(str(exc) or "Facebook generic extractor failed")

    # 3) gallery-dl has a maintained Facebook extractor and can load the
    # user's existing Chrome session. Use it before raw HTML parsing.
    try:
        return resolve_facebook_with_gallery_dl(url)
    except Exception as exc:
        errors.append(str(exc) or "gallery-dl Facebook fallback failed")

    # 4) Public HTML metadata fallback.
    try:
        html, final_url = fetch_facebook_html(url)
        media_urls = extract_facebook_video_urls(html)
        if media_urls:
            return {
                "id": "facebook_public",
                "title": "Facebook Video",
                "url": media_urls[0],
                "thumbnail": "",
                "webpage_url": final_url or url,
                "_mydownloader_direct": True,
            }
    except Exception as exc:
        errors.append(str(exc) or "Facebook public HTML fallback failed")

    raise RuntimeError("; ".join(errors[-3:]) or "Facebook media could not be resolved.")


# ============================================================
# INSTAGRAM PUBLIC PHOTO METADATA FALLBACK
# ============================================================

def fetch_public_html(url):
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
        },
        timeout=20,
        allow_redirects=True,
    )

    response.raise_for_status()
    return response.text, response.url


def decode_html_value(value):
    if not value:
        return ""

    value = value.replace("&amp;", "&")
    value = value.replace("&quot;", '"')
    value = value.replace("&#x27;", "'")
    value = value.replace("&#39;", "'")

    return value


def extract_meta_value(html, property_name=None, name=None):
    if property_name:
        pattern = (
            r'<meta[^>]+property=["\']'
            + re.escape(property_name)
            + r'["\'][^>]+content=["\']([^"\']+)'
        )
    else:
        pattern = (
            r'<meta[^>]+name=["\']'
            + re.escape(name)
            + r'["\'][^>]+content=["\']([^"\']+)'
        )

    match = re.search(
        pattern,
        html,
        flags=re.IGNORECASE,
    )

    if not match:
        return ""

    return decode_html_value(match.group(1))


def clean_media_url(value):
    if not value:
        return ""
    value = str(value).strip()
    value = (value.replace("&amp;", "&")
             .replace("\\/", "/")
             .replace("\\u0026", "&")
             .replace("\\u003D", "=")
             .replace("\\u002F", "/"))
    return value.strip("\"'")


def is_instagram_media_url(url):
    """Return True only for likely Instagram image/video CDN URLs.

    Instagram pages also contain hundreds of static.cdninstagram.com
    JavaScript/CSS asset URLs. Those must never be reported as media.
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()

        if parsed.scheme not in ("http", "https"):
            return False

        # Never treat Instagram's static page assets as media.
        if host == "static.cdninstagram.com" or path.startswith("/rsrc.php"):
            return False

        # Never accept JavaScript/CSS/font/document assets.
        if path.endswith((".js", ".css", ".map", ".woff", ".woff2", ".html")):
            return False

        # Public Instagram media is normally served from these CDN families.
        if "fbcdn.net" in host or "cdninstagram.com" in host:
            return True

        return False
    except Exception:
        return False


def add_unique_url(collection, value):
    value = clean_media_url(value)
    if (value and value.startswith(("http://", "https://"))
            and value not in collection):
        collection.append(value)


def extract_meta_images(soup):
    images = []
    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        content = (tag.get("content") or "").strip()
        if prop in ("og:image", "og:image:url", "og:image:secure_url",
                    "twitter:image", "twitter:image:src"):
            if is_instagram_media_url(content):
                add_unique_url(images, content)
    return images


def collect_images_from_json(value, results):
    if isinstance(value, dict):
        for key, child in value.items():
            key_lower = str(key).lower()
            if key_lower in ("image", "images", "contenturl", "content_url",
                             "thumbnailurl", "thumbnail_url", "url", "src",
                             "display_url"):
                if isinstance(child, str):
                    if is_instagram_media_url(child):
                        add_unique_url(results, child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, str):
                            if is_instagram_media_url(item):
                                add_unique_url(results, item)
                        else:
                            collect_images_from_json(item, results)
                else:
                    collect_images_from_json(child, results)
            else:
                collect_images_from_json(child, results)
    elif isinstance(value, list):
        for item in value:
            collect_images_from_json(item, results)


def extract_json_ld_images(soup):
    images = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            collect_images_from_json(json.loads(raw), images)
        except Exception:
            try:
                collect_images_from_json(json.loads(raw.replace("\\/", "/")), images)
            except Exception:
                pass
    return images



def extract_instagram_carousel_images(html):
    """Extract photo URLs from Instagram carousel structures embedded in public HTML.

    Handles nested carousel_media/image_versions2/candidates structures and
    safely ignores None, malformed objects, profile pictures and video URLs.
    """
    results = []

    def add_candidate(value):
        if not isinstance(value, str):
            return
        value = clean_media_url(value)
        if not is_instagram_media_url(value):
            return
        lower = value.lower()
        # Avoid obvious video CDN assets.
        if any(token in lower for token in (".mp4", "video", ".m3u8")):
            return
        add_unique_url(results, value)

    def walk(value, in_carousel=False):
        if value is None:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_lower = str(key).lower()

                if key_lower in ("carousel_media", "carousel_items", "children", "items"):
                    if isinstance(child, list):
                        for item in child:
                            walk(item, True)
                    elif isinstance(child, dict):
                        walk(child, True)
                    continue

                if key_lower in ("image_versions2", "image_versions", "candidates"):
                    walk(child, in_carousel)
                    continue

                if key_lower in ("url", "src", "display_url", "contenturl", "content_url",
                                 "thumbnail_url", "thumbnailurl"):
                    if in_carousel or key_lower in ("display_url", "contenturl", "content_url"):
                        add_candidate(child)
                    continue

                walk(child, in_carousel)

        elif isinstance(value, list):
            for item in value:
                walk(item, in_carousel)

    # First try JSON-bearing script tags.
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or "carousel_media" not in raw:
            continue

        candidates = [raw]
        cleaned = raw.replace("\\/", "/").replace("\\u0026", "&")
        if cleaned != raw:
            candidates.append(cleaned)

        for text_blob in candidates:
            try:
                walk(json.loads(text_blob), True)
            except Exception:
                pass

    # Instagram sometimes embeds escaped JS objects that are not standalone JSON.
    # Target only image_versions2/candidates URL pairs so unrelated page assets
    # are not returned.
    patterns = [
        r'"image_versions2"\s*:\s*\{.*?"candidates"\s*:\s*\[(.*?)\]',
        r'"carousel_media"\s*:\s*\[(.*?)\]\s*,\s*"(?:caption|comment_count|like_count)',
    ]
    for pattern in patterns:
        for block in re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL):
            for match in re.findall(r'"url"\s*:\s*"((?:\\.|[^"\\])+)"', block):
                add_candidate(match)

    return results


def extract_instagram_carousel_items(html):
    """Return normalized carousel photo items with stable numbering."""
    urls = extract_instagram_carousel_images(html)
    return [
        {
            "index": index,
            "media_type": "image",
            "url": url,
        }
        for index, url in enumerate(urls, 1)
    ]


def extract_structured_images(html):
    images = []
    patterns = [
        r'"(?:image|images|display_url|thumbnail_url|contentUrl|src)"\s*:\s*"([^"]+)"',
        r"'(?:image|images|display_url|thumbnail_url|contentUrl|src)'\s*:\s*'([^']+)'",
    ]
    for pattern in patterns:
        for value in re.findall(pattern, html, flags=re.IGNORECASE):
            value = clean_media_url(value)
            if is_instagram_media_url(value):
                add_unique_url(images, value)
    return images


def _best_entry_image(entry):
    """Return the best image-like URL exposed by a yt-dlp Instagram entry."""
    if not isinstance(entry, dict):
        return ""

    candidates = []

    # Thumbnail is commonly the actual CDN image for photo entries.
    thumb = entry.get("thumbnail")
    if isinstance(thumb, str):
        candidates.append(thumb)

    # Some extractors expose thumbnails as a list.
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list):
        for item in thumbs:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                candidates.append(item["url"])

    # Some extractor versions expose the image URL directly.
    for key in ("display_url", "image", "image_url"):
        value = entry.get(key)
        if isinstance(value, str):
            candidates.append(value)

    for value in candidates:
        value = clean_media_url(value)
        if value.startswith(("http://", "https://")):
            return value

    return ""


def extract_instagram_with_ytdlp(url):
    """
    Ask yt-dlp for the public Instagram post metadata first.

    This is intentionally metadata-only: no browser cookies, credentials,
    login-wall bypass, or access-control bypass.
    """
    options = get_common_ytdl_opts()
    options.update({
        "skip_download": True,
        "noplaylist": False,
    })

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        return None

    entries = info.get("entries")
    if isinstance(entries, list):
        clean_entries = [e for e in entries if isinstance(e, dict)]
    else:
        clean_entries = []

    # A carousel/post may be represented as a playlist with multiple entries.
    if clean_entries:
        items = []
        for index, entry in enumerate(clean_entries, 1):
            image_url = _best_entry_image(entry)

            # Video entries can expose a thumbnail while photo entries generally
            # expose only the thumbnail. We still return the image URL here as
            # the carousel preview/media image.
            if image_url:
                items.append({
                    "index": index,
                    "media_type": "video" if (
                        entry.get("duration") is not None
                        or entry.get("vcodec") not in (None, "none")
                        or entry.get("formats")
                    ) else "image",
                    "url": image_url,
                    "thumbnail": image_url,
                    "webpage_url": entry.get("webpage_url") or url,
                })

        if items:
            image_urls = []
            for item in items:
                add_unique_url(image_urls, item["url"])

            if image_urls:
                return {
                    "media_type": "carousel" if len(image_urls) > 1 else "image",
                    "media_url": image_urls[0],
                    "media_urls": image_urls,
                    "media_items": items,
                    "thumbnail": image_urls[0],
                    "title": info.get("title") or "Instagram Photo",
                    "description": info.get("description") or "",
                    "webpage_url": info.get("webpage_url") or url,
                    "count": len(image_urls),
                    "source": "yt-dlp",
                }

    # Single photo/post.
    image_url = _best_entry_image(info)
    if image_url:
        return {
            "media_type": "image",
            "media_url": image_url,
            "media_urls": [image_url],
            "media_items": [{
                "index": 1,
                "media_type": "image",
                "url": image_url,
                "thumbnail": image_url,
                "webpage_url": info.get("webpage_url") or url,
            }],
            "thumbnail": image_url,
            "title": info.get("title") or "Instagram Photo",
            "description": info.get("description") or "",
            "webpage_url": info.get("webpage_url") or url,
            "count": 1,
            "source": "yt-dlp",
        }

    return None



def normalize_instagram_post_url(url):
    """Remove Instagram tracking/query parameters while preserving the post URL."""
    try:
        parsed = urlparse(normalize_url(url))
        path = parsed.path or "/"
        match = re.search(r"/(p|reel|tv)/([^/]+)/?", path, flags=re.IGNORECASE)
        if match:
            return f"https://www.instagram.com/{match.group(1)}/{match.group(2)}/"
        return f"https://www.instagram.com{path}"
    except Exception:
        return normalize_url(url)


def extract_instagram_with_gallery_dl(url):
    """Extract media URLs from a public Instagram page without login cookies.

    Cloud-safe: does not read local browser sessions.
    """
    clean_url = normalize_instagram_post_url(url)

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "-g",
        clean_url,
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Python environment could not start gallery-dl.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("gallery-dl timed out while reading Instagram media.") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    media_urls = []
    for line in stdout.splitlines():
        value = clean_media_url(line.strip())
        if not value.startswith(("http://", "https://")):
            continue
        if is_instagram_media_url(value):
            lower = value.lower()
            if any(token in lower for token in (".mp4", ".m3u8", "/video/")):
                continue
            add_unique_url(media_urls, value)

    if not media_urls:
        combined = (stderr + "\n" + stdout).strip()
        if "login page" in combined.lower():
            raise RuntimeError(
                "Instagram redirected gallery-dl to the login page. "
                "Make sure Chrome is logged in to Instagram."
            )
        if completed.returncode != 0:
            last_error = next(
                (line.strip() for line in reversed(stderr.splitlines()) if line.strip()),
                "gallery-dl could not extract Instagram media.",
            )
            raise RuntimeError(last_error)
        raise RuntimeError("gallery-dl returned no Instagram image URLs.")

    return {
        "media_type": "carousel" if len(media_urls) > 1 else "image",
        "media_url": media_urls[0],
        "media_urls": media_urls,
        "media_items": [
            {
                "index": index,
                "media_type": "image",
                "url": media_url,
            }
            for index, media_url in enumerate(media_urls, 1)
        ],
        "thumbnail": media_urls[0],
        "title": "Instagram Carousel" if len(media_urls) > 1 else "Instagram Photo",
        "description": "",
        "webpage_url": clean_url,
        "count": len(media_urls),
        "source": "gallery-dl",
    }


def download_image_url(image_url, temp_dir, index=1, retries=3):
    """Download one resolved Instagram image with retry/backoff and basic validation."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Referer": "https://www.instagram.com/",
    }
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                image_url,
                headers=headers,
                timeout=(15, 45),
                stream=True,
            )
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "image/jpeg")
            content_type_clean = content_type.split(";", 1)[0].strip().lower()
            suffix = ".jpg"
            if "png" in content_type_clean:
                suffix = ".png"
            elif "webp" in content_type_clean:
                suffix = ".webp"
            elif "gif" in content_type_clean:
                suffix = ".gif"

            image_path = os.path.join(temp_dir, f"instagram_{index:02d}{suffix}")
            total = 0
            with open(image_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        output.write(chunk)
                        total += len(chunk)

            if total < 256:
                raise RuntimeError("Instagram returned an unexpectedly small image response.")

            return image_path, content_type_clean
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.8 * attempt)

    raise RuntimeError(f"Image {index} download failed after {retries} attempts: {last_error}")


def download_carousel_images(media_urls, indices, temp_dir, max_workers=4):
    """Download selected carousel images concurrently while preserving numbering."""
    indices = sorted(set(int(i) for i in indices))
    if not indices:
        return []

    results = {}
    failures = []
    workers = max(1, min(max_workers, len(indices)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(download_image_url, media_urls[index - 1], temp_dir, index): index
            for index in indices
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                failures.append(f"Image {index}: {exc}")

    if failures:
        for path, _ in results.values():
            try:
                os.remove(path)
            except OSError:
                pass
        raise RuntimeError("; ".join(failures))

    return [results[index][0] for index in indices]


def extract_instagram_photo_metadata(url):
    """Best-effort public HTML/structured-data image extraction.
    Does not use browser cookies, login credentials, or access-control bypasses.
    """
    html, final_url = fetch_public_html(url)
    soup = BeautifulSoup(html, "html.parser")

    og_title = extract_meta_value(html, property_name="og:title")
    og_description = extract_meta_value(html, property_name="og:description")

    # Carousel-specific extraction first. This prevents the generic metadata
    # fallback from collapsing a multi-photo post into only og:image.
    image_urls = extract_instagram_carousel_images(html)

    for image in extract_meta_images(soup):
        add_unique_url(image_urls, image)
    for image in extract_json_ld_images(soup):
        add_unique_url(image_urls, image)
    for image in extract_structured_images(html):
        add_unique_url(image_urls, image)

    image_urls = [
        x for x in image_urls
        if "profile_pic" not in x.lower()
        and "profilepic" not in x.lower()
        and "avatar" not in x.lower()
    ]

    if not image_urls:
        raise RuntimeError("Public Instagram page did not expose an accessible image URL.")

    media_type = "carousel" if len(image_urls) > 1 else "image"

    return {
        "media_type": media_type,
        "media_url": image_urls[0],
        "media_urls": image_urls,
        "media_items": [
            {
                "index": index,
                "media_type": "image",
                "url": image_url,
            }
            for index, image_url in enumerate(image_urls, 1)
        ],
        "thumbnail": image_urls[0],
        "title": og_title or "Instagram Photo",
        "description": og_description or "",
        "webpage_url": final_url or url,
        "count": len(image_urls),
    }


# ============================================================
# YT-DLP VIDEO RESOLUTION
# ============================================================

def instagram_story_highlight_urls(url, mode):
    """Resolve public/accessible Instagram stories or highlights via gallery-dl.

    gallery-dl supports Instagram profile subcategories such as stories and
    highlights. We use the user's existing browser session only as an optional
    access mechanism; the app never asks for or stores Instagram credentials.
    """
    mode = str(mode or "").lower().strip()
    if mode not in ("stories", "highlights"):
        raise RuntimeError("Unsupported Instagram collection type.")

    if not _gallery_dl_available():
        raise RuntimeError("gallery-dl is not installed in this virtual environment.")

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "-g",
        "--no-input",
        "--cookies-from-browser",
        "chrome",
        "-o",
        f"extractor.instagram.include={mode}",
        "-o",
        "extractor.instagram.previews=true",
        "-o",
        "extractor.instagram.videos=true",
        url,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Instagram took too long to resolve the stories/highlights.")
    except Exception as exc:
        raise RuntimeError(str(exc) or "Could not start gallery-dl.")

    urls = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(("http://", "https://")) and line not in urls:
            urls.append(line)

    if not urls:
        detail = (result.stderr or "").strip().splitlines()
        message = detail[-1] if detail else "No accessible Instagram media was found."
        raise RuntimeError(message)

    return urls


def _gallery_dl_available():
    try:
        import gallery_dl  # noqa: F401
        return True
    except Exception:
        return False


def instagram_collection_items(url, mode):
    urls = instagram_story_highlight_urls(url, mode)
    items = []
    for index, media_url in enumerate(urls, 1):
        lower = media_url.lower().split("?", 1)[0]
        media_type = "video" if lower.endswith((".mp4", ".m4v", ".mov", ".webm")) else "image"
        items.append({
            "index": index,
            "media_type": media_type,
            "url": media_url,
        })
    return items


def detect_instagram_media_kind(url):
    """Detect the Instagram media type directly from the supplied URL.

    This is intentionally URL-based for unambiguous Instagram media URLs:
    /stories/highlights/... -> highlight
    /stories/<username>/... -> story
    /reel/... or /reels/... -> reel/video
    /p/... or /tv/... -> post/photo-or-carousel (resolved by gallery-dl)
    """
    path = (urlparse(normalize_url(url)).path or "").lower().rstrip("/")
    parts = [part for part in path.split("/") if part]

    if not parts:
        return "profile"

    if parts[0] == "stories":
        if len(parts) >= 2 and parts[1] == "highlights":
            return "highlights"
        return "stories"

    if parts[0] in ("reel", "reels", "tv"):
        return "video"

    if parts[0] in ("p", "posts"):
        return "post"

    return "profile"




def download_urls_to_archive(urls, prefix):
    """Download direct Instagram media URLs and return a temporary file path."""
    temp_dir = tempfile.mkdtemp(prefix="mydownloader_instagram_collection_")
    paths = []
    try:
        for index, media_url in enumerate(urls, 1):
            response = requests.get(
                media_url,
                headers={"User-Agent": USER_AGENT},
                timeout=60,
                stream=True,
            )
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            lower = media_url.lower().split("?", 1)[0]
            if "video" in content_type or lower.endswith((".mp4", ".m4v", ".mov", ".webm")):
                ext = ".mp4"
            elif "png" in content_type or lower.endswith(".png"):
                ext = ".png"
            elif "webp" in content_type or lower.endswith(".webp"):
                ext = ".webp"
            else:
                ext = ".jpg"
            path = os.path.join(temp_dir, f"{prefix}_{index:02d}{ext}")
            with open(path, "wb") as output:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        output.write(chunk)
            paths.append(path)

        if len(paths) == 1:
            return paths[0], temp_dir, False

        archive_path = os.path.join(temp_dir, f"{prefix}.zip")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, arcname=os.path.basename(path))
        return archive_path, temp_dir, True
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def resolve_video(url):
    platform = detect_platform(url)

    if platform == "instagram":
        option_sets = [get_instagram_ytdl_opts()]
    elif platform == "facebook":
        # Facebook share/reel URLs are currently prone to "Cannot parse data"
        # even on recent yt-dlp versions. Retry with the logged-in Chrome session
        # and browser impersonation when the public request fails.
        option_sets = [
            get_facebook_ytdl_opts(False),
            get_facebook_ytdl_opts(True),
        ]
    else:
        option_sets = [get_common_ytdl_opts()]

    errors = []
    for options in option_sets:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                return info
            errors.append("No media information returned.")
        except Exception as exc:
            errors.append(str(exc) or "Unknown extractor error")

    raise RuntimeError("; ".join(errors[-2:]) or "No media information returned.")


def choose_download_options(url, output_template, use_browser_session=False):
    platform = detect_platform(url)

    if platform == "instagram":
        options = get_instagram_ytdl_opts()
    elif platform == "facebook":
        options = get_facebook_ytdl_opts(use_browser_session)
    else:
        options = get_common_ytdl_opts()

    options.update(
        {
            "outtmpl": output_template,
            "continuedl": True,
            "retries": 5,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "socket_timeout": 45,
            "noplaylist": True,
            "concurrent_fragment_downloads": 4,
        }
    )

    ffmpeg = get_ffmpeg_path()

    if ffmpeg:
        options.update(
            {
                "format": "bv*+ba/b",
                "merge_output_format": "mp4",
            }
        )
    else:
        options.update(
            {
                "format": "best[ext=mp4]/best",
            }
        )

    return options


def find_downloaded_file(temp_dir):
    files = []

    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)

        if os.path.isfile(path):
            files.append(path)

    if not files:
        return None

    mp4 = [
        path
        for path in files
        if path.lower().endswith(".mp4")
    ]

    if mp4:
        return max(
            mp4,
            key=os.path.getsize,
        )

    return max(
        files,
        key=os.path.getsize,
    )


# ============================================================
# SAFE CAROUSEL DEBUG HELPERS
# ============================================================

def debug_carousel_children(carousel_list):
    """Debug carousel structures without crashing on None/malformed children."""
    if not isinstance(carousel_list, list):
        print("CAROUSEL: not a list ->", type(carousel_list).__name__)
        return

    print("CAROUSEL ITEMS:", len(carousel_list))

    for index, child in enumerate(carousel_list, 1):
        if not isinstance(child, dict):
            print(f"CAROUSEL {index}: SKIPPED invalid child = {child!r}")
            continue

        images = child.get("image_versions2") or {}
        if not isinstance(images, dict):
            images = {}

        candidates = images.get("candidates") or []
        if not isinstance(candidates, list):
            candidates = []

        videos = child.get("video_versions") or []
        if not isinstance(videos, list):
            videos = []

        print(
            f"CAROUSEL {index}: images={len(candidates)} videos={len(videos)}"
        )

        if candidates and isinstance(candidates[0], dict):
            print(" URL:", str(candidates[0].get("url") or "")[:250])


def _gallery_dl_available():
    try:
        import gallery_dl  # noqa: F401
        return True
    except Exception:
        return False


# ============================================================
# ROOT / HEALTH
# ============================================================

@app.route("/")
def home():
    return jsonify(
        {
            "status": "success",
            "message": "My Video Downloader API is running!",
            "version": APP_VERSION,
            "platforms": [
                "instagram",
                "facebook",
            ],
            "features": [
                "public video download",
                "public photo metadata fallback",
                "V9 smart UI and carousel controls",
                "V9.2 automatic Instagram media-type detection",
                "V9.3 download progress, speed, ETA, and cancel",
                "automatic Stories and Highlights routing",
                "Instagram single-photo extraction",
                "Instagram carousel extraction",
                "browser preview UI at /ui",
                "selected carousel image download",
                "Instagram Reel video download with Chrome session",
                "Facebook private/login-required link detection",
                "Instagram Stories download",
                "Instagram Highlights download",
                "download completion notification",
            ],
        }
    )


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "version": APP_VERSION,
            "ffmpeg": bool(get_ffmpeg_path()),
            "gallery_dl": _gallery_dl_available(),
        }
    )


# ============================================================
# SIMPLE WEB UI
# ============================================================

@app.route("/ui")
def web_ui():
    html = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Downloader {APP_VERSION}</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:radial-gradient(circle at top,#20242d 0,#101114 48%,#0b0c0f 100%);color:#f5f5f7;min-height:100vh}main{max-width:1100px;margin:0 auto;padding:34px 18px 60px}.card{background:rgba(25,27,32,.94);border:1px solid #30343d;border-radius:24px;padding:22px;box-shadow:0 20px 60px #0008;backdrop-filter:blur(14px)}.brand{display:flex;align-items:center;justify-content:space-between;gap:12px}.brand h1{margin:0;font-size:28px;letter-spacing:-.5px}.badge{font-size:12px;color:#aeb5c2;border:1px solid #363b46;border-radius:999px;padding:6px 10px}.sub{margin:8px 0 0;color:#aeb3bf}.drop{margin-top:20px;border:1px dashed #454b58;border-radius:16px;padding:12px;background:#111318;transition:.2s}.drop.drag{border-color:#fff;background:#171a20}.row{display:flex;gap:10px}.row input{flex:1;min-width:0;background:#0b0d10;border:1px solid #3a3f4a;color:#fff;border-radius:13px;padding:15px;font-size:15px;outline:none}.row input:focus{border-color:#727988}.btn{border:0;border-radius:13px;padding:13px 18px;font-weight:750;cursor:pointer;background:#fff;color:#111;transition:transform .12s,opacity .12s}.btn:hover{transform:translateY(-1px)}.btn.secondary{background:#2c3039;color:#fff}.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}.status{margin:14px 0;padding:12px 14px;border-radius:13px;background:#111318;color:#bfc5d1;display:none}.error{color:#ff9a9a}.success{color:#a8efb7}.progressWrap{display:none;margin-top:12px}.progressTop{display:flex;justify-content:space-between;gap:12px;font-size:12px;color:#aeb3bf;margin-bottom:7px}.progressStats{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:11px;color:#8f95a3;margin-top:7px}.progressActions{display:flex;justify-content:flex-end;margin-top:9px}.progressActions .btn{padding:7px 11px;font-size:12px}.bar{height:8px;border-radius:999px;background:#292d35;overflow:hidden}.bar i{display:block;height:100%;width:0%;background:#fff;border-radius:999px;transition:width .15s}.meta{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:18px}.meta strong{font-size:15px}.small{font-size:12px;color:#8f95a3}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:12px;margin-top:16px}.item{background:#111318;border:1px solid #2d3038;border-radius:15px;overflow:hidden;position:relative}.item img{display:block;width:100%;aspect-ratio:1;object-fit:cover;background:#08090b}.item .num{position:absolute;top:8px;left:8px;background:#000b;border-radius:999px;padding:4px 8px;font-size:11px}.item label{display:flex;align-items:center;gap:8px;padding:9px;font-size:13px}.item input{accent-color:#fff}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}.videoCard{margin-top:16px;padding:18px;border:1px solid #30343d;border-radius:16px;background:#111318}.empty{padding:28px 10px;text-align:center;color:#8f95a3}.footer{margin-top:16px;color:#707784;font-size:11px;text-align:center}@media(max-width:650px){main{padding:18px 10px 40px}.card{padding:16px;border-radius:18px}.brand h1{font-size:23px}.row{flex-direction:column}.btn{width:100%}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.actions .btn{width:auto;flex:1;min-width:135px}}
</style>
</head>
<body>
<main>
<div class="card">
<div class="brand"><h1>My Downloader <span class="small">v{APP_VERSION}</span></h1><button class="btn secondary" onclick="toggleSettings()">⚙ Settings</button></div>
<p>Instagram Photo / Carousel / Reel / Story / Highlight and Facebook video downloader.</p><div class="small" style="margin-top:6px">Facebook: public videos only — private/login-required links are not bypassed.</div>
<div id="drop" class="drop">
<div class="row">
<input id="url" placeholder="Paste Instagram or Facebook URL">
<button class="btn secondary" onclick="pasteUrl()">Paste</button>
<button class="btn" id="resolveBtn" onclick="resolveMedia()">Preview</button>
</div>
<div class="small" style="margin-top:9px">Tip: press Enter to preview. The link type is detected automatically.</div>
</div>
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">
<input id="filename" placeholder="Optional filename (without extension)" style="flex:1;min-width:220px;background:#0b0d10;border:1px solid #3a3f4a;color:#fff;border-radius:13px;padding:12px;font-size:14px;outline:none">
<button class="btn secondary" onclick="toggleHistory()">History</button><button class="btn secondary" onclick="resetUi()">Clear</button>
</div>
<div id="settings" style="display:none;margin-top:12px;background:#111318;border:1px solid #2d3038;border-radius:14px;padding:14px">
<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap"><b>Settings</b><button class="btn secondary" style="padding:7px 10px;font-size:12px" onclick="toggleSettings()">Close</button></div>
<div style="display:grid;gap:10px;margin-top:12px">
<label class="small">Default filename prefix<input id="prefix" style="display:block;width:100%;margin-top:6px;background:#0b0d10;border:1px solid #3a3f4a;color:#fff;border-radius:10px;padding:10px" placeholder="MyDownloader"></label>
<label style="display:flex;align-items:center;gap:9px;font-size:13px"><input id="darkMode" type="checkbox" checked onchange="applyTheme()"> Dark mode</label>
<label style="display:flex;align-items:center;gap:9px;font-size:13px"><input id="clearAfter" type="checkbox"> Clear URL after successful download</label>
</div></div>
<div id="history" style="display:none;margin-top:12px;background:#111318;border:1px solid #2d3038;border-radius:14px;padding:12px"></div>
<div id="status" class="status"></div>
<div id="progressWrap" class="progressWrap"><div class="progressTop"><span id="progressLabel">Download progress</span><span id="progressText">0%</span></div><div class="bar"><i id="progressBar"></i></div><div class="progressStats"><span id="progressBytes">0 MB / —</span><span id="progressSpeed">0 MB/s</span><span id="progressEta">ETA —</span></div><div class="progressActions"><button id="cancelBtn" class="btn secondary" style="display:none" onclick="cancelDownload()">Cancel</button></div></div>
<div id="meta" class="meta"></div>
<div id="grid" class="grid"></div>
<div id="actions" class="actions" style="display:none">
<button class="btn" onclick="downloadSelected()">Download Selected</button>
<button class="btn secondary" onclick="downloadAll()">Download All</button>
<button class="btn secondary" onclick="selectAll(true)">Select All</button>
<button class="btn secondary" onclick="selectAll(false)">Clear</button>
</div>
</div>
</main>
<script>
let current=null;
const $=id=>document.getElementById(id);
const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
function showStatus(msg,cls=''){const s=$('status');s.style.display='block';s.className='status '+cls;s.textContent=msg}
function setProgress(p,label){$('progressWrap').style.display='block';$('progressBar').style.width=Math.max(0,Math.min(100,p))+'%';$('progressText').textContent=label||Math.round(p)+'%'}
let activeDownloadController=null;
function fmtMB(n){return (n/1024/1024).toFixed(1)+' MB'}
function fmtSpeed(n){return (n/1024/1024).toFixed(2)+' MB/s'}
function fmtEta(sec){if(!Number.isFinite(sec)||sec<0||sec>86400)return 'ETA —';if(sec<60)return 'ETA '+Math.ceil(sec)+'s';const m=Math.floor(sec/60),s=Math.ceil(sec%60);return 'ETA '+m+'m '+s+'s'}
function cancelDownload(){if(activeDownloadController)activeDownloadController.abort()}
function notifyComplete(name){try{if('Notification' in window){if(Notification.permission==='granted')new Notification('My Downloader',{body:'Download complete: '+name});else if(Notification.permission==='default')Notification.requestPermission().catch(()=>{})}}catch(_){} }
function selectAll(v){document.querySelectorAll('.pick').forEach(x=>x.checked=v)}
function selectedIndices(){return [...document.querySelectorAll('.pick:checked')].map(x=>x.value)}
function toggleSettings(){const x=$('settings');x.style.display=x.style.display==='none'?'block':'none';if(x.style.display==='block'){loadSettings()}}
function loadSettings(){const s=JSON.parse(localStorage.getItem('mydownloader_settings_v92')||'{}');$('prefix').value=s.prefix||'';$('darkMode').checked=s.darkMode!==false;$('clearAfter').checked=s.clearAfter===true;applyTheme()}
function saveSettings(){localStorage.setItem('mydownloader_settings_v92',JSON.stringify({prefix:$('prefix').value.trim(),darkMode:$('darkMode').checked,clearAfter:$('clearAfter').checked}))}
function applyTheme(){const dark=$('darkMode').checked;document.documentElement.style.colorScheme=dark?'dark':'light';document.body.style.background=dark?'radial-gradient(circle at top,#20242d 0,#101114 48%,#0b0c0f 100%)':'#f4f5f7';document.body.style.color=dark?'#f5f5f7':'#16181d';saveSettings()}
function customName(fallback){const p=($('prefix').value||'').trim();return p?p+'_'+fallback:fallback}
loadSettings();
function resetUi(){if(activeDownloadController)activeDownloadController.abort();$('url').value='';$('filename').value='';current=null;$('grid').innerHTML='';$('meta').innerHTML='';$('actions').style.display='none';$('progressWrap').style.display='none';$('cancelBtn').style.display='none';$('status').style.display='none';$('resolveBtn').disabled=false}
async function pasteUrl(){try{const t=await navigator.clipboard.readText();if(t){$('url').value=t;showStatus('URL pasted.','success');}else showStatus('Clipboard is empty.','error')}catch(_){showStatus('Clipboard access is unavailable. Paste the URL manually.','error')}}
async function resolveMedia(){
 const url=$('url').value.trim();
 if(!url){showStatus('Paste a URL first.','error');return}
 $('resolveBtn').disabled=true;$('grid').innerHTML='';$('actions').style.display='none';$('meta').innerHTML='';$('progressWrap').style.display='none';
 showStatus('Detecting media type…');
 try{
  const r=await fetch('/api/resolve?url='+encodeURIComponent(url));
  const d=await r.json();
  if(!r.ok||d.status!=='success')throw new Error(d.message||'Could not resolve media');
  current=d;
  const items=d.media_items||((d.media_urls||[]).map((u,i)=>({index:i+1,media_type:d.media_type||'image',url:u})));
  const kindLabel=d.media_type==='highlights'?'Instagram Highlight':
                  d.media_type==='stories'?'Instagram Story':
                  d.media_type==='video'?(d.platform==='facebook'?'Facebook Video':'Instagram Reel/Video'):
                  d.media_type==='carousel'?'Instagram Carousel':
                  d.media_type==='image'?'Instagram Photo':
                  d.media_type||'Media';
  $('meta').innerHTML='<strong>'+esc(kindLabel)+'</strong><span class="small">'+(d.count||items.length)+' item(s) · '+esc(d.source||d.platform||'')+'</span>';
  if(d.media_type==='video'){
    $('grid').innerHTML='<div class="videoCard"><b>'+esc(kindLabel)+' ready</b><p class="small">Click Download Video to save it.</p></div>';
    $('actions').style.display='flex';
    $('actions').innerHTML='<button class="btn" onclick="downloadAll()">Download Video</button>';
    showStatus('Detected automatically: '+kindLabel+'.','success');
    return;
  }
  $('grid').innerHTML=items.map(it=>'<div class="item"><span class="num">#'+it.index+'</span><img loading="lazy" src="'+esc(it.url)+'" onerror="this.style.opacity=.25"><label><input class="pick" type="checkbox" value="'+it.index+'" checked> Select '+esc(d.media_type==='highlights'?'highlight':d.media_type==='stories'?'story':'image')+' '+it.index+'</label><button class="btn secondary" style="margin:0 9px 9px;width:calc(100% - 18px);padding:9px 10px;font-size:12px" onclick="'+(d.media_type==='stories'||d.media_type==='highlights'?'downloadOneCollection':'downloadOneImage')+'('+it.index+')">Download this</button></div>').join('');
  $('actions').style.display='flex';
  if(d.media_type==='stories'||d.media_type==='highlights'){
    $('actions').innerHTML='<button class="btn" onclick="downloadSelectedCollection()">Download Selected</button><button class="btn secondary" onclick="downloadAllCollection()">Download All</button><button class="btn secondary" onclick="selectAll(true)">Select All</button><button class="btn secondary" onclick="selectAll(false)">Clear</button>';
  }else{
    $('actions').innerHTML='<button class="btn" onclick="downloadSelected()">Download Selected</button><button class="btn secondary" onclick="downloadAll()">Download All</button><button class="btn secondary" onclick="selectAll(true)">Select All</button><button class="btn secondary" onclick="selectAll(false)">Clear</button>';
  }
  showStatus('Detected automatically: '+kindLabel+' · '+items.length+' item(s).','success');
 }catch(e){showStatus(e.message+' — you can retry.','error')}finally{$('resolveBtn').disabled=false}
}
function downloadOneCollection(index){if(!current)return;const url=$('url').value.trim();const kind=current.media_type;const item=(current.media_items||[]).find(x=>String(x.index)===String(index));const ext=item&&item.media_type==='video'?'.mp4':'.jpg';return downloadRequest('/api/download?collection='+encodeURIComponent(kind)+'&url='+encodeURIComponent(url)+'&indices='+encodeURIComponent(String(index)),customName('instagram_'+kind+'_'+String(index).padStart(2,'0'))+ext)}
function downloadAllCollection(){if(!current)return;const url=$('url').value.trim();const kind=current.media_type;return downloadRequest('/api/download?collection='+encodeURIComponent(kind)+'&url='+encodeURIComponent(url),customName('instagram_'+kind)+'.zip')}
function downloadSelectedCollection(){if(!current)return;const picks=selectedIndices();if(!picks.length){showStatus('Select at least one item.','error');return}const url=$('url').value.trim();const kind=current.media_type;if(picks.length===1)return downloadOneCollection(picks[0]);return downloadRequest('/api/download?collection='+encodeURIComponent(kind)+'&url='+encodeURIComponent(url)+'&indices='+encodeURIComponent(picks.join(',')),customName('instagram_'+kind+'_selected')+'.zip')}
function saveHistory(entry){
 const key='mydownloader_history_v92';
 let h=[]; try{h=JSON.parse(localStorage.getItem(key)||'[]')}catch(_){h=[]}
 h.unshift({...entry,time:new Date().toLocaleString()});
 h=h.slice(0,20); localStorage.setItem(key,JSON.stringify(h));
}
function renderHistory(){
 const box=$('history'); let h=[]; try{h=JSON.parse(localStorage.getItem('mydownloader_history_v92')||'[]')}catch(_){h=[]}
 if(!h.length){box.innerHTML='<div class="small">No download history yet.</div>';return}
 box.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><b>Recent downloads</b><button class="btn secondary" style="padding:7px 10px;font-size:12px" onclick="clearHistory()">Clear</button></div>'+h.map(x=>'<div style="padding:8px 0;border-top:1px solid #252932"><div style="font-size:13px;word-break:break-all">'+esc(x.name)+'</div><div class="small">'+esc(x.type)+' · '+esc(x.time)+'</div></div>').join('')
}
function toggleHistory(){const box=$('history');box.style.display=box.style.display==='none'?'block':'none';if(box.style.display==='block')renderHistory()}
function clearHistory(){localStorage.removeItem('mydownloader_history_v92');renderHistory()}
async function downloadRequest(url, filename){
 $('resolveBtn').disabled=true;$('cancelBtn').style.display='inline-block';showStatus('Preparing download…');setProgress(0,'Connecting…');
 $('progressBytes').textContent='0 MB / —';$('progressSpeed').textContent='0 MB/s';$('progressEta').textContent='ETA —';
 activeDownloadController=new AbortController();const started=performance.now();
 try{
  const r=await fetch(url,{signal:activeDownloadController.signal});
  if(!r.ok){let msg='Download failed';try{const d=await r.json();msg=d.message||msg}catch(_){}throw new Error(msg)}
  const total=Number(r.headers.get('content-length')||0);let loaded=0;const reader=r.body?.getReader();const chunks=[];
  if(reader){while(true){const {done,value}=await reader.read();if(done)break;chunks.push(value);loaded+=value.byteLength;const elapsed=Math.max(.001,(performance.now()-started)/1000);const speed=loaded/elapsed;const pct=total?loaded/total*100:Math.min(95,loaded/1024/1024);const eta=total&&speed>0?(total-loaded)/speed:NaN;setProgress(pct,total?Math.round(pct)+'%':'Downloading…');$('progressBytes').textContent=total?fmtMB(loaded)+' / '+fmtMB(total):fmtMB(loaded)+' / —';$('progressSpeed').textContent=fmtSpeed(speed);$('progressEta').textContent=fmtEta(eta)}}
  else{chunks.push(await r.blob());loaded=chunks[0].size||0}
  const blob=chunks.length===1&&chunks[0] instanceof Blob?chunks[0]:new Blob(chunks);const finalName=filename||'download';const objectUrl=URL.createObjectURL(blob);const a=document.createElement('a');a.href=objectUrl;a.download=finalName;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(objectUrl),2000);
  $('progressBytes').textContent=total?fmtMB(total)+' / '+fmtMB(total):fmtMB(loaded)+' / —';$('progressSpeed').textContent=fmtSpeed(loaded/Math.max(.001,(performance.now()-started)/1000));$('progressEta').textContent='ETA 0s';setProgress(100,'Complete');showStatus('Download complete.','success');saveHistory({name:finalName,type:current?.media_type||'media'});notifyComplete(finalName);
  if($('clearAfter').checked){$('url').value='';current=null;$('grid').innerHTML='';$('actions').style.display='none'}
 }catch(e){if(e.name==='AbortError'){showStatus('Download cancelled.','error');setProgress(0,'Cancelled')}else showStatus(e.message,'error')}
 finally{activeDownloadController=null;$('cancelBtn').style.display='none';$('resolveBtn').disabled=false}
}
function customName(fallback){const v=$('filename').value.trim().replace(/[\/:*?"<>|]+/g,'_');if(v)return v;const p=($('prefix').value||'').trim().replace(/[\/:*?"<>|]+/g,'_');return p?p+'_'+fallback:fallback}
function downloadOneImage(index){if(!current)return;const url=$('url').value.trim();const base=current.media_type==='image'?'instagram_photo':'instagram_'+String(index).padStart(2,'0');return downloadRequest('/api/download?url='+encodeURIComponent(url)+'&indices='+encodeURIComponent(String(index)),customName(base)+'.jpg')}
function downloadAll(){if(!current)return;const url=$('url').value.trim();if(current.media_type==='video'){const base=current.platform==='facebook'?'facebook_video':'instagram_video';return downloadRequest('/api/download?url='+encodeURIComponent(url),customName(base)+'.mp4')}const base=current.media_type==='image'?'instagram_photo':'instagram_carousel';return downloadRequest('/api/download?url='+encodeURIComponent(url),customName(base)+(current.media_type==='image'?'.jpg':'.zip'))}
function downloadSelected(){if(!current)return;const picks=selectedIndices();if(!picks.length){showStatus('Select at least one image.','error');return}const url=$('url').value.trim();if(picks.length===1)return downloadRequest('/api/download?url='+encodeURIComponent(url)+'&indices='+encodeURIComponent(picks.join(',')),customName(current.media_type==='image'?'instagram_photo':'instagram_'+String(picks[0]).padStart(2,'0'))+'.jpg');return downloadRequest('/api/download?url='+encodeURIComponent(url)+'&indices='+encodeURIComponent(picks.join(',')),customName('instagram_selected')+'.zip')}
$('url').addEventListener('keydown',e=>{if(e.key==='Enter')resolveMedia()});
const drop=$('drop');drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('drag')});drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('drag');const t=e.dataTransfer.getData('text/plain');if(t){$('url').value=t;resolveMedia()}});
</script>
</body></html>"""
    html = html.replace("{APP_VERSION}", APP_VERSION)
    return html


# ============================================================
# RESOLVE
# ============================================================

@app.route("/api/resolve", methods=["GET", "POST"])
def resolve():
    if request.method == "GET":
        url = normalize_url(
            request.args.get("url", "")
        )
    else:
        data = request.get_json(
            silent=True
        ) or {}

        url = normalize_url(
            data.get("url", "")
        )

    if not url:
        return jsonify(
            {
                "status": "error",
                "message": "URL is required",
            }
        ), 400

    if not validate_url(url):
        return jsonify(
            {
                "status": "error",
                "message": (
                    "Unsupported or invalid "
                    "Instagram/Facebook URL"
                ),
            }
        ), 400

    platform = detect_platform(url)
    collection = normalize_url(request.args.get("collection", "")).lower()

    # V9.2: automatically detect Instagram Stories/Highlights from the URL.
    # An explicit collection parameter remains supported for compatibility.
    if platform == "instagram" and collection not in ("stories", "highlights"):
        detected_kind = detect_instagram_media_kind(url)
        if detected_kind in ("stories", "highlights"):
            collection = detected_kind

    if platform == "instagram" and collection in ("stories", "highlights"):
        try:
            items = instagram_collection_items(url, collection)
            return jsonify({
                "status": "success",
                "platform": "instagram",
                "media_type": collection,
                "source": "gallery-dl",
                "title": "Instagram " + collection.title(),
                "media_items": items,
                "media_urls": [item["url"] for item in items],
                "count": len(items),
                "webpage_url": url,
            })
        except Exception as exc:
            return jsonify({
                "status": "error",
                "platform": "instagram",
                "message": (
                    "Instagram " + collection + " could not be resolved. "
                    + (str(exc) or "No accessible media found.")
                ),
            }), 502

    # --------------------------------------------------------
    # Instagram:
    # First try yt-dlp for video.
    # If that fails, try public HTML photo metadata.
    # --------------------------------------------------------

    if platform == "instagram":

        # Cloud-safe public HTML first. This avoids spawning gallery-dl on
        # Render, where a slow Instagram response can exhaust the free worker.
        try:
            photo = extract_instagram_photo_metadata(url)
            return jsonify(
                {
                    "status": "success",
                    "platform": "instagram",
                    **photo,
                    "source": photo.get("source") or "public-html",
                }
            )
        except Exception:
            pass

        # If public HTML does not expose an image, try yt-dlp for a public Reel/video.
        try:
            info = resolve_video(url)
            if isinstance(info, dict) and info.get("_type") != "playlist":
                return jsonify(
                    {
                        "status": "success",
                        "platform": "instagram",
                        "media_type": "video",
                        "title": info.get("title") or "Instagram Video",
                        "thumbnail": info.get("thumbnail") or "",
                        "duration": info.get("duration"),
                        "webpage_url": info.get("webpage_url") or url,
                    }
                )
        except Exception:
            pass

        # Final fallback: public HTML metadata again (kept for compatibility).
        try:
            photo = extract_instagram_photo_metadata(url)
            return jsonify(
                {
                    "status": "success",
                    "platform": "instagram",
                    **photo,
                }
            )
        except Exception as exc:
            return jsonify(
                {
                    "status": "error",
                    "platform": "instagram",
                    "message": (
                        "Instagram media could not be resolved. "
                        + (str(exc) or "No accessible public media found.")
                    ),
                }
            ), 502

    # --------------------------------------------------------
    # Facebook video
    # --------------------------------------------------------

    try:
        info = resolve_facebook_with_fallback(url)

        return jsonify(
            {
                "status": "success",
                "platform": "facebook",
                "media_type": "video",
                "title": (
                    info.get("title")
                    or "Facebook Video"
                ),
                "thumbnail": (
                    info.get("thumbnail")
                    or ""
                ),
                "duration": info.get("duration"),
                "webpage_url": (
                    info.get("webpage_url")
                    or url
                ),
            }
        )

    except Exception as exc:
        raw_message = str(exc) or "Media could not be resolved."
        friendly = facebook_access_message(url, raw_message)
        return jsonify(
            {
                "status": "error",
                "platform": platform,
                "message": friendly or raw_message,
                "reason": "private_or_login_required" if friendly else "extractor_failed",
            }
        ), 502


# ============================================================
# DOWNLOAD
# ============================================================

@app.route("/api/download", methods=["GET", "POST"])
def download():

    if request.method == "GET":
        url = normalize_url(
            request.args.get("url", "")
        )
    else:
        data = request.get_json(
            silent=True
        ) or {}

        url = normalize_url(
            data.get("url", "")
        )

    if not url:
        return jsonify(
            {
                "status": "error",
                "message": "URL is required",
            }
        ), 400

    if not validate_url(url):
        return jsonify(
            {
                "status": "error",
                "message": (
                    "Unsupported or invalid "
                    "Instagram/Facebook URL"
                ),
            }
        ), 400

    platform = detect_platform(url)
    collection = normalize_url(request.args.get("collection", "")).lower()

    # V9.2: automatically detect Instagram Stories/Highlights from the URL.
    # An explicit collection parameter remains supported for compatibility.
    if platform == "instagram" and collection not in ("stories", "highlights"):
        detected_kind = detect_instagram_media_kind(url)
        if detected_kind in ("stories", "highlights"):
            collection = detected_kind

    if platform == "instagram" and collection in ("stories", "highlights"):
        try:
            items = instagram_collection_items(url, collection)
            media_urls = [item["url"] for item in items]
            requested_indices = request.args.get("indices")
            if requested_indices:
                selected = []
                for raw in requested_indices.split(","):
                    try:
                        value = int(raw.strip())
                    except (TypeError, ValueError):
                        continue
                    if 1 <= value <= len(media_urls) and value not in selected:
                        selected.append(value)
                if selected:
                    media_urls = [media_urls[i - 1] for i in selected]

            if not media_urls:
                raise RuntimeError("No accessible Instagram media found.")

            path, temp_dir, is_archive = download_urls_to_archive(
                media_urls, "instagram_" + collection
            )
            if is_archive:
                download_name = "instagram_" + collection + ".zip"
                mimetype = "application/zip"
            else:
                download_name = os.path.basename(path)
                mimetype = "video/mp4" if path.lower().endswith(".mp4") else "image/jpeg"

            response = send_file(
                path, as_attachment=True, download_name=download_name,
                mimetype=mimetype, conditional=False,
            )

            @response.call_on_close
            def cleanup_instagram_collection():
                shutil.rmtree(temp_dir, ignore_errors=True)

            return response
        except Exception as exc:
            return jsonify({
                "status": "error",
                "platform": "instagram",
                "message": (
                    "Instagram " + collection + " download failed. "
                    + (str(exc) or "No accessible media found.")
                ),
            }), 502

    # --------------------------------------------------------
    # Instagram photo fallback
    #
    # This returns a publicly exposed image URL as an image
    # response. It does not use cookies or login credentials.
    # --------------------------------------------------------

    if platform == "instagram":

        # Public HTML first. Do not spawn gallery-dl for normal post downloads
        # on Render; it can hang and get the free worker killed.
        try:
            photo = extract_instagram_photo_metadata(url)
            media_urls = photo.get("media_urls") or []

            if media_urls:
                # Support either one index or a comma-separated list of indices.
                # No selection means download the complete carousel.
                requested_indices = request.args.get("indices")
                requested_index = request.args.get("index")
                selected_indices = None

                if requested_indices:
                    parsed = []
                    for raw in requested_indices.split(","):
                        try:
                            value = int(raw.strip())
                        except (TypeError, ValueError):
                            continue
                        if 1 <= value <= len(media_urls) and value not in parsed:
                            parsed.append(value)
                    selected_indices = parsed or None
                elif requested_index:
                    try:
                        value = int(requested_index)
                    except (TypeError, ValueError):
                        value = 1
                    selected_indices = [max(1, min(value, len(media_urls)))]

                temp_dir = tempfile.mkdtemp(prefix="mydownloader_photo_")

                # Exactly one selected image -> direct image download.
                if selected_indices and len(selected_indices) == 1:
                    selected_index = selected_indices[0]
                    selected_url = media_urls[selected_index - 1]
                    image_path, content_type = download_image_url(
                        selected_url, temp_dir, selected_index
                    )
                    response = send_file(
                        image_path, as_attachment=True,
                        download_name=os.path.basename(image_path),
                        mimetype=content_type, conditional=False,
                    )

                    @response.call_on_close
                    def cleanup_gallery_photo():
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    return response

                # No index requested: single photo remains a normal image.
                if not selected_indices and len(media_urls) == 1:
                    image_path, content_type = download_image_url(
                        media_urls[0],
                        temp_dir,
                        1,
                    )

                    response = send_file(
                        image_path,
                        as_attachment=True,
                        download_name=os.path.basename(image_path),
                        mimetype=content_type,
                        conditional=False,
                    )

                    @response.call_on_close
                    def cleanup_single_gallery_photo():
                        shutil.rmtree(temp_dir, ignore_errors=True)

                    return response

                image_paths = []
                indices_to_download = selected_indices or list(range(1, len(media_urls) + 1))
                image_paths = download_carousel_images(
                    media_urls,
                    indices_to_download,
                    temp_dir,
                    max_workers=4,
                )

                zip_path = os.path.join(
                    temp_dir,
                    "instagram_carousel.zip",
                )

                with zipfile.ZipFile(
                    zip_path,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                ) as archive:
                    for image_path in image_paths:
                        archive.write(
                            image_path,
                            arcname=os.path.basename(image_path),
                        )

                response = send_file(
                    zip_path,
                    as_attachment=True,
                    download_name="instagram_carousel.zip",
                    mimetype="application/zip",
                    conditional=False,
                )

                @response.call_on_close
                def cleanup_gallery_carousel():
                    shutil.rmtree(temp_dir, ignore_errors=True)

                return response
        except Exception:
            pass

        # Normal Instagram Reel/video fallback through yt-dlp.
        video_error = None
        try:
            info = resolve_video(url)
            if isinstance(info, dict) and info.get("_type") != "playlist":
                temp_dir = tempfile.mkdtemp(prefix="mydownloader_")
                try:
                    output_template = os.path.join(
                        temp_dir,
                        "%(title).150B.%(ext)s",
                    )
                    options = choose_download_options(url, output_template)
                    with yt_dlp.YoutubeDL(options) as ydl:
                        downloaded_info = ydl.extract_info(url, download=True)

                    file_path = find_downloaded_file(temp_dir)
                    if not file_path:
                        raise RuntimeError("yt-dlp completed but no video file was produced.")

                    title = (
                        downloaded_info.get("title")
                        if isinstance(downloaded_info, dict)
                        else None
                    ) or "instagram_video"

                    # Preserve the actual produced extension; yt-dlp may merge
                    # into mp4 or fall back to another supported container.
                    ext = os.path.splitext(file_path)[1].lower() or ".mp4"
                    download_name = re.sub(r'[\\/:*?"<>|]+', "_", title[:120]).strip() or "instagram_video"
                    if not download_name.lower().endswith(ext):
                        download_name += ext

                    response = send_file(
                        file_path,
                        as_attachment=True,
                        download_name=download_name,
                        mimetype="video/mp4" if ext == ".mp4" else "application/octet-stream",
                        conditional=False,
                    )

                    @response.call_on_close
                    def cleanup_video():
                        shutil.rmtree(temp_dir, ignore_errors=True)

                    return response
                except Exception:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    raise
        except Exception as exc:
            video_error = str(exc)


        # Public HTML fallback (same cloud-safe path).
        try:
            photo = extract_instagram_photo_metadata(url)
            image_path, content_type = download_image_url(
                photo["media_url"],
                tempfile.mkdtemp(prefix="mydownloader_photo_"),
                1,
            )
            temp_dir = os.path.dirname(image_path)
            response = send_file(
                image_path,
                as_attachment=True,
                download_name=os.path.basename(image_path),
                mimetype=content_type,
                conditional=False,
            )

            @response.call_on_close
            def cleanup_html_photo():
                shutil.rmtree(temp_dir, ignore_errors=True)

            return response
        except Exception as exc:
            details = str(exc) or "No accessible public media available."
            if video_error:
                details = f"{details} Video extractor: {video_error}"
            return jsonify(
                {
                    "status": "error",
                    "platform": "instagram",
                    "message": "Instagram media could not be downloaded. " + details,
                }
            ), 502

    # --------------------------------------------------------
    # Facebook video
    # --------------------------------------------------------

    temp_dir = tempfile.mkdtemp(prefix="mydownloader_")

    def cleanup_temp_files():
        for name in os.listdir(temp_dir):
            path = os.path.join(temp_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    try:
        output_template = os.path.join(temp_dir, "%(title).150B.%(ext)s")
        info = None
        download_errors = []

        # First try yt-dlp Facebook extraction with and without the user's
        # existing Chrome session.
        for use_browser_session in (False, True):
            try:
                options = choose_download_options(
                    url,
                    output_template,
                    use_browser_session=use_browser_session,
                )
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as exc:
                download_errors.append(str(exc) or "Facebook extractor error")
                cleanup_temp_files()

        # If the site-specific extractor fails, try generic/direct public media.
        if info is None:
            try:
                info = resolve_facebook_with_fallback(url)
            except Exception as exc:
                download_errors.append(str(exc) or "Facebook fallback resolution failed")
                info = None

        file_path = find_downloaded_file(temp_dir)

        # Generic/direct extraction may return a direct video URL instead of a
        # downloaded file. Stream it into the temporary directory.
        if not file_path and isinstance(info, dict):
            direct_url = info.get("url")
            if direct_url:
                direct_path = os.path.join(temp_dir, "facebook_video.mp4")
                response = requests.get(
                    direct_url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": "https://www.facebook.com/",
                    },
                    timeout=60,
                    stream=True,
                )
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "video/mp4").lower()
                if "text/html" in content_type:
                    raise RuntimeError("Facebook returned an HTML page instead of video data.")
                with open(direct_path, "wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
                file_path = direct_path

        if not file_path:
            raise RuntimeError(
                "; ".join(download_errors[-3:])
                or "Facebook download failed."
            )

        title = (
            info.get("title")
            if isinstance(info, dict)
            else None
        ) or "facebook_video"
        title = re.sub(r'[\\/:*?"<>|]+', "_", str(title)[:120]).strip() or "facebook_video"
        ext = os.path.splitext(file_path)[1].lower() or ".mp4"
        if not title.lower().endswith(ext):
            title += ext

        response = send_file(
            file_path,
            as_attachment=True,
            download_name=title,
            mimetype="video/mp4" if ext == ".mp4" else "application/octet-stream",
            conditional=False,
        )

        @response.call_on_close
        def cleanup_facebook():
            shutil.rmtree(temp_dir, ignore_errors=True)

        return response

    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raw_message = str(exc) or "Facebook download failed."
        friendly = facebook_access_message(url, raw_message)
        return jsonify(
            {
                "status": "error",
                "platform": "facebook",
                "message": friendly or raw_message,
                "reason": "private_or_login_required" if friendly else "download_failed",
            }
        ), 502


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    ffmpeg = get_ffmpeg_path()

    print()
    print("==============================================")
    print(" My Video Downloader API")
    print("==============================================")
    print(f" Version : {APP_VERSION}")
    print(
        " FFmpeg  : "
        + (ffmpeg or "Not found")
    )
    print()
    print(" Local:")
    print(" http://127.0.0.1:5050")
    print()
    print(" LAN:")
    print(" http://<Mac-LAN-IP>:5050")
    print()
    print("==============================================")
    print()

    app.run(
        host="0.0.0.0",
        port=5050,
        debug=False,
        threaded=True,
    )
