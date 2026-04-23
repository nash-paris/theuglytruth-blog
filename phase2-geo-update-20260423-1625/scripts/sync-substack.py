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
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from slugify import slugify
from PIL import Image
from io import BytesIO

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

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

# --- Phase 2 GEO : enrichissement via LLM -----------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # Modèle le plus économique 2026
MAX_HTML_CHARS_FOR_LLM = 12000  # Limite input pour éviter tokens excessifs

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

def extract_tldr(soup: BeautifulSoup, max_words: int = 55) -> Optional[str]:
    """GEO — Extrait un TL;DR à partir du premier paragraphe substantiel.

    Heuristiques :
    - Premier <p> de plus de 80 caractères (évite les intros type "Salut,")
    - Nettoyé (pas de markdown, pas de HTML résiduel)
    - Tronqué à ~55 mots si plus long
    """
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) < 80:
            continue
        # Premier paragraphe substantiel
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]).rstrip(".,;:!? ") + "…"
        return text
    return None


def enrich_with_claude(title: str, subtitle: Optional[str], html_body: str) -> Optional[dict]:
    """Phase 2 GEO — Appel à l'API Claude Haiku pour générer un enrichissement structuré.

    Retourne un dict {tldr, faq, entities} ou None si échec/absence de clé.
    """
    if not ANTHROPIC_API_KEY or not ANTHROPIC_AVAILABLE:
        return None

    # Nettoie le HTML pour réduire les tokens (retire scripts, styles, attrs lourds)
    soup = BeautifulSoup(html_body, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text_body = soup.get_text(" ", strip=True)
    if len(text_body) > MAX_HTML_CHARS_FOR_LLM:
        text_body = text_body[:MAX_HTML_CHARS_FOR_LLM] + " […troncature…]"

    prompt = f"""Tu es un assistant spécialisé dans l'optimisation GEO (Generative Engine Optimization).

Contexte : "The Ugly Truth" est une newsletter francophone de Nash HUGHES sur la tech, l'IA, la géopolitique et la défense. Ton irrévérencieux signature.

Titre : {title}
{f'Sous-titre : {subtitle}' if subtitle else ''}

Contenu :
{text_body}

Ta tâche : générer un enrichissement GEO pour que cet article soit cité optimalement par les moteurs IA (Perplexity, ChatGPT, Claude, Google SGE).

Retourne UNIQUEMENT un JSON valide strict (pas de markdown, pas de commentaires, pas de texte avant/après) :

{{
  "tldr": "Résumé answer-first en 2 phrases factuelles (120-200 caractères). Commence par la thèse principale. Style direct et neutre. Pas de punchline.",
  "faq": [
    {{"question": "Question naturelle qu'un utilisateur poserait sur ce sujet en langage courant", "answer": "Réponse factuelle 1-2 phrases."}},
    {{"question": "...", "answer": "..."}},
    {{"question": "...", "answer": "..."}}
  ],
  "entities": ["Personne1", "Entreprise2", "Pays3", "Technologie4"]
}}

Règles :
- FAQ : 3 à 5 paires Q/R maximum, factuelles, formulations naturelles
- Entities : max 12, noms propres uniquement, désambiguïsés, pas de mots génériques
- Langue : français
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
        # Nettoie les éventuels ```json ... ```
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        # Validation structurelle
        if not isinstance(data, dict):
            return None
        if "tldr" not in data or "faq" not in data or "entities" not in data:
            return None
        # Sanity check
        if not isinstance(data["faq"], list) or not isinstance(data["entities"], list):
            return None
        return data
    except Exception as e:
        print(f"  ⚠️  LLM enrichment KO: {e}", file=sys.stderr)
        return None


def extract_entities(soup: BeautifulSoup) -> list[str]:
    """GEO — Extrait les entités nommées citées (heuristique simple : mots en capitales).

    Pour la Phase 2, on remplacera par un appel LLM. Pour l'instant : regex
    sur les tokens qui ressemblent à des noms propres (min 2 lettres + capitale initiale).
    """
    text = soup.get_text(" ", strip=True)
    # Sequences de mots commençant par une majuscule (ignore les débuts de phrase)
    candidates = re.findall(r"\b(?:[A-ZÉÈÀÇ][a-zéèàùçA-Z]+(?:[-\s][A-ZÉÈÀÇ][a-zéèàùç]+){0,3})\b", text)
    # Filtre les faux positifs courants
    stopwords = {"Salut", "Nash", "Ugly", "Truth", "Substack", "The", "France", "Europe"}
    entities = []
    for c in candidates:
        if c not in stopwords and len(c) > 3 and c not in entities:
            entities.append(c)
    return entities[:15]  # Top 15 pour éviter le bruit


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

    # 1.5 GEO — Extractions : Phase 2 (LLM) si clé API dispo, sinon fallback Phase 1 (heuristique)
    llm_data = enrich_with_claude(post.title, post.subtitle, post.html)
    if llm_data:
        print(f"  🤖 Enrichissement LLM OK (tldr={len(llm_data['tldr'])}c, faq={len(llm_data['faq'])}, entities={len(llm_data['entities'])})")
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
