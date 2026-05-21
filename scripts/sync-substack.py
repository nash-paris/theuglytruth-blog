#!/usr/bin/env python3
"""
sync-substack.py — Synchronise Substack → blog Astro.

- Fetch les posts via l'API archive (ou le flux RSS en repli)
- Filtre : posts dont le titre contient "ugly truth", publiés a partir du
  7 mars 2024, podcasts/audio exclus
- Telecharge chaque post :
    * HTML brut → converti en Markdown
    * Images → telechargees, optimisees (WebP), stockees localement
    * Front-matter YAML avec metadonnees + enrichissement GEO
- Ecrit dans src/content/blog/<slug>.md

MODE INCREMENTAL (defaut) :
  Ne traite que les editions dont le .md n'existe pas encore. La detection
  se fait via l'existence du fichier .md (persistant dans git), ce qui rend
  le sync idempotent meme en CI ou le cache local n'est pas conserve.
  Avantage cle : sur une execution normale sans nouvelle edition, le script
  ne fait qu'1 a 2 requetes HTTP → surface de blocage minimale.

MODE COMPLET (--full) :
  Re-parcourt toute l'archive (utile pour un re-backfill).

ROBUSTESSE RESEAU :
  - Headers de navigateur realistes (contourne le filtrage par User-Agent)
  - requests.Session avec retry + backoff exponentiel
  - "Warmup" : visite la home pour recolter les cookies Cloudflare
  - Repli RSS fonctionnel (via requests, pas via l'UA par defaut de feedparser)
  - Echec BRUYANT : si 0 post n'est recupere depuis la source, le script
    sort en code non-zero (echec visible au lieu d'un echec silencieux).

Usage :
    python3 scripts/sync-substack.py                 # incremental, archive
    python3 scripts/sync-substack.py --full          # re-sync complet
    python3 scripts/sync-substack.py --source rss    # via RSS
    python3 scripts/sync-substack.py --dry-run
    python3 scripts/sync-substack.py --limit 5       # pour tester

Dependances (auto-installees si absentes) :
    feedparser, requests, beautifulsoup4, markdownify, python-slugify, pillow
"""

from __future__ import annotations
import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Bootstrap dependances
# --------------------------------------------------------------------------- #

REQUIRED = ["feedparser", "requests", "beautifulsoup4", "markdownify", "python-slugify", "Pillow", "anthropic"]

def ensure_deps():
    missing = []
    for pkg in REQUIRED:
        mod = pkg.replace("-", "_").lower()
        if mod == "beautifulsoup4":
            mod = "bs4"
        if mod == "python_slugify":
            mod = "slugify"
        if mod == "pillow":
            mod = "PIL"
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"📦 Installation : {missing}", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", *missing])

ensure_deps()

import os
import feedparser
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from slugify import slugify
from PIL import Image
from io import BytesIO

try:
    from urllib3.util.retry import Retry
except ImportError:  # tres vieux urllib3
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SUBSTACK_BASE = "https://nashsuglytruth.substack.com"
SUBSTACK_FEED = f"{SUBSTACK_BASE}/feed"
SUBSTACK_ARCHIVE_API = f"{SUBSTACK_BASE}/api/v1/archive"

CUTOFF_DATE = datetime(2024, 3, 7, tzinfo=timezone.utc)

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "src" / "content" / "blog"
IMAGES_DIR = BASE_DIR / "public" / "images" / "posts"
STATE_FILE = BASE_DIR / "scripts" / ".cache" / "sync-state.json"

MAX_IMAGE_WIDTH = 1600
JPEG_QUALITY = 82
WEBP_QUALITY = 82

# --- Couche HTTP : se faire passer pour un vrai navigateur ------------------
# Le blocage observe sur les runners GitHub Actions vient d'un filtrage
# combine User-Agent + plage d'IP datacenter. Des headers de navigateur
# complets + des cookies recoltes via un warmup eliminent le filtrage UA et
# la majorite des challenges Cloudflare "soft".
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

HTTP_TIMEOUT = 30
RETRY_TOTAL = 3
RETRY_BACKOFF = 2  # 2s, 4s, 8s

