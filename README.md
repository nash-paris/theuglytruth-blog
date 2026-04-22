# The Ugly Truth — Blog

Blog SEO satellite de la newsletter [The Ugly Truth, by Nash](https://nashsuglytruth.substack.com).

Les éditions sont automatiquement synchronisées depuis Substack 2 fois par jour via GitHub Actions.

## Stack

- **Framework** : [Astro](https://astro.build) (SSG statique)
- **Hébergement** : [Cloudflare Pages](https://pages.cloudflare.com) (gratuit)
- **Sync** : script Python `scripts/sync-substack.py` déclenché par GitHub Actions cron
- **Domaine** : [theuglytruth.fr](https://theuglytruth.fr)

## Commandes

```sh
# Installation des dépendances
npm install

# Dev local sur http://localhost:4321
npm run dev

# Build production
npm run build

# Synchronisation manuelle Substack → blog
npm run sync
# ou
python3 scripts/sync-substack.py
```

## Structure

```
src/
├── content/
│   ├── blog/          # Posts Markdown auto-synchronisés depuis Substack
│   └── config.ts      # Schema de validation des posts
├── layouts/           # Layouts Astro (BaseLayout, etc.)
├── pages/             # Routes (/, /blog, /blog/[slug], /a-propos, /rss.xml)
├── components/        # Composants Astro (Header, Footer)
└── styles/            # CSS

public/                # Assets statiques
scripts/
└── sync-substack.py   # Script de sync depuis Substack RSS

.github/workflows/
└── sync-substack.yml  # Cron GitHub Actions 2x/jour
```

## Périmètre d'import

- Uniquement les posts "The Ugly Truth" publiés **à partir du 7 mars 2024**
- Podcasts exclus
- Archives pré-rebrand (Elodie & Nash, Light Me Up) exclues
