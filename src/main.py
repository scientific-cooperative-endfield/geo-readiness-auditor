"""GEO & AI Readiness Website Auditor — Core Actor Logic."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from apify import Actor
from bs4 import BeautifulSoup
from lxml import etree

AI_CRAWLERS: list[str] = [
    'GPTBot',
    'OAI-SearchBot',
    'ChatGPT-User',
    'ClaudeBot',
    'anthropic-ai',
    'PerplexityBot',
    'Google-Extended',
    'Applebot-Extended',
    'CCBot',
]

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (compatible; GEOReadinessAuditor/1.0; +https://apify.com)'
)

MARKDOWN_LINK_RE = re.compile(r'\[[^\]]+\]\((?:https?://|/|#)[^)\s]+\)')
WORD_RE = re.compile(r'\b\w+\b', re.UNICODE)


def get_user_agent() -> str:
    """Return polite User-Agent (allows optional override via environment variable)."""
    return os.environ.get('GEO_AUDITOR_USER_AGENT', DEFAULT_USER_AGENT)


def normalize_url(raw_url: str) -> tuple[str, str]:
    """Normalize input URL and return (target_url, base_origin)."""
    cleaned = raw_url.strip()
    if not cleaned:
        raise ValueError('Empty URL provided')
    if not re.match(r'^https?://', cleaned, re.IGNORECASE):
        cleaned = f'https://{cleaned}'
    parsed = urlparse(cleaned)
    if not parsed.netloc:
        raise ValueError(f'Invalid URL: {raw_url!r}')
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    origin = f'{scheme}://{netloc}'
    path = parsed.path or '/'
    target_url = f'{origin}{path}'
    if parsed.query:
        target_url = f'{target_url}?{parsed.query}'
    return target_url, origin


def parse_robots_txt(robots_text: str) -> tuple[dict[str, str], list[str]]:
    """Parse robots.txt for AI crawler rules and Sitemap directives.

    Returns:
        (crawler_statuses, sitemaps) where each crawler status is one of:
        'allowed', 'disallowed', or 'not_specified'.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    current_agents: list[str] = []
    sitemaps: list[str] = []
    seen_directive_in_group = False

    for raw_line in robots_text.splitlines():
        line = raw_line.split('#', 1)[0].strip()
        if not line or ':' not in line:
            continue
        directive, value = line.split(':', 1)
        directive_l = directive.strip().lower()
        value_s = value.strip()

        if directive_l == 'sitemap':
            if value_s and value_s not in sitemaps:
                sitemaps.append(value_s)
            continue

        if directive_l == 'user-agent':
            agent = value_s.lower()
            if seen_directive_in_group:
                current_agents = [agent]
                seen_directive_in_group = False
            else:
                current_agents.append(agent)
            groups.setdefault(agent, [])
        elif directive_l in ('allow', 'disallow'):
            seen_directive_in_group = True
            for agent in current_agents:
                groups.setdefault(agent, []).append((directive_l, value_s))

    def eval_rules(rules: list[tuple[str, str]]) -> str | None:
        """Evaluate root ('/') access from a list of (allow|disallow, path) rules."""
        if not rules:
            return None
        best_match_len = -1
        best_decision: str | None = None
        for directive, path in rules:
            if directive == 'disallow' and path == '':
                if best_match_len < 0:
                    best_decision = 'allowed'
                continue
            if path in ('/', '/*'):
                match_len = len(path)
                if match_len > best_match_len or (
                    match_len == best_match_len and directive == 'allow'
                ):
                    best_match_len = match_len
                    best_decision = 'allowed' if directive == 'allow' else 'disallowed'
        if best_decision is None:
            return 'allowed'
        return best_decision

    wildcard_decision = eval_rules(groups.get('*', []))
    statuses: dict[str, str] = {}

    for bot in AI_CRAWLERS:
        bot_key = bot.lower()
        if bot_key in groups:
            decision = eval_rules(groups[bot_key])
            statuses[bot] = decision or 'allowed'
        elif wildcard_decision == 'disallowed':
            statuses[bot] = 'disallowed'
        else:
            statuses[bot] = 'not_specified'

    return statuses, sitemaps


