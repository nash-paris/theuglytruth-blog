import { defineCollection, z } from 'astro:content';

const blog = defineCollection({
  type: 'content',
  schema: z.object({
    title: z.string(),
    subtitle: z.string().optional(),
    tldr: z.string().optional(), // GEO: résumé "answer-first" de 2-3 phrases
    date: z.coerce.date(),
    substackUrl: z.string().url().optional(),
    coverImage: z.string().optional(),
    tags: z.array(z.string()).default([]),
    entities: z.array(z.string()).default([]), // GEO: entités nommées citées (personnes, entreprises, pays)
    faq: z.array(z.object({
      question: z.string(),
      answer: z.string(),
    })).optional(), // GEO: FAQ balisée FAQPage
    draft: z.boolean().default(false),
  }),
});

export const collections = { blog };
