import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import type { APIContext } from 'astro';

export async function GET(context: APIContext) {
  const posts = await getCollection('blog');
  const sorted = posts
    .filter(p => !p.data.draft)
    .sort((a, b) => b.data.date.valueOf() - a.data.date.valueOf());

  return rss({
    title: 'The Ugly Truth, by Nash',
    description: "IA, tech, géopolitique, défense. La vérité qui dérange, chaque semaine.",
    site: context.site!,
    items: sorted.map(post => ({
      link: `/blog/${post.slug}/`,
      title: post.data.title,
      description: post.data.subtitle || '',
      pubDate: post.data.date,
    })),
  });
}
