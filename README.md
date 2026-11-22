# GEO & AI Readiness Website Auditor

A lightweight, high-speed Apify Actor that audits any list of websites for **Generative Engine Optimization (GEO)** and **AI search readiness**. Built with pure asynchronous Python (`httpx` + `BeautifulSoup`/`lxml`)—no heavy headless browser required—so it runs fast on minimal memory.

## Why GEO & AI Readiness Matters

Modern discovery increasingly happens inside AI answer engines and assistants such as ChatGPT, Claude, Perplexity, and Google AI Overviews. Traditional SEO alone is no longer enough: websites must explicitly permit AI crawlers, expose machine-readable site summaries (`/llms.txt`), provide clean Schema.org JSON-LD entity graphs, and deliver real server-side rendered (SSR) HTML rather than empty JavaScript shells.

## What This Actor Checks

For every URL you provide, the Actor performs six core checks concurrently:

1. **AI Crawler Access (`/robots.txt`)** — Parses `robots.txt` rules for major AI crawlers (`GPTBot`, `OAI-SearchBot`, `ChatGPT-User`, `ClaudeBot`, `anthropic-ai`, `PerplexityBot`, `Google-Extended`, `Applebot-Extended`, `CCBot`) and reports each as `allowed`, `disallowed`, or `not_specified`.
2. **LLM Site Summaries (`/llms.txt` & `/llms-full.txt`)** — Verifies HTTP 200 responses with genuine text/markdown content (filtering out HTML soft-404 pages), and reports existence, byte size, and markdown link count.
3. **XML Sitemap Validation (`/sitemap.xml`)** — Checks sitemaps declared in `robots.txt` as well as `/sitemap.xml`, confirming valid `urlset` or `sitemapindex` XML.
4. **Structured Data (`JSON-LD`)** — Extracts all `<script type="application/ld+json">` blocks and lists detected Schema.org `@type` values.
5. **SSR & Heading Visibility** — Measures visible server-rendered word count versus raw HTML byte size to flag JS-only empty shells, and counts `<h1>` and `<h2>` tags.
6. **Metadata & AI Directives** — Extracts `<title>`, `<meta name="description">`, canonical URL, OpenGraph (`og:title`, `og:description`), and `<meta name="robots">` / `X-Robots-Tag` directives (flagging `noai`, `noimageai`, and `noindex`).

## GEO Score Rubric (0–100)

| Category | Max Points | Criteria |
| --- | --- | --- |
| AI Bots Allowed | 25 | Proportion of tracked AI crawlers not disallowed in `robots.txt` (with penalties for `noai`/`noindex`) |
| `llms.txt` Valid | 20 | Valid markdown `/llms.txt` (15 pts) plus markdown links or `/llms-full.txt` (+5 pts) |
| JSON-LD Present | 20 | Valid `application/ld+json` block with Schema.org `@type` values (15 pts for 1 type, 20 pts for 2+) |
| Visible SSR Text & Headings | 20 | Server-rendered visible word count (12 pts) plus `<h1>` (4 pts) and `<h2>` (4 pts) |
| Meta Tags + `sitemap.xml` | 15 | Title (3), description (3), canonical (2), OpenGraph (2), valid XML sitemap (5) |

Each audited URL also includes a `recommendations` list with concrete, prioritized fixes.

## Input Example

```json
{
    "urls": [
        "https://stripe.com",
        "https://apify.com"
    ],
    "maxConcurrency": 5,
    "timeoutSecs": 15
}
```

## Output Example

```json
{
    "url": "https://apify.com",
    "finalUrl": "https://apify.com/",
    "origin": "https://apify.com",
    "status": "ok",
    "httpStatusCode": 200,
    "auditedAt": "2026-11-22T15:00:00+00:00",
    "geoScore": 100,
    "scoreBreakdown": {
        "aiBotsAllowed": 25,
        "llmsTxt": 20,
        "jsonLd": 20,
        "ssrAndHeadings": 20,
        "metaAndSitemap": 15
    },
    "robotsTxt": {
        "url": "https://apify.com/robots.txt",
        "statusCode": 200,
        "aiCrawlers": {
            "GPTBot": "not_specified",
            "OAI-SearchBot": "not_specified",
            "ChatGPT-User": "not_specified",
            "ClaudeBot": "not_specified",
            "anthropic-ai": "not_specified",
            "PerplexityBot": "not_specified",
            "Google-Extended": "not_specified",
            "Applebot-Extended": "not_specified",
            "CCBot": "not_specified"
        },
        "sitemapsDeclared": [
            "https://apify.com/sitemap.xml"
        ]
    },
    "llmsTxt": {
        "url": "https://apify.com/llms.txt",
        "exists": true,
        "statusCode": 200,
        "byteSize": 17633,
        "markdownLinkCount": 73
    },
    "llmsFullTxt": {
        "url": "https://apify.com/llms-full.txt",
        "exists": false,
        "statusCode": 404,
        "byteSize": 0,
        "markdownLinkCount": 0
    },
    "sitemap": {
        "exists": true,
        "url": "https://apify.com/sitemap.xml",
        "statusCode": 200,
        "type": "sitemapindex",
        "discoveredFrom": "robots.txt"
    },
    "html": {
        "title": "Apify: Marketplace of ready-to-run tools for AI",
        "metaDescription": "...",
        "canonicalUrl": "https://apify.com",
        "openGraph": {
            "ogTitle": "...",
            "ogDescription": "..."
        },
        "metaRobots": {
            "raw": "index,follow",
            "hasNoAi": false,
            "hasNoImageAi": false,
            "hasNoIndex": false
        },
        "jsonLd": {
            "present": true,
            "blockCount": 3,
            "schemaTypes": ["Organization", "SoftwareApplication", "WebSite"]
        },
        "ssrCheck": {
            "visibleWordCount": 1601,
            "rawHtmlBytes": 523912,
            "textToHtmlRatio": 0.0196,
            "isLikelyJsShell": false
        },
        "headings": {
            "h1Count": 1,
            "h2Count": 11
        }
    },
    "recommendations": []
}
```

## Use Cases

- **SEO & GEO Agencies**: Bulk-audit client portfolios and generate prioritized AI readiness reports.
- **Competitive Benchmarking**: Compare your brand's `geoScore`, `llms.txt` coverage, and AI crawler policies against competitors.
- **CI/CD & Monitoring**: Schedule recurring runs to catch accidental `robots.txt` blocks, broken sitemaps, or SSR regressions.
- **Lead Generation**: Identify websites missing `llms.txt`, JSON-LD, or SSR content that need GEO consulting.

## Pay-Per-Event Billing

This Actor supports Apify's Pay-Per-Event model using the `site-audited` event. You are only charged for successfully audited URLs; network or parsing errors are logged to the dataset without incurring an event charge.
