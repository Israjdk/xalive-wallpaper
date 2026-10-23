"""Wallpaper catalogs, direct downloads and ad-blocking rules for Xalive Wallpaper."""

from xalive_i18n import tr
import html
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Xalive/1.0"
VIDEO_TYPES = ("video/", "application/octet-stream", "binary/octet-stream")


@dataclass
class Item:
    title: str
    thumb: str
    page: str
    source: str
    detail: str = ""


def fetch(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.geturl()


def fetch_text(url, timeout=20):
    data, final_url = fetch(url, timeout)
    return data.decode("utf-8", "replace"), final_url


class MoeWalls:
    name = "MoeWalls"
    # The real file link is produced by the site's own page script, so the
    # download is performed by a hidden browser view (with ad blocking).
    needs_browser = True
    ARTICLE = re.compile(
        r'<article[^>]*>.*?<a title="([^"]*)" class="g1-frame" href="([^"]+)">.*?'
        r'<img[^>]* src="([^"]+)"',
        re.S,
    )

    def search(self, query, page):
        path = "https://moewalls.com/" + (f"page/{page}/" if page > 1 else "")
        if query:
            path += "?s=" + urllib.parse.quote_plus(query)
        text, _ = fetch_text(path)
        items = []
        for chunk in text.split("<article")[1:]:
            match = self.ARTICLE.search("<article" + chunk.split("</article>")[0] + "</article>")
            if match:
                title, page_url, thumb = match.groups()
                resolution = re.search(r'entry-resolutions"><a[^>]*>([^<]*)<', chunk)
                resolution = resolution.group(1) if resolution else ""
                items.append(
                    Item(html.unescape(title).replace(" Live Wallpaper", ""), thumb,
                         page_url, self.name, resolution or "")
                )
        return items

    def resolve(self, item):
        return None


class MotionBGs:
    name = "MotionBGs"
    needs_browser = False
    CARD = re.compile(
        r'<a title="([^"]*)" href=(/[^ >]+)>.*?<img [^>]*?src=([^ >]+)[^>]*>(.*?)</a>', re.S
    )

    def __init__(self):
        self.search_bases = {}

    def search(self, query, page):
        base = "https://motionbgs.com/"
        if query:
            if query not in self.search_bases:
                _text, final = fetch_text(
                    "https://motionbgs.com/search?q=" + urllib.parse.quote_plus(query)
                )
                self.search_bases[query] = final
            base = self.search_bases[query]
        url = base if page == 1 else base.rstrip("/") + f"/{page}/"
        text, _ = fetch_text(url)
        items = []
        for title, href, thumb, rest in self.CARD.findall(text):
            if href.startswith(("/page/", "/dl/", "/tag:")):
                continue
            quality = re.search(r"class=frm>\s*([^<]*?)\s*<", rest)
            items.append(
                Item(
                    html.unescape(title).replace(" live wallpaper", ""),
                    urllib.parse.urljoin("https://motionbgs.com/", thumb),
                    urllib.parse.urljoin("https://motionbgs.com/", href),
                    self.name,
                    quality.group(1) if quality else "",
                )
            )
        return items

    def resolve(self, item):
        text, _ = fetch_text(item.page)
        for quality in ("4k", "hd"):
            match = re.search(r'href=["\']?(/dl/%s/\d+/?)' % quality, text)
            if match:
                return urllib.parse.urljoin("https://motionbgs.com/", match.group(1))
        raise RuntimeError(tr("No se encontró un enlace de descarga."))


class InternetArchive:
    name = "Internet Archive"
    needs_browser = False
    ROWS = 24

    def search(self, query, page):
        terms = query or "live wallpaper"
        q = f"title:({terms}) AND mediatype:movies"
        url = (
            "https://archive.org/advancedsearch.php?"
            + urllib.parse.urlencode(
                {"q": q, "rows": self.ROWS, "page": page, "output": "json",
                 "sort[]": "downloads desc"}
            )
            + "&fl[]=identifier&fl[]=title"
        )
        text, _ = fetch_text(url)
        docs = json.loads(text)["response"]["docs"]
        return [
            Item(
                doc.get("title", doc["identifier"]),
                f"https://archive.org/services/img/{doc['identifier']}",
                f"https://archive.org/details/{doc['identifier']}",
                self.name,
            )
            for doc in docs
        ]

    def resolve(self, item):
        identifier = item.page.rsplit("/", 1)[-1]
        text, _ = fetch_text(f"https://archive.org/metadata/{identifier}")
        files = [
            f for f in json.loads(text).get("files", [])
            if f.get("source") == "original"
            and f["name"].lower().endswith((".mp4", ".webm", ".mkv", ".mov"))
        ]
        if not files:
            raise RuntimeError(tr("Este elemento no tiene un video original."))
        best = max(files, key=lambda f: int(f.get("size", 0) or 0))
        return f"https://archive.org/download/{identifier}/" + urllib.parse.quote(best["name"])


SOURCES = [MoeWalls(), MotionBGs(), InternetArchive()]


def unique_target(folder, name):
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(". ") or "wallpaper.mp4"
    target = Path(folder) / name
    counter = 1
    while target.exists():
        target = Path(folder) / f"{Path(name).stem} ({counter}){Path(name).suffix}"
        counter += 1
    return target


def download_file(url, folder, progress, cancelled, referer=None):
    """Download url unmodified into folder; returns the final path."""
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        mime = (response.headers.get_content_type() or "").lower()
        if not mime.startswith(VIDEO_TYPES):
            raise RuntimeError(tr("El enlace no devolvió un video."))
        name = response.headers.get_filename() or Path(
            urllib.parse.unquote(urllib.parse.urlparse(response.geturl()).path)
        ).name
        target = unique_target(folder, name)
        partial = target.with_name(target.name + ".part")
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        try:
            with open(partial, "wb") as out:
                while True:
                    if cancelled():
                        raise RuntimeError("Cancelado")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    progress(done, total)
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return target


# Domains of common ad and pop-under networks, blocked when loaded by a third party.
AD_DOMAINS = [
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "google-analytics.com", "googletagmanager.com",
    "adnxs.com", "taboola.com", "outbrain.com", "popads.net", "popcash.net",
    "propellerads.com", "exoclick.com", "exosrv.com", "juicyads.com",
    "adsterra.com", "highperformanceformat.com", "clickadu.com", "hilltopads.net",
    "onclickads.net", "trafficjunky.net", "trafficstars.com", "adcash.com",
    "mgid.com", "revcontent.com", "criteo.com", "criteo.net", "pubmatic.com",
    "rubiconproject.com", "openx.net", "amazon-adsystem.com", "2mdn.net",
    "adsrvr.org", "ad-maven.com", "admaven.com", "monetag.com", "a-ads.com",
    "bidvertiser.com", "yllix.com", "tsyndicate.com", "adskeeper.com",
    "adform.net", "smartadserver.com", "casalemedia.com", "advertising.com",
    "moatads.com", "scorecardresearch.com", "quantserve.com", "hotjar.com",
    "yandex.ru", "mc.yandex.ru", "pushnews.eu", "profitableratecpm.com",
    "effectivegatecpm.com", "pagead2.googlesyndication.com", "ezoic.net",
    "ezodn.com", "mediavine.com", "adthrive.com", "sharethrough.com",
    "tapad.com", "bluekai.com", "nr-data.net", "clarity.ms",
]
HIDE_SELECTORS = [
    ".adsbygoogle", "ins.adsbygoogle", "[id^='google_ads']", "[id^='div-gpt-ad']",
    "[class*='ad-banner']", "[class*='adsbox']", "[id*='ad-container']",
    ".advertisement", ".ad-slot", "iframe[src*='doubleclick']",
]


def adblock_rules_json():
    rules = []
    for domain in AD_DOMAINS:
        escaped = re.escape(domain).replace("\\-", "-")
        rules.append({
            "trigger": {
                "url-filter": r"^https?://([^/]+\.)?" + escaped + r"[:/]",
                "load-type": ["third-party"],
            },
            "action": {"type": "block"},
        })
    rules.append({
        "trigger": {"url-filter": ".*"},
        "action": {"type": "css-display-none", "selector": ", ".join(HIDE_SELECTORS)},
    })
    return json.dumps(rules)


def registrable_domain(uri):
    host = urllib.parse.urlparse(uri or "").hostname or ""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class WebGuard:
    """Stops pop-ups and page redirects to other sites that the user did not ask for."""

    def __init__(self, WebKit2, GLib):
        self.WebKit2 = WebKit2
        self.GLib = GLib
        self.trusted = ""

    def attach(self, view):
        view.connect("decide-policy", self.on_policy)
        view.connect("create", self.on_create)

    def navigate(self, view, uri):
        self.trusted = registrable_domain(uri)
        view.load_uri(uri)

    def on_policy(self, view, decision, kind):
        W = self.WebKit2
        if kind == W.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            nav = action.get_navigation_type()
            T = W.NavigationType
            if nav in (T.BACK_FORWARD, T.RELOAD) or (
                nav in (T.LINK_CLICKED, T.FORM_SUBMITTED) and action.is_user_gesture()
            ):
                self.trusted = registrable_domain(action.get_request().get_uri())
            return False
        if kind == W.PolicyDecisionType.RESPONSE:
            response = decision.get_response()
            mime = response.get_mime_type() or ""
            if mime.startswith("video/") or not decision.is_mime_type_supported():
                decision.download()
                return True
            if (
                decision.is_main_frame_main_resource()
                and mime.startswith("text/html")
                and self.trusted
                and registrable_domain(response.get_uri()) != self.trusted
            ):
                decision.ignore()
                return True
        return False

    def on_create(self, view, action):
        uri = action.get_request().get_uri()
        if registrable_domain(uri) == self.trusted and uri != view.get_uri():
            self.GLib.idle_add(view.load_uri, uri)
        elif (
            action.get_navigation_type() == self.WebKit2.NavigationType.LINK_CLICKED
            and action.is_user_gesture()
        ):
            self.GLib.idle_add(self.navigate, view, uri)
        return None