_SESSION: Optional[requests.Session] = None

def get_session() -> requests.Session:
    """Session HTTP partagee : headers navigateur, retry/backoff, cookies."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[403, 408, 429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    # Warmup : visite la home pour recolter les cookies (clearance Cloudflare).
    try:
        s.get(SUBSTACK_BASE + "/", timeout=HTTP_TIMEOUT)
        print("  🍪 Warmup OK (cookies recoltes)", flush=True)
    except Exception as e:
        print(f"  ⚠️  Warmup KO ({e}) — on continue quand meme", file=sys.stderr, flush=True)
    _SESSION = s
    return s

def polite_sleep(lo: float = 0.6, hi: float = 1.6) -> None:
    """Petite pause aleatoire entre requetes : moins agressif, moins detectable."""
    time.sleep(random.uniform(lo, hi))

# --- Phase 2 GEO : enrichissement via LLM -----------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # Modele le plus economique 2026
MAX_HTML_CHARS_FOR_LLM = 12000  # Limite input pour eviter tokens excessifs

# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #

@dataclass
class Post:
    id: str
    title: str
    subtitle: Optional[str]
    date: datetime
    url: str
    slug: str
    html: str
    cover_image_url: Optional[str] = None  # Cover officiel choisi par l'auteur

# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def should_keep(title: str) -> bool:
    """Garde uniquement les posts 'The Ugly Truth' hors podcasts/audio."""
    if not title:
        return False
    low = title.lower()
    if "podcast" in low or " audio" in low or low.endswith("audio"):
        return False
    return "ugly truth" in low

def clean_slug(raw: str, title: str) -> str:
    """Slug depuis URL Substack ou fallback titre."""
    if raw:
        return raw.strip("/")
    return slugify(title, max_length=80)

def md_already_synced(slug: str) -> bool:
    """Vrai si l'edition est deja presente sur le blog (fichier .md commite)."""
    return (CONTENT_DIR / f"{slug}.md").exists()

def count_existing_md() -> int:
    if not CONTENT_DIR.exists():
        return 0
    return len([p for p in CONTENT_DIR.glob("*.md")])

def sanitize_image_filename(url: str) -> str:
    """Nom de fichier image lisible et stable base sur hash de l'URL."""
    h = hashlib.sha1(url.encode()).hexdigest()[:10]
    ext = ".webp"
    name = url.rsplit("/", 1)[-1].split("?")[0]
    name = slugify(Path(name).stem, max_length=30) or "image"
    return f"{name}-{h}{ext}"

