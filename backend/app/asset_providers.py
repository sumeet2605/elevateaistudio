import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PEXELS_LICENSE = "https://www.pexels.com/license/"
MAX_RESPONSE = 26 * 1024 * 1024


class ProviderError(ValueError):
    pass


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ProviderError("The provider returned an unexpected redirect.")


def fetch(url, hosts, *, headers=None, data=None, types=("application/json",), maximum=MAX_RESPONSE):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ProviderError("The provider URL is outside the authorised HTTPS hosts.")
    try:
        with build_opener(RejectRedirect).open(Request(url, headers=headers or {}, data=data), timeout=55) as reply:
            if reply.headers.get_content_type() not in types:
                raise ProviderError("The provider returned an unexpected media type.")
            declared = reply.headers.get("Content-Length")
            if declared and declared.isdecimal() and int(declared) > maximum:
                raise ProviderError("The provider media exceeds the import limit.")
            content = reply.read(maximum + 1)
            if len(content) > maximum:
                raise ProviderError("The provider media exceeds the import limit.")
            return content
    except HTTPError as error:
        if error.code == 429:
            raise ProviderError("Provider rate limit reached. Try again later.") from error
        raise ProviderError(f"Provider request failed (HTTP {error.code}).") from error
    except (TimeoutError, URLError) as error:
        raise ProviderError("Provider connection failed or timed out.") from error


MOCK_STOCK = [
    {"id": "routine", "title": "Repeated routine", "creator": "Studio demo", "kind": "image"},
    {"id": "crossroads", "title": "Two possible directions", "creator": "Studio demo", "kind": "video"},
    {"id": "reflection", "title": "Quiet reflection", "creator": "Studio demo", "kind": "image"},
]


class MockStock:
    name = "mock"
    def search(self, query, page, kind):
        choices = [row for row in MOCK_STOCK if row["kind"] == kind]
        matches = [row for row in choices if query.casefold() in row["title"].casefold()]
        return {"provider": self.name, "items": (matches or choices) if page == 1 else [], "next_page": None}


class Pexels:
    name = "pexels"
    def __init__(self, key):
        self.key = key

    def call(self, path):
        if not self.key:
            raise ProviderError("Pexels API key is missing.")
        return json.loads(fetch("https://api.pexels.com" + path, {"api.pexels.com"}, headers={"Authorization": self.key}))

    @staticmethod
    def record(row, kind):
        author = row["user"]["name"] if kind == "video" else row["photographer"]
        page = row.get("url", "")
        if urlsplit(page).hostname not in {"www.pexels.com", "pexels.com"} or not page.startswith("https://"):
            raise ProviderError("Pexels returned an invalid source page.")
        return {"id": str(row["id"]), "title": row.get("alt") or f"Pexels {kind} {row['id']}",
                "creator": author, "source_url": page, "kind": kind, "license": PEXELS_LICENSE,
                "restrictions": "Use within an edited production. No standalone redistribution. Credit the creator and Pexels."}

    def search(self, query, page, kind):
        path = "/videos/search?" if kind == "video" else "/v1/search?"
        response = self.call(path+urlencode({"query": query, "page": page, "per_page": 12, "orientation": "portrait"}))
        return {"provider": self.name, "items": [self.record(row, kind) for row in response["videos" if kind == "video" else "photos"]],
                "next_page": page+1 if response.get("next_page") and page < 80 else None}

    def download(self, asset_id, kind, maximum):
        if not asset_id.isascii() or not asset_id.isdecimal() or len(asset_id) > 15:
            raise ProviderError("Invalid Pexels asset ID.")
        row = self.call(("/videos/videos/" if kind == "video" else "/v1/photos/") + asset_id)
        metadata = self.record(row, kind)
        if kind == "video":
            files = [item for item in row["video_files"] if item.get("file_type") == "video/mp4" and item.get("width")]
            if not files:
                raise ProviderError("No authorised MP4 variant is available.")
            url = min(files, key=lambda item: abs(item["width"] - 1080))["link"]
            return fetch(url, {"videos.pexels.com"}, types=("video/mp4",), maximum=maximum), ".mp4", metadata
        return fetch(row["src"]["large"], {"images.pexels.com"}, types=("image/jpeg",), maximum=maximum), ".jpg", metadata


class OpenAIImages:
    name = "openai"
    def __init__(self, key, model):
        self.key, self.model = key, model

    def generate(self, prompt, size, quality, maximum):
        if not self.key:
            raise ProviderError("OpenAI API key is missing.")
        payload = {"model": self.model, "prompt": prompt, "size": size, "quality": quality,
                   "n": 1, "output_format": "png"}
        response = json.loads(fetch("https://api.openai.com/v1/images/generations", {"api.openai.com"},
                                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                                    data=json.dumps(payload).encode(), maximum=min(MAX_RESPONSE, maximum*2)))
        image = base64.b64decode(response["data"][0]["b64_json"], validate=True)
        if len(image) > maximum:
            raise ProviderError("The generated image exceeds the import limit.")
        return image
