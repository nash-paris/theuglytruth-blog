#!/usr/bin/env python3
"""
sync-substack.py — Synchronise Substack → blog Astro.

- Fetch le flux RSS complet
- Filtre : posts dont le titre commence par "The Ugly Truth" ou "THE UGLY TRUTH",
  publiés à partir du 7 mars 2024, podcasts exclus
- Télécharge chaque post :
    * HTML brut → converti en Markdown
    * Images → téléchargées, optimisées (WebP si possible), stockées localement
    * Front-matter YAML avec métadonnées
- Écrit dans src/content/blog/<slug>.md
- Idempotent : ne recrée pas les fichiers existants sauf si le post a été mis à jour sur Substack

Usage :
    python3 scripts/sync-substack.py
    python3 scripts/sync-substack.py --dry-run
    python3 scripts/sync-substack.py --limit 5  # pour tester

Dépendances (auto-installées si absentes) :
    feedparser, requests, beautifulsoup4, markdownify, python-slugify, pillow
"""

from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Bootstrap dépendances
# --------------------------------------------------------------------------- #

REQUIRED = ["feedparser", "requests", "beautifulsoup4", "markdownify", "python-slugify", "Pillow"]

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

import feedparser
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from slugify import slugify
from PIL import Image
from io import BytesIO

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SUBSTACK_FEED = "https://nashsuglytruth.substack.com/feed"
SUBSTACK_ARCHIVE_API = "https://nashsuglytruth.substack.com/api/v1/archive"

CUTOFF_DATE = datetime(2024, 3, 7, tzinfo=timezone.utc)

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "src" / "content" / "blog"
IMAGES_DIR = BASE_DIR / "public" / "images" / "posts"
STATE_FILE = BASE_DIR / "scripts" / ".cache" / "sync-state.json"

MAX_IMAGE_WIDTH = 1600
JPEG_QUALITY = 82
WEBP_QUALITY = 82

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TheUglyTruth-SyncBot/1.0; +https://theuglytruth.fr)",
}

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
    """Garde uniquement les posts 'The Ugly Truth' hors podcasts."""
    if not title:
        return False
    low = title.lower()
    if "podcast" in low:
        return False
    return "ugly truth" in low

def clean_slug(raw: str, title: str) -> str:
    """Slug depuis URL Substack ou fallback titre."""
    if raw:
        # retire les préfixes type "the-ugly-truth-"
        return raw.strip("/")
    return slugify(title, max_length=80)

def sanitize_image_filename(url: str) -> str:
    """Nom de fichier image lisible et stable basé sur hash de l'URL."""
    h = hashlib.sha1(url.encode()).hexdigest()[:10]
    ext = ".webp"
    name = url.rsplit("/", 1)[-1].split("?")[0]
    name = slugify(Path(name).stem, max_length=30) or "image"
    return f"{name}-{h}{ext}"

def download_and_optimize_image(url: str, dest: Path) -> bool:
    """Télécharge + convertit en WebP + redimensionne. Retourne True si OK."""
    if dest.exists():
        return True
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
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

def fetch_feed_posts(limit: Optional[int] = None) -> list[Post]:
    """RSS = 20 derniers posts. Pour tous les archives, utiliser fetch_archive_posts."""
    print(f"📡 Fetch RSS : {SUBSTACK_FEED}")
    feed = feedparser.parse(SUBSTACK_FEED)
    posts = []
    for entry in feed.entries:
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if published < CUTOFF_DATE:
            continue
        if not should_keep(entry.title):
            continue
        slug_raw = entry.link.rstrip("/").rsplit("/", 1)[-1]
        html = entry.get("content", [{}])[0].get("value") or entry.get("summary", "")
        posts.append(Post(
            id=str(entry.get("id", entry.link)),
            title=entry.title,
            subtitle=entry.get("subtitle") or None,
            date=published,
            url=entry.link,
            slug=clean_slug(slug_raw, entry.title),
            html=html,
        ))
        if limit and len(posts) >= limit:
            break
    print(f"  → {len(posts)} posts retenus via RSS")
    return posts