def is_html_content(content_type: str, body: str) -> bool:
    """Detect if a response body is an HTML page (e.g., soft 404 / SPA fallback)."""
    ct = content_type.lower()
    if 'text/html' in ct or 'application/xhtml' in ct:
        return True
    sample = body.lstrip()[:256].lower()
    return (
        sample.startswith('<!doctype html')
        or sample.startswith('<html')
        or '<head>' in sample
        or '<body' in sample
    )


async def check_llms_file(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """Fetch and validate /llms.txt or /llms-full.txt."""
    result: dict[str, Any] = {
        'url': url,
        'exists': False,
        'statusCode': None,
        'byteSize': 0,
        'markdownLinkCount': 0,
    }
    try:
        resp = await client.get(url)
        result['statusCode'] = resp.status_code
        if resp.status_code != 200:
            return result
        body = resp.text
        ct = resp.headers.get('content-type', '')
        if not body.strip() or is_html_content(ct, body):
            return result
        result['exists'] = True
        result['byteSize'] = len(resp.content)
        result['markdownLinkCount'] = len(MARKDOWN_LINK_RE.findall(body))
    except (httpx.HTTPError, OSError):
        pass
    return result


async def check_sitemap(
    client: httpx.AsyncClient,
    origin: str,
    robots_sitemaps: list[str],
) -> dict[str, Any]:
    """Verify valid XML sitemap exists from robots.txt or /sitemap.xml."""
    candidates: list[tuple[str, str]] = [
        (sm_url, 'robots.txt') for sm_url in robots_sitemaps[:5]
    ]
    default_sitemap = urljoin(f'{origin}/', 'sitemap.xml')
    if default_sitemap not in robots_sitemaps:
        candidates.append((default_sitemap, 'default_path'))

    for candidate_url, source in candidates:
        try:
            resp = await client.get(candidate_url)
            if resp.status_code != 200 or not resp.content.strip():
                continue
            ct = resp.headers.get('content-type', '')
            if is_html_content(ct, resp.text):
                continue
            try:
                root = etree.fromstring(resp.content)
                local_tag = etree.QName(root).localname.lower()
                if local_tag in ('urlset', 'sitemapindex'):
                    return {
                        'exists': True,
                        'url': str(resp.url),
                        'statusCode': resp.status_code,
                        'type': local_tag,
                        'discoveredFrom': source,
                    }
            except etree.XMLSyntaxError:
                continue
        except (httpx.HTTPError, OSError):
            continue

    return {
        'exists': False,
        'url': default_sitemap,
        'statusCode': None,
        'type': None,
        'discoveredFrom': None,
    }


def extract_schema_types(payload: Any, found: set[str]) -> None:
    """Recursively collect Schema.org @type values from parsed JSON-LD."""
    if isinstance(payload, list):
        for item in payload:
            extract_schema_types(item, found)
    elif isinstance(payload, dict):
        schema_type = payload.get('@type')
        if isinstance(schema_type, str) and schema_type.strip():
            found.add(schema_type.strip())
        elif isinstance(schema_type, list):
            for t in schema_type:
                if isinstance(t, str) and t.strip():
                    found.add(t.strip())
        if '@graph' in payload:
            extract_schema_types(payload['@graph'], found)
        for key, val in payload.items():
            if key not in ('@type', '@graph') and isinstance(val, (dict, list)):
                extract_schema_types(val, found)


def analyze_html(html_bytes: bytes, html_text: str, x_robots_tag: str = '') -> dict[str, Any]:
    """Analyze HTML for metadata, OpenGraph, robots flags, JSON-LD, SSR text, and headings."""
    soup = BeautifulSoup(html_text, 'lxml')

    title_tag = soup.find('title')
    title = title_tag.get_text(strip=True) if title_tag else None

    meta_desc_tag = soup.find('meta', attrs={'name': re.compile(r'^description$', re.I)})
    meta_description = (
        meta_desc_tag.get('content', '').strip()
        if meta_desc_tag and meta_desc_tag.get('content')
        else None
    )

    canonical_tag = soup.find(
        'link',
        attrs={
            'rel': lambda v: v
            and 'canonical' in [x.lower() for x in (v if isinstance(v, list) else [v])]
        },
    )
    canonical_url = (
        canonical_tag.get('href', '').strip()
        if canonical_tag and canonical_tag.get('href')
        else None
    )

    og_title_tag = soup.find('meta', attrs={'property': re.compile(r'^og:title$', re.I)})
    og_desc_tag = soup.find('meta', attrs={'property': re.compile(r'^og:description$', re.I)})
    og_title = (
        og_title_tag.get('content', '').strip()
        if og_title_tag and og_title_tag.get('content')
        else None
    )
    og_description = (
        og_desc_tag.get('content', '').strip()
        if og_desc_tag and og_desc_tag.get('content')
        else None
    )

    robots_tags = soup.find_all('meta', attrs={'name': re.compile(r'^robots$', re.I)})
    robots_contents = [
        tag.get('content', '').strip()
        for tag in robots_tags
        if tag.get('content')
    ]
    if x_robots_tag:
        robots_contents.append(x_robots_tag.strip())
    combined_robots = ', '.join(robots_contents)
    robots_tokens = {
        tok.strip().lower()
        for tok in re.split(r'[,;\s]+', combined_robots)
        if tok.strip()
    }
    meta_robots = {
        'raw': combined_robots or None,
        'hasNoAi': 'noai' in robots_tokens,
        'hasNoImageAi': 'noimageai' in robots_tokens,
        'hasNoIndex': 'noindex' in robots_tokens,
    }

    json_ld_tags = soup.find_all(
        'script', attrs={'type': re.compile(r'^application/ld\+json', re.I)}
    )
    schema_types: set[str] = set()
    valid_json_ld_blocks = 0
    for tag in json_ld_tags:
        raw_json = tag.string or tag.get_text()
        if not raw_json or not raw_json.strip():
            continue
        try:
            parsed_ld = json.loads(raw_json)
            valid_json_ld_blocks += 1
            extract_schema_types(parsed_ld, schema_types)
        except json.JSONDecodeError:
            continue

    h1_count = len(soup.find_all('h1'))
    h2_count = len(soup.find_all('h2'))

    for non_visible in soup(['script', 'style', 'noscript', 'template', 'svg', 'head']):
        non_visible.decompose()
    visible_text = soup.get_text(separator=' ', strip=True)
    visible_word_count = len(WORD_RE.findall(visible_text))
    raw_html_bytes = len(html_bytes)
    text_bytes = len(visible_text.encode('utf-8'))
    text_to_html_ratio = round(text_bytes / raw_html_bytes, 4) if raw_html_bytes > 0 else 0.0
    is_likely_js_shell = visible_word_count < 50 or (
        raw_html_bytes > 10_000 and visible_word_count < 30
    )

    return {
        'title': title,
        'metaDescription': meta_description,
        'canonicalUrl': canonical_url,
        'openGraph': {
            'ogTitle': og_title,
            'ogDescription': og_description,
        },
        'metaRobots': meta_robots,
        'jsonLd': {
            'present': valid_json_ld_blocks > 0,
            'blockCount': valid_json_ld_blocks,
            'schemaTypes': sorted(schema_types),
        },
        'ssrCheck': {
            'visibleWordCount': visible_word_count,
            'rawHtmlBytes': raw_html_bytes,
            'textToHtmlRatio': text_to_html_ratio,
            'isLikelyJsShell': is_likely_js_shell,
        },
        'headings': {
            'h1Count': h1_count,
            'h2Count': h2_count,
        },
    }


def calculate_geo_score(
    ai_bots: dict[str, str],
    llms_txt: dict[str, Any],
    llms_full_txt: dict[str, Any],
    sitemap: dict[str, Any],
    html_data: dict[str, Any],
) -> tuple[int, dict[str, int], list[str]]:
    """Calculate GEO readiness score (0-100), component breakdown, and recommendations."""
    recommendations: list[str] = []

    total_bots = len(AI_CRAWLERS)
    disallowed_bots = [bot for bot, status in ai_bots.items() if status == 'disallowed']
    allowed_or_open = total_bots - len(disallowed_bots)
    ai_bots_score = round(25 * (allowed_or_open / total_bots)) if total_bots else 0

    meta_robots = html_data['metaRobots']
    if meta_robots['hasNoAi'] or meta_robots['hasNoIndex']:
        ai_bots_score = max(0, ai_bots_score - 10)

    if disallowed_bots:
        recommendations.append(
            f"Review robots.txt rules blocking AI crawlers: {', '.join(disallowed_bots)}."
        )
    if meta_robots['hasNoAi'] or meta_robots['hasNoImageAi']:
        recommendations.append(
            'Remove restrictive noai/noimageai directives from meta robots or X-Robots-Tag if you want AI citation visibility.'
        )
    if meta_robots['hasNoIndex']:
        recommendations.append(
            'Remove noindex directive from meta robots or X-Robots-Tag so search and AI engines can index the page.'
        )

    llms_score = 0
    if llms_txt['exists']:
        llms_score = 15
        if llms_full_txt['exists'] or llms_txt['markdownLinkCount'] > 0:
            llms_score = 20
        else:
            recommendations.append(
                'Add structured markdown links to /llms.txt or publish /llms-full.txt to help LLMs navigate key documentation.'
            )
    else:
        recommendations.append(
            'Create a valid markdown /llms.txt file at your site root summarizing key pages and documentation for AI assistants.'
        )

    json_ld = html_data['jsonLd']
    schema_types = json_ld['schemaTypes']
    if json_ld['present'] and len(schema_types) >= 2:
        json_ld_score = 20
    elif json_ld['present'] and len(schema_types) == 1:
        json_ld_score = 15
        recommendations.append(
            'Expand JSON-LD structured data with additional Schema.org types (e.g., Organization, WebSite, FAQPage, Product).'
        )
    else:
        json_ld_score = 0
        recommendations.append(
            'Add valid application/ld+json Schema.org structured data (such as Organization, WebSite, or Article) to improve entity grounding.'
        )

    ssr = html_data['ssrCheck']
    headings = html_data['headings']
    ssr_score = 0
    if not ssr['isLikelyJsShell'] and ssr['visibleWordCount'] >= 150:
        ssr_score += 12
    elif not ssr['isLikelyJsShell'] and ssr['visibleWordCount'] >= 50:
        ssr_score += 6
        recommendations.append(
            'Increase server-rendered visible body copy (currently under 150 words) to give AI crawlers richer factual context.'
        )
    else:
        recommendations.append(
            'Enable Server-Side Rendering (SSR) or static pre-rendering: the initial HTML appears to be a JS-only shell with minimal visible text.'
        )

    if headings['h1Count'] >= 1:
        ssr_score += 4
    else:
        recommendations.append('Add a clear <h1> heading describing the primary topic of the page.')

    if headings['h2Count'] >= 1:
        ssr_score += 4
    else:
        recommendations.append(
            'Add descriptive <h2> subheadings to structure content for AI answer extraction.'
        )

    meta_sitemap_score = 0
    if html_data['title']:
        meta_sitemap_score += 3
    else:
        recommendations.append('Add a descriptive <title> tag.')

    if html_data['metaDescription']:
        meta_sitemap_score += 3
    else:
        recommendations.append('Add a concise <meta name="description"> tag.')

    if html_data['canonicalUrl']:
        meta_sitemap_score += 2
    else:
        recommendations.append('Add a <link rel="canonical"> tag to prevent duplicate-URL dilution.')

    og = html_data['openGraph']
    if og['ogTitle'] and og['ogDescription']:
        meta_sitemap_score += 2
    else:
        recommendations.append(
            'Add OpenGraph og:title and og:description metadata for rich citations and previews.'
        )

    if sitemap['exists']:
        meta_sitemap_score += 5
    else:
        recommendations.append('Publish a valid XML /sitemap.xml and reference it in /robots.txt.')

    breakdown = {
        'aiBotsAllowed': ai_bots_score,
        'llmsTxt': llms_score,
        'jsonLd': json_ld_score,
        'ssrAndHeadings': ssr_score,
        'metaAndSitemap': meta_sitemap_score,
    }
    total_score = sum(breakdown.values())
    return total_score, breakdown, recommendations


async def audit_single_url(
    raw_url: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Run a complete GEO & AI readiness audit for a single URL."""
    target_url, origin = normalize_url(raw_url)
    robots_url = urljoin(f'{origin}/', 'robots.txt')
    llms_url = urljoin(f'{origin}/', 'llms.txt')
    llms_full_url = urljoin(f'{origin}/', 'llms-full.txt')

    async def fetch_robots() -> tuple[dict[str, str], list[str], int | None]:
        try:
            resp = await client.get(robots_url)
            if resp.status_code == 200 and not is_html_content(
                resp.headers.get('content-type', ''), resp.text
            ):
                bots, sitemaps = parse_robots_txt(resp.text)
                return bots, sitemaps, resp.status_code
            return {b: 'not_specified' for b in AI_CRAWLERS}, [], resp.status_code
        except (httpx.HTTPError, OSError):
            return {b: 'not_specified' for b in AI_CRAWLERS}, [], None

    target_task = asyncio.create_task(client.get(target_url))
    robots_task = asyncio.create_task(fetch_robots())
    llms_task = asyncio.create_task(check_llms_file(client, llms_url))
    llms_full_task = asyncio.create_task(check_llms_file(client, llms_full_url))

    try:
        target_resp = await target_task
        target_resp.raise_for_status()
    except Exception:
        for task in (robots_task, llms_task, llms_full_task):
            task.cancel()
        await asyncio.gather(robots_task, llms_task, llms_full_task, return_exceptions=True)
        raise

    (ai_bots, robots_sitemaps, robots_status), llms_txt, llms_full_txt = await asyncio.gather(
        robots_task,
        llms_task,
        llms_full_task,
    )

    sitemap = await check_sitemap(client, origin, robots_sitemaps)

    x_robots_tag = target_resp.headers.get('x-robots-tag', '')
    html_data = analyze_html(target_resp.content, target_resp.text, x_robots_tag=x_robots_tag)

    geo_score, score_breakdown, recommendations = calculate_geo_score(
        ai_bots=ai_bots,
        llms_txt=llms_txt,
        llms_full_txt=llms_full_txt,
        sitemap=sitemap,
        html_data=html_data,
    )

    return {
        'url': raw_url,
        'finalUrl': str(target_resp.url),
        'origin': origin,
        'status': 'ok',
        'httpStatusCode': target_resp.status_code,
        'auditedAt': datetime.now(timezone.utc).isoformat(),
        'geoScore': geo_score,
        'scoreBreakdown': score_breakdown,
        'robotsTxt': {
            'url': robots_url,
            'statusCode': robots_status,
            'aiCrawlers': ai_bots,
            'sitemapsDeclared': robots_sitemaps,
        },
        'llmsTxt': llms_txt,
        'llmsFullTxt': llms_full_txt,
        'sitemap': sitemap,
        'html': html_data,
        'recommendations': recommendations,
    }


async def main() -> None:
    """Apify Actor entry point."""
    async with Actor:
        actor_input = await Actor.get_input() or {}
        raw_urls = actor_input.get('urls') or []
        if not isinstance(raw_urls, list) or not raw_urls:
            Actor.log.warning('No URLs provided in input; exiting.')
            return

        max_concurrency = max(1, int(actor_input.get('maxConcurrency', 5)))
        timeout_secs = max(5, int(actor_input.get('timeoutSecs', 15)))

        Actor.log.info(
            'Starting GEO & AI Readiness audit for %d URL(s) (maxConcurrency=%d, timeoutSecs=%d)',
            len(raw_urls),
            max_concurrency,
            timeout_secs,
        )

        semaphore = asyncio.Semaphore(max_concurrency)
        stop_event = asyncio.Event()

        headers = {
            'User-Agent': get_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        timeout = httpx.Timeout(timeout_secs)

        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            http2=True,
        ) as client:

            async def process_url(raw_url: str) -> None:
                if stop_event.is_set():
                    return
                async with semaphore:
                    if stop_event.is_set():
                        return
                    try:
                        Actor.log.info('Auditing %s', raw_url)
                        result = await audit_single_url(raw_url, client)
                        charge_result = await Actor.push_data(
                            result,
                            charged_event_name='site-audited',
                        )
                        Actor.log.info(
                            'Completed %s -> geoScore=%d/100',
                            raw_url,
                            result['geoScore'],
                        )
                        if charge_result and charge_result.event_charge_limit_reached:
                            Actor.log.warning(
                                'Pay-Per-Event charge limit reached after %s; stopping gracefully.',
                                raw_url,
                            )
                            stop_event.set()
                    except Exception as exc:
                        Actor.log.error('Failed to audit %s: %s', raw_url, exc)
                        await Actor.push_data(
                            {
                                'url': raw_url,
                                'status': 'error',
                                'errorMessage': str(exc),
                            }
                        )

            await asyncio.gather(*(process_url(u) for u in raw_urls))

        Actor.log.info('GEO & AI Readiness audit finished.')
