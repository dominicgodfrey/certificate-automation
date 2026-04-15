"""Render the certificate HTML template to a PDF.

Uses Jinja2 for templating and Playwright (headless Chromium) for PDF rendering.
Headless Chromium is heavier than weasyprint, but guarantees pixel-perfect
output for any CSS — including the clip-path ribbon and custom @font-face fonts.
"""
import asyncio
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
TEMPLATES = ROOT / "templates"


class CertificateRenderer:
    def __init__(self, template_name: str = "certificate.html"):
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES)),
            autoescape=True,  # Escape HTML in template variables to prevent injection
        )
        self.template = self.env.get_template(template_name)
        self._browser = None
        self._playwright = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch()
        return self

    async def __aexit__(self, *args):
        await self._browser.close()
        await self._playwright.stop()

    async def render(self, data: dict, output_path: Path) -> Path:
        """Render the template with `data` and write a PDF to `output_path`."""
        html = self.template.render(**data)

        # Write to a temp file inside templates/ so relative asset paths resolve.
        # Unique per render so concurrent calls don't collide.
        tmp_path = TEMPLATES / f"_render_{output_path.stem}.html"
        tmp_path.write_text(html, encoding="utf-8")

        try:
            page = await self._browser.new_page()
            await page.goto(tmp_path.as_uri())
            await page.wait_for_load_state("networkidle")
            # Critical: wait for @font-face fonts to load before PDF capture,
            # otherwise auto-shrink runs against fallback metrics.
            await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(150)  # let auto-shrink JS settle
            await page.pdf(
                path=str(output_path),
                width="11in",
                height="8.5in",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
            await page.close()
        finally:
            tmp_path.unlink(missing_ok=True)

        return output_path


# Convenience sync entry point for one-off rendering
def render_one(data: dict, output_path: Path) -> Path:
    async def _go():
        async with CertificateRenderer() as r:
            return await r.render(data, output_path)
    return asyncio.run(_go())