def fetch_archive_posts(limit: Optional[int] = None) -> list[Post]:
    """API interne /api/v1/archive — couvre toutes les archives.
    Remarque : nécessite une session authentifiée pour accéder aux posts privés.
    Pour du contenu public, les URLs publiques marchent sans cookie.
    """
    print(f"📡 Fetch archive API : {SUBSTACK_ARCHIVE_API}")
    all_meta = []
    offset = 0
    while True:
        r = requests.get(f"{SUBSTACK_ARCHIVE_API}?sort=new&offset={offset}&limit=25", headers=HEADERS, timeout=30)
        if not r.ok:
            print(f"  ⚠️  HTTP {r.status_code}, fallback RSS", file=sys.stderr)
            return fetch_feed_posts(limit)
        batch = r.json()
        if not batch:
            break
        all_meta.extend(batch)
        offset += 25
        if len(batch) < 25:
            break
    posts = []
    for meta in all_meta:
        title = meta.get("title", "")
        post_date = meta.get("post_date", "")
        try:
            dt = datetime.fromisoformat(post_date.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < CUTOFF_DATE:
            continue
        if not should_keep(title):
            continue
        url = meta.get("canonical_url") or f"https://nashsuglytruth.substack.com/p/{meta.get('slug', '')}"
        # Fetch HTML individuel
        html = fetch_post_html(url)
        posts.append(Post(
            id=str(meta.get("id")),
            title=title,
            subtitle=meta.get("subtitle") or None,
            date=dt,
            url=url,
            slug=meta.get("slug") or clean_slug("", title),
            html=html,
        ))
        if limit and len(posts) >= limit:
            break
    print(f"  → {len(posts)} posts retenus via archive")
    return posts

def fetch_post_html(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
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

def process_post(post: Post, dry_run: bool = False) -> bool:
    """Traite un post, télécharge ses images, écrit le fichier Markdown."""
    soup = BeautifulSoup(post.html, "html.parser")

    # 1. Remplace les URLs d'images par des chemins locaux
    cover_image = None
    post_images_dir = IMAGES_DIR / post.slug
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

    # 2. Convertit HTML → Markdown
    markdown = md(str(soup), heading_style="ATX", bullets="-")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    # 3. Front-matter
    frontmatter = [
        "---",
        f'title: {json.dumps(post.title, ensure_ascii=False)}',
    ]
    if post.subtitle:
        frontmatter.append(f'subtitle: {json.dumps(post.subtitle, ensure_ascii=False)}')
    frontmatter.append(f"date: {post.date.strftime('%Y-%m-%d')}")
    frontmatter.append(f'substackUrl: {json.dumps(post.url)}')
    if cover_image:
        frontmatter.append(f'coverImage: {json.dumps(cover_image)}')
    frontmatter.append("---")
    frontmatter.append("")

    content = "\n".join(frontmatter) + markdown + "\n"

    # 4. Écrit le fichier
    out = CONTENT_DIR / f"{post.slug}.md"
    if dry_run:
        print(f"  📝 (dry-run) would write {out}")
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
    parser.add_argument("--dry-run", action="store_true", help="Simule sans écrire")
    parser.add_argument("--limit", type=int, help="Limite le nombre de posts traités (test)")
    parser.add_argument("--source", choices=["rss", "archive"], default="archive",
                        help="rss = 20 derniers (rapide), archive = toutes (complet)")
    args = parser.parse_args()

    state = load_state()

    print(f"🚀 Sync Substack → blog Astro ({args.source})")
    print(f"   Cutoff date : {CUTOFF_DATE.isoformat()}")
    print(f"   Content dir : {CONTENT_DIR}")

    posts = fetch_archive_posts(args.limit) if args.source == "archive" else fetch_feed_posts(args.limit)

    processed = 0
    for post in posts:
        # Skip si inchangé depuis dernière sync
        fingerprint = hashlib.sha1(post.html.encode()).hexdigest()
        if state.get(post.id) == fingerprint:
            continue
        print(f"\n📄 {post.date.strftime('%Y-%m-%d')} — {post.title}")
        if process_post(post, dry_run=args.dry_run):
            state[post.id] = fingerprint
            processed += 1

    if not args.dry_run:
        save_state(state)

    print(f"\n✨ Terminé. {processed} post(s) mis à jour, {len(posts) - processed} inchangés.")

if __name__ == "__main__":
    main()
