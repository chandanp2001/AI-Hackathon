"""Web scraper service for fetching documentation from Alpha Docs and related sites.

This module provides async web scraping capabilities with:
- Rate limiting and retries
- URL filtering for allowed domains
- Content extraction (text, code blocks, metadata)
- Link following within allowed domains
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Any
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger(__name__)


@dataclass
class ScraperConfig:
    """Configuration for the web scraper.
    
    Args:
        base_url: Starting URL for scraping
        allowed_domains: List of domains to follow links to
        max_pages: Maximum number of pages to scrape per request
        timeout: Request timeout in seconds
        rate_limit_delay: Delay between requests in seconds
        max_retries: Maximum number of retries for failed requests
        user_agent: User agent string for requests
    """
    base_url: str = "https://alpha.razorpay.com/"
    allowed_domains: list[str] = field(default_factory=lambda: [
        "alpha.razorpay.com",
        "razorpay.com",
    ])
    max_pages: int = 10
    timeout: int = 30
    rate_limit_delay: float = 0.5
    max_retries: int = 3
    user_agent: str = "Mozilla/5.0 (compatible; AlphaDocsBot/1.0)"


@dataclass
class ScrapedPage:
    """Represents a scraped web page.
    
    Args:
        url: Page URL
        title: Page title
        content: Extracted text content
        code_blocks: List of code snippets found
        metadata: Additional metadata (description, keywords, etc.)
        links: Internal links found on the page
        scrape_time: Time taken to scrape in seconds
    """
    url: str
    title: str
    content: str
    code_blocks: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    scrape_time: float = 0.0


class WebScraper:
    """Async web scraper for documentation sites.
    
    Provides on-demand scraping with content extraction optimized
    for technical documentation.
    
    Args:
        config: Scraper configuration
        
    Examples:
        >>> config = ScraperConfig(base_url="https://alpha.razorpay.com/")
        >>> scraper = WebScraper(config)
        >>> pages = await scraper.scrape_for_query("payment integration")
    """
    
    def __init__(self, config: Optional[ScraperConfig] = None):
        self.config = config or ScraperConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._visited_urls: set[str] = set()
        self._last_request_time: float = 0.0
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            headers = {"User-Agent": self.config.user_agent}
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session
        
    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            
    def _is_allowed_url(self, url: str) -> bool:
        """Check if URL is within allowed domains.
        
        Args:
            url: URL to check
            
        Returns:
            bool: True if URL domain is allowed
        """
        try:
            parsed = urlparse(url)
            return any(
                parsed.netloc == domain or parsed.netloc.endswith(f".{domain}")
                for domain in self.config.allowed_domains
            )
        except Exception:
            return False
            
    def _normalize_url(self, url: str, base_url: str) -> Optional[str]:
        """Normalize and validate a URL.
        
        Args:
            url: URL to normalize (can be relative)
            base_url: Base URL for resolving relative URLs
            
        Returns:
            Normalized absolute URL or None if invalid
        """
        try:
            # Skip fragment-only, javascript, and mailto links
            if url.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                return None
                
            # Resolve relative URLs
            absolute_url = urljoin(base_url, url)
            
            # Remove fragments
            parsed = urlparse(absolute_url)
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            
            # Add trailing slash for consistency if no file extension
            if not re.search(r'\.\w+$', parsed.path) and not clean_url.endswith('/'):
                clean_url += '/'
                
            return clean_url if self._is_allowed_url(clean_url) else None
            
        except Exception:
            return None
            
    async def _rate_limit(self) -> None:
        """Apply rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.config.rate_limit_delay:
            await asyncio.sleep(self.config.rate_limit_delay - elapsed)
        self._last_request_time = time.time()
        
    async def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch a single page with retries.
        
        Args:
            url: URL to fetch
            
        Returns:
            HTML content or None if failed
        """
        session = await self._get_session()
        
        for attempt in range(self.config.max_retries):
            try:
                await self._rate_limit()
                
                async with session.get(url) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        if 'text/html' in content_type:
                            return await response.text()
                        else:
                            logger.debug(f"Skipping non-HTML content: {url}")
                            return None
                    elif response.status == 429:
                        # Rate limited - wait and retry
                        retry_after = int(response.headers.get('Retry-After', 5))
                        logger.warning(f"Rate limited, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                    elif response.status >= 400:
                        logger.warning(f"HTTP {response.status} for {url}")
                        return None
                        
            except asyncio.TimeoutError:
                logger.warning(f"Timeout fetching {url} (attempt {attempt + 1})")
            except aiohttp.ClientError as e:
                logger.warning(f"Client error fetching {url}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error fetching {url}: {e}")
                
            if attempt < self.config.max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                
        return None
        
    def _extract_content(self, html: str, url: str) -> ScrapedPage:
        """Extract content from HTML.
        
        Args:
            html: Raw HTML content
            url: Page URL
            
        Returns:
            ScrapedPage with extracted content
        """
        start_time = time.time()
        soup = BeautifulSoup(html, 'lxml')
        
        # Remove script, style, nav, footer elements
        for element in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            element.decompose()
            
        # Extract title
        title = ""
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)
        elif soup.find('h1'):
            title = soup.find('h1').get_text(strip=True)
            
        # Extract metadata
        metadata = {}
        
        # Meta description
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            metadata['description'] = meta_desc['content']
            
        # Meta keywords
        meta_keywords = soup.find('meta', attrs={'name': 'keywords'})
        if meta_keywords and meta_keywords.get('content'):
            metadata['keywords'] = meta_keywords['content']
            
        # Open Graph data
        og_title = soup.find('meta', attrs={'property': 'og:title'})
        if og_title and og_title.get('content'):
            metadata['og_title'] = og_title['content']
            
        # Extract code blocks
        code_blocks = []
        for code in soup.find_all(['code', 'pre']):
            code_text = code.get_text(strip=True)
            if code_text and len(code_text) > 10:  # Skip tiny snippets
                code_blocks.append(code_text)
                
        # Extract main content
        # Look for main content areas first
        main_content = None
        for selector in ['main', 'article', '[role="main"]', '.content', '.documentation', '.docs-content']:
            if selector.startswith('.') or selector.startswith('['):
                main_content = soup.select_one(selector)
            else:
                main_content = soup.find(selector)
            if main_content:
                break
                
        if not main_content:
            main_content = soup.find('body') or soup
            
        # Extract text content
        content_parts = []
        for element in main_content.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'td', 'th']):
            text = element.get_text(strip=True)
            if text and len(text) > 5:
                # Add heading markers
                if element.name.startswith('h'):
                    level = int(element.name[1])
                    text = f"{'#' * level} {text}"
                content_parts.append(text)
                
        content = "\n\n".join(content_parts)
        
        # Extract internal links
        links = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            normalized = self._normalize_url(href, url)
            if normalized and normalized not in self._visited_urls:
                links.append(normalized)
                
        scrape_time = time.time() - start_time
        
        return ScrapedPage(
            url=url,
            title=title,
            content=content[:50000],  # Limit content size
            code_blocks=code_blocks[:20],  # Limit code blocks
            metadata=metadata,
            links=links[:50],  # Limit links
            scrape_time=scrape_time
        )
        
    async def scrape_url(self, url: str) -> Optional[ScrapedPage]:
        """Scrape a single URL.
        
        Args:
            url: URL to scrape
            
        Returns:
            ScrapedPage or None if failed
        """
        if url in self._visited_urls:
            return None
            
        html = await self._fetch_page(url)
        if not html:
            return None
            
        self._visited_urls.add(url)
        return self._extract_content(html, url)
        
    async def scrape_for_query(
        self,
        query: str,
        start_url: Optional[str] = None,
        max_pages: Optional[int] = None
    ) -> list[ScrapedPage]:
        """Scrape pages relevant to a query.
        
        Starts from the base URL and follows links, looking for
        content relevant to the query.
        
        Args:
            query: Search query to guide scraping
            start_url: Starting URL (defaults to config base_url)
            max_pages: Maximum pages to scrape (defaults to config max_pages)
            
        Returns:
            List of scraped pages
            
        Examples:
            >>> pages = await scraper.scrape_for_query("payment integration")
            >>> for page in pages:
            ...     print(f"{page.title}: {len(page.content)} chars")
        """
        start_url = start_url or self.config.base_url
        max_pages = max_pages or self.config.max_pages
        
        # Reset visited URLs for new query
        self._visited_urls.clear()
        
        pages: list[ScrapedPage] = []
        urls_to_visit: list[str] = [start_url]
        query_terms = set(query.lower().split())
        
        while urls_to_visit and len(pages) < max_pages:
            current_url = urls_to_visit.pop(0)
            
            if current_url in self._visited_urls:
                continue
                
            page = await self.scrape_url(current_url)
            if not page:
                continue
                
            pages.append(page)
            logger.info(f"Scraped: {page.title} ({len(page.content)} chars)")
            
            # Score links by relevance to query and add to queue
            scored_links = []
            for link in page.links:
                if link in self._visited_urls or link in urls_to_visit:
                    continue
                    
                # Simple relevance scoring based on URL
                link_lower = link.lower()
                score = sum(1 for term in query_terms if term in link_lower)
                scored_links.append((score, link))
                
            # Sort by score (descending) and add to queue
            scored_links.sort(reverse=True, key=lambda x: x[0])
            for _, link in scored_links:
                if link not in urls_to_visit:
                    urls_to_visit.append(link)
                    
        return pages
        
    async def search_and_scrape(
        self,
        query: str,
        search_terms: Optional[list[str]] = None
    ) -> list[ScrapedPage]:
        """Search and scrape pages based on query.
        
        This method attempts to find the most relevant pages for the query
        by building search URLs and scraping the results.
        
        Args:
            query: Natural language query
            search_terms: Optional specific search terms
            
        Returns:
            List of relevant scraped pages
        """
        # Build search URL if the site supports it
        base_url = self.config.base_url
        
        # Try common search patterns
        search_paths = [
            f"{base_url}search?q={query.replace(' ', '+')}",
            f"{base_url}?s={query.replace(' ', '+')}",
            base_url,  # Fall back to main page
        ]
        
        all_pages: list[ScrapedPage] = []
        
        for search_url in search_paths:
            if len(all_pages) >= self.config.max_pages:
                break
                
            pages = await self.scrape_for_query(
                query,
                start_url=search_url,
                max_pages=self.config.max_pages - len(all_pages)
            )
            all_pages.extend(pages)
            
            if pages:  # If we found pages, stop trying other search patterns
                break
                
        return all_pages
        
    def filter_relevant_content(
        self,
        pages: list[ScrapedPage],
        query: str,
        min_relevance: float = 0.1
    ) -> list[dict[str, Any]]:
        """Filter and format page content by relevance to query.
        
        Args:
            pages: List of scraped pages
            query: Original query
            min_relevance: Minimum relevance score (0-1)
            
        Returns:
            List of formatted content dictionaries
        """
        query_terms = set(query.lower().split())
        results = []
        
        for page in pages:
            # Calculate simple relevance score
            content_lower = (page.title + " " + page.content).lower()
            matches = sum(1 for term in query_terms if term in content_lower)
            relevance = matches / len(query_terms) if query_terms else 0
            
            if relevance >= min_relevance:
                results.append({
                    "url": page.url,
                    "title": page.title,
                    "content": page.content[:5000],  # Limit for LLM context
                    "code_blocks": page.code_blocks[:5],
                    "relevance_score": relevance,
                    "metadata": page.metadata
                })
                
        # Sort by relevance
        results.sort(key=lambda x: x["relevance_score"], reverse=True)
        return results[:10]  # Return top 10 most relevant