def download_and_optimize_image(url: str, dest: Path) -> bool:
    """Telecharge + convertit en WebP + redimensionne. Retourne True si OK."""
    if dest.exists():
        return True
    try:
        r = get_session().get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content))
        if img.mode in ("P", "RGBA"):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        if img.width > MAX_IMAGE_WIDTH:
            ratio = MAX_IMAGE_WIDTH / img.width
            img = img.resize((MAX_IMAGE_WIDTH, int(img.height * ratio)), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() == ".webp":
            img.save(dest, "WEBP", quality=WEBP_QUALITY, method=6)
        else:
            img.save(dest, quality=JPEG_QUALITY, optimize=True)
        return True
    except Exception as e:
        print(f"  ⚠️  Image KO {url}: {e}", file=sys.stderr)
        return False

# --------------------------------------------------------------------------- #
# Fetch posts
# --------------------------------------------------------------------------- #

def fetch_feed_posts(limit: Optional[int] = None, full: bool = False) -> list[Post]:
    """Repli RSS : ~20 posts les plus recents, contenu complet inclus.

    On telecharge le flux via requests (headers navigateur) PUIS on le parse,
    au lieu de laisser feedparser faire la requete avec son User-Agent par
    defaut (qui se fait bloquer).
    """
    print(f"📡 Fetch RSS : {SUBSTACK_FEED}", flush=True)
    try:
        r = get_session().get(SUBSTACK_FEED, timeout=HTTP_TIMEOUT,
                               headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        print(f"  ⚠️  RSS fetch KO : {e}", file=sys.stderr, flush=True)
        return []

    posts = []
    for entry in feed.entries:
        if not getattr(entry, "published_parsed", None):
            continue
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if published < CUTOFF_DATE:
            continue
        if not should_keep(entry.get("title", "")):
            continue
        slug_raw = entry.link.rstrip("/").rsplit("/", 1)[-1]
        slug = clean_slug(slug_raw, entry.title)
        if not full and md_already_synced(slug):
            continue
        html = entry.get("content", [{}])[0].get("value") or entry.get("summary", "")
        # Cover : Substack expose parfois media_thumbnail / media_content dans le RSS
        cover = None
        for key in ("media_thumbnail", "media_content"):
            media = entry.get(key)
            if media and isinstance(media, list) and media and media[0].get("url"):
                cover = media[0]["url"]
                break
        posts.append(Post(
            id=str(entry.get("id", entry.link)),
            title=entry.title,
            subtitle=entry.get("subtitle") or None,
            date=published,
            url=entry.link,
            slug=slug,
            html=html,
            cover_image_url=cover,
        ))
        if limit and len(posts) >= limit:
            break
    print(f"  → {len(posts)} post(s) a traiter via RSS "
          f"({'mode complet' if full else 'mode incremental'})", flush=True)
    return posts

def _fetch_archive_page(offset: int) -> Optional[list]:
    """Recupere une page de l'API archive. None = erreur reseau/HTTP/JSON."""
    url = f"{SUBSTACK_ARCHIVE_API}?sort=new&offset={offset}&limit=25"
    try:
        r = get_session().get(
            url, timeout=HTTP_TIMEOUT,
            headers={"Accept": "application/json, text/plain, */*",
                     "Referer": f"{SUBSTACK_BASE}/archive",
                     "X-Requested-With": "XMLHttpRequest"},
        )
    except Exception as e:
        print(f"  ⚠️  Erreur reseau (offset {offset}) : {e}", file=sys.stderr, flush=True)
        return None
    if not r.ok:
        print(f"  ⚠️  HTTP {r.status_code} (offset {offset})", file=sys.stderr, flush=True)
        return None
    try:
        return r.json()
    except Exception as e:
        # Page de challenge Cloudflare = HTML, pas du JSON
        print(f"  ⚠️  Reponse non-JSON (offset {offset}) : {e}", file=sys.stderr, flush=True)
        return None

def fetch_archive_posts(limit: Optional[int] = None, full: bool = False) -> list[Post]:
    """API interne /api/v1/archive.

    Incremental (defaut) : s'arrete des qu'une page ne contient aucune
    nouvelle edition (l'archive est triee du plus recent au plus ancien,
    donc tout ce qui suit est deja synchronise). Ne telecharge le HTML que
    pour les editions reellement nouvelles.

    Complet (--full) : parcourt toute l'archive.

    Repli : si l'API ne renvoie rien d'exploitable, bascule sur le RSS.
    """
    print(f"📡 Fetch archive API : {SUBSTACK_ARCHIVE_API}", flush=True)
    new_meta: list[dict] = []
    total_meta = 0
    offset = 0
    page = 0
    api_ok = False

    while True:
        batch = _fetch_archive_page(offset)
        if batch is None:
            break  # erreur → on tentera le repli RSS plus bas
        api_ok = True
        page += 1
        if not batch:
            print(f"  ✓ Page {page} vide → fin de pagination", flush=True)
            break
        total_meta += len(batch)
        # Editions de cette page qui ne sont pas encore sur le blog
        page_new = []
        for meta in batch:
            slug = meta.get("slug") or clean_slug("", meta.get("title", ""))
            if not should_keep(meta.get("title", "")):
                continue
            if not full and md_already_synced(slug):
                continue
            page_new.append(meta)
        new_meta.extend(page_new)
        print(f"  ✓ Page {page} : {len(batch)} editions, {len(page_new)} nouvelle(s) "
              f"(total nouvelles : {len(new_meta)})", flush=True)
        # Early-exit incremental : page sans nouveaute → tout le reste est deja synchro
        if not full and not page_new:
            print(f"  ⏹  Page sans nouvelle edition → arret (incremental)", flush=True)
            break
        if limit and len(new_meta) >= limit:
            break
        if page >= 20:
            print(f"  ⚠️  Garde-fou : arret a 20 pages", file=sys.stderr, flush=True)
            break
        offset += len(batch)
        polite_sleep()

    # --- Repli RSS si l'API a echoue ---------------------------------------
    if not api_ok:
        print("  ↪️  API archive injoignable → repli sur le flux RSS", flush=True)
        rss_posts = fetch_feed_posts(limit, full=full)
        if rss_posts or full:
            return rss_posts
        # RSS vide aussi : soit rien de neuf, soit blocage total → verifie
        # Si le RSS n'a meme pas pu etre parse, fetch_feed_posts renvoie [].
        # On ne peut pas distinguer ici → on laisse main() trancher.
        return []

    # --- Construction des Post depuis les meta nouvelles -------------------
    posts: list[Post] = []
    for meta in new_meta:
        title = meta.get("title", "")
        try:
            dt = datetime.fromisoformat(meta.get("post_date", "").replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < CUTOFF_DATE:
            continue
        url = meta.get("canonical_url") or f"{SUBSTACK_BASE}/p/{meta.get('slug', '')}"
        html = fetch_post_html(url)
        polite_sleep()
        posts.append(Post(
            id=str(meta.get("id")),
            title=title,
            subtitle=meta.get("subtitle") or None,
            date=dt,
            url=url,
            slug=meta.get("slug") or clean_slug("", title),
            html=html,
            cover_image_url=meta.get("cover_image") or None,
        ))
        if limit and len(posts) >= limit:
            break

    mode = "mode complet" if full else "mode incremental"
    print(f"  → {len(posts)} post(s) a traiter via archive "
          f"({mode}, {total_meta} editions vues)", flush=True)
    # Stocke un indicateur pour main() : l'API a-t-elle repondu ?
    fetch_archive_posts.api_responded = api_ok  # type: ignore
    fetch_archive_posts.total_meta = total_meta  # type: ignore
    return posts

# Attributs par defaut (lus par main() pour la detection d'echec)
fetch_archive_posts.api_responded = False  # type: ignore
fetch_archive_posts.total_meta = 0  # type: ignore

def fetch_post_html(url: str) -> str:
    try:
        r = get_session().get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        article = soup.select_one("div.body.markup") or soup.select_one("article") or soup
        return str(article)
    except Exception as e:
        print(f"  ⚠️  HTML KO {url}: {e}", file=sys.stderr)
        return ""

# --------------------------------------------------------------------------- #
# Conversion HTML → Markdown
# --------------------------------------------------------------------------- #

def extract_tldr(soup: BeautifulSoup, max_words: int = 55) -> Optional[str]:
    """GEO — Extrait un TL;DR a partir du premier paragraphe substantiel."""
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) < 80:
            continue
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]).rstrip(".,;:!? ") + "…"
        return text
    return None


def enrich_with_claude(title: str, subtitle: Optional[str], html_body: str) -> Optional[dict]:
    """Phase 2 GEO — Appel a l'API Claude Haiku pour generer un enrichissement.

    Retourne un dict {tldr, faq, entities} ou None si echec/absence de cle.
    """
    if not ANTHROPIC_API_KEY or not ANTHROPIC_AVAILABLE:
        return None

    soup = BeautifulSoup(html_body, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text_body = soup.get_text(" ", strip=True)
    if len(text_body) > MAX_HTML_CHARS_FOR_LLM:
        text_body = text_body[:MAX_HTML_CHARS_FOR_LLM] + " […troncature…]"

    prompt = f"""Tu es un assistant specialise dans l'optimisation GEO (Generative Engine Optimization).

Contexte : "The Ugly Truth" est une newsletter francophone de Nash HUGHES sur la tech, l'IA, la geopolitique et la defense. Ton irreverencieux signature.

Titre : {title}
{f'Sous-titre : {subtitle}' if subtitle else ''}

Contenu :
{text_body}

Ta tache : generer un enrichissement GEO pour que cet article soit cite optimalement par les moteurs IA (Perplexity, ChatGPT, Claude, Google SGE).

Retourne UNIQUEMENT un JSON valide strict (pas de markdown, pas de commentaires, pas de texte avant/apres) :

{{
  "tldr": "Resume answer-first en 2 phrases factuelles (120-200 caracteres). Commence par la these principale. Style direct et neutre. Pas de punchline.",
  "faq": [
    {{"question": "Question naturelle qu'un utilisateur poserait sur ce sujet en langage courant", "answer": "Reponse factuelle 1-2 phrases."}},
    {{"question": "...", "answer": "..."}},
    {{"question": "...", "answer": "..."}}
  ],
  "entities": ["Personne1", "Entreprise2", "Pays3", "Technologie4"]
}}

Regles :
- FAQ : 3 a 5 paires Q/R maximum, factuelles, formulations naturelles
- Entities : max 12, noms propres uniquement, desambiguises, pas de mots generiques
- Langue : francais
- JSON strict et valide uniquement
"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        if "tldr" not in data or "faq" not in data or "entities" not in data:
            return None
        if not isinstance(data["faq"], list) or not isinstance(data["entities"], list):
            return None
        return data
    except Exception as e:
        print(f"  ⚠️  LLM enrichment KO: {e}", file=sys.stderr)
        return None


def extract_entities(soup: BeautifulSoup) -> list[str]:
    """GEO — Extrait les entites nommees citees (heuristique simple)."""
    text = soup.get_text(" ", strip=True)
    candidates = re.findall(r"\b(?:[A-ZÉÈÀÇ][a-zéèàùçA-Z]+(?:[-\s][A-ZÉÈÀÇ][a-zéèàùç]+){0,3})\b", text)
    stopwords = {"Salut", "Nash", "Ugly", "Truth", "Substack", "The", "France", "Europe"}
    entities = []
    for c in candidates:
        if c not in stopwords and len(c) > 3 and c not in entities:
            entities.append(c)
    return entities[:15]


def process_post(post: Post, dry_run: bool = False) -> bool:
    """Traite un post, telecharge ses images, ecrit le fichier Markdown."""
    soup = BeautifulSoup(post.html, "html.parser")

    cover_image = None
    post_images_dir = IMAGES_DIR / post.slug

    # 1a. Cover officiel choisi par l'auteur sur Substack (prioritaire)
    if post.cover_image_url:
        filename = sanitize_image_filename(post.cover_image_url)
        dest = post_images_dir / filename
        if not dry_run:
            if download_and_optimize_image(post.cover_image_url, dest):
                cover_image = f"/images/posts/{post.slug}/{filename}"
        else:
            cover_image = f"/images/posts/{post.slug}/{filename}"

    # 1b. Toutes les images du body — reecriture vers chemins locaux
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or not src.startswith("http"):
            continue
        filename = sanitize_image_filename(src)
        dest = post_images_dir / filename
        if not dry_run:
            ok = download_and_optimize_image(src, dest)
            if not ok:
                continue
        local_path = f"/images/posts/{post.slug}/{filename}"
        img["src"] = local_path
        if not cover_image:
            cover_image = local_path

    # 1.5 GEO — Phase 2 (LLM) si cle API dispo, sinon fallback Phase 1
    llm_data = enrich_with_claude(post.title, post.subtitle, post.html)
    if llm_data:
        print(f"  🤖 Enrichissement LLM OK (tldr={len(llm_data['tldr'])}c, "
              f"faq={len(llm_data['faq'])}, entities={len(llm_data['entities'])})")
        tldr = llm_data["tldr"]
        entities = llm_data["entities"]
        faq = llm_data["faq"]
    else:
        tldr = extract_tldr(soup)
        entities = extract_entities(soup)
        faq = None

    # 2. Convertit HTML → Markdown
    markdown = md(str(soup), heading_style="ATX", bullets="-")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    # 3. Front-matter enrichi GEO
    frontmatter = [
        "---",
        f'title: {json.dumps(post.title, ensure_ascii=False)}',
    ]
    if post.subtitle:
        frontmatter.append(f'subtitle: {json.dumps(post.subtitle, ensure_ascii=False)}')
    if tldr:
        frontmatter.append(f'tldr: {json.dumps(tldr, ensure_ascii=False)}')
    frontmatter.append(f"date: {post.date.strftime('%Y-%m-%d')}")
    frontmatter.append(f'substackUrl: {json.dumps(post.url)}')
    if cover_image:
        frontmatter.append(f'coverImage: {json.dumps(cover_image)}')
    if entities:
        frontmatter.append(f'entities: {json.dumps(entities, ensure_ascii=False)}')
    if faq:
        frontmatter.append("faq:")
        for qa in faq:
            frontmatter.append(f"  - question: {json.dumps(qa['question'], ensure_ascii=False)}")
            frontmatter.append(f"    answer: {json.dumps(qa['answer'], ensure_ascii=False)}")
    frontmatter.append("---")
    frontmatter.append("")

    content = "\n".join(frontmatter) + markdown + "\n"

    # 4. Ecrit le fichier
    out = CONTENT_DIR / f"{post.slug}.md"
    if dry_run:
        print(f"  📝 (dry-run) would write {out} ({len(content)} chars)")
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"  ✅ {post.slug}.md ({len(content)} chars)")
    return True

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Simule sans ecrire")
    parser.add_argument("--limit", type=int, help="Limite le nombre de posts traites (test)")
    parser.add_argument("--full", action="store_true",
                        help="Re-sync complet (par defaut : incremental)")
    parser.add_argument("--source", choices=["rss", "archive"], default="archive",
                        help="archive = API interne (defaut), rss = flux RSS")
    args = parser.parse_args()

    state = load_state()

    print(f"🚀 Sync Substack → blog Astro ({args.source}, "
          f"{'complet' if args.full else 'incremental'})")
    print(f"   Cutoff date : {CUTOFF_DATE.isoformat()}")
    print(f"   Content dir : {CONTENT_DIR}")
    print(f"   Editions deja sur le blog : {count_existing_md()}")

    if args.source == "archive":
        posts = fetch_archive_posts(args.limit, full=args.full)
        api_responded = getattr(fetch_archive_posts, "api_responded", False)
        total_meta = getattr(fetch_archive_posts, "total_meta", 0)
    else:
        posts = fetch_feed_posts(args.limit, full=args.full)
        # En mode RSS direct, on considere l'API "absente" ; le succes se
        # mesure au fait d'avoir parse au moins une entree existante.
        api_responded = True
        total_meta = -1  # non applicable

    # --- Detection d'echec BRUYANTE ----------------------------------------
    # Cas anormal : la source n'a renvoye AUCUNE edition exploitable alors
    # que le blog en contient deja (≈90 dans l'archive). C'est un blocage,
    # pas un "rien de neuf". On sort en erreur pour que l'echec soit VISIBLE.
    if args.source == "archive" and not api_responded:
        print("\n❌ ECHEC : l'API archive ET le repli RSS sont injoignables.",
              file=sys.stderr)
        print("   Cause probable : blocage reseau (IP runner / Cloudflare).",
              file=sys.stderr)
        print("   → Lancer une sync manuelle ou via la tache planifiee Cowork.",
              file=sys.stderr)
        sys.exit(1)
    if args.source == "archive" and api_responded and total_meta == 0:
        print("\n❌ ECHEC : l'API archive a repondu mais 0 edition retournee.",
              file=sys.stderr)
        print("   Cause probable : challenge Cloudflare / reponse vide.",
              file=sys.stderr)
        sys.exit(1)

    processed = 0
    for post in posts:
        fingerprint = hashlib.sha1(post.html.encode()).hexdigest()
        if not args.full and state.get(post.id) == fingerprint:
            continue
        print(f"\n📄 {post.date.strftime('%Y-%m-%d')} — {post.title}")
        if process_post(post, dry_run=args.dry_run):
            state[post.id] = fingerprint
            processed += 1

    if not args.dry_run:
        save_state(state)

    print(f"\n✨ Termine. {processed} edition(s) ajoutee(s)/mise(s) a jour, "
          f"{len(posts) - processed} inchangee(s).")
    if processed == 0:
        print("   (Aucune nouvelle edition — le blog est deja a jour.)")

if __name__ == "__main__":
    main()
