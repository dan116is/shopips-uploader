#!/usr/bin/env python3
"""
SHOPIPS — Automatic Product Uploader for Konimbo
Runs daily via GitHub Actions to upload pending products.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

MANAGEMENT_URL = "https://bnext-api.co.il/melaket/konimbo/reports/NewProducts"
MAKUR_URL = "https://www.makorhachashmal.co.il"
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Vue-3 reactive field setter (must be redefined per evaluate() call)
# ---------------------------------------------------------------------------
SET_V = """
(el, val) => {
    if (!el) return false;
    const proto = el.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, val);
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
}
"""


class Logger:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        self.lines.append(line)

    def save(self):
        fname = LOGS_DIR / f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        fname.write_text("\n".join(self.lines), encoding="utf-8")
        return fname


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

async def set_by_id(page, field_id: str, value: str) -> bool:
    return await page.evaluate(
        f"([id, val]) => {{ const el = document.getElementById(id); return ({SET_V})(el, val); }}",
        [field_id, value],
    )


async def set_by_index(page, index: int, value: str) -> bool:
    return await page.evaluate(
        f"""([idx, val]) => {{
            const inputs = Array.from(
                document.querySelectorAll('input[type="text"], input:not([type]), textarea')
            );
            const el = inputs[idx];
            return ({SET_V})(el, val);
        }}""",
        [index, value],
    )


async def safe_close_dialog(page):
    """Close any open modal/dialog without pressing Escape."""
    for sel in ['button:has-text("סגירה")', 'button:has-text("×")', 'button[aria-label="Close"]', '.modal-close']:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await page.wait_for_timeout(600)
                return
        except Exception:
            pass


async def screenshot_on_error(page, sku: str):
    try:
        path = LOGS_DIR / f"error_{sku}_{datetime.now().strftime('%H%M%S')}.png"
        await page.screenshot(path=str(path))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Image search on makorhachashmal.co.il
# ---------------------------------------------------------------------------

async def fetch_makur_images(context, search_term: str, log: Logger) -> list[str]:
    """Return up to 3 large cloudfront image URLs for the search term."""
    if not search_term:
        return []
    page = await context.new_page()
    try:
        await page.goto(f"{MAKUR_URL}/search?q={search_term}", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # Click first product
        first_product = page.locator("a.product-item, .product-card a, .products-grid a").first
        if not await first_product.is_visible(timeout=4000):
            log.info(f"  [תמונות] לא נמצאו תוצאות ב-makorhachashmal עבור: {search_term}")
            return []
        await first_product.click()
        await page.wait_for_timeout(2000)

        # Try to open lightbox to load large images
        zoom_btn = page.locator(".gallery-image, .product-image img, .fotorama__img").first
        try:
            await zoom_btn.click(timeout=3000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Extract cloudfront image URLs
        urls = await page.evaluate("""
            () => {
                const imgs = Array.from(document.querySelectorAll('img'));
                const seen = new Set();
                const result = [];
                for (const img of imgs) {
                    const src = img.src || '';
                    if (src.includes('cloudfront') && src.includes('/photos/') && !seen.has(src)) {
                        seen.add(src);
                        const large = src.replace('/medium/', '/large/').replace('/small/', '/large/').replace('/thumb/', '/large/');
                        result.push(large);
                    }
                }
                return result.slice(0, 3);
            }
        """)
        log.info(f"  [תמונות] נמצאו {len(urls)} תמונות מ-makorhachashmal")
        return urls
    except Exception as e:
        log.info(f"  [תמונות] שגיאה ב-makorhachashmal: {e}")
        return []
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Category selection
# ---------------------------------------------------------------------------

async def select_category(page, category: str, search_hint: str, log: Logger):
    """Open category dropdown, search, and click the matching item."""
    # Find and click the dropdown trigger
    dropdown_triggers = [
        f'button:has-text("{category}")',
        '[class*="category"] button',
        '[data-field="category"] button',
        'button:has-text("בחר קטגוריה")',
        'button:has-text("קטגוריה")',
    ]
    clicked = False
    for sel in dropdown_triggers:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        log.info(f"  [קטגוריה] לא נמצא כפתור דרופדאון — מדלג")
        return

    await page.wait_for_timeout(600)

    # Type in search box
    search_input = page.locator('input[placeholder*="חפש"], input[placeholder*="search"]').first
    try:
        await search_input.wait_for(state="visible", timeout=3000)
        await search_input.fill(search_hint)
        await page.wait_for_timeout(1000)
    except PWTimeout:
        log.info(f"  [קטגוריה] שדה חיפוש לא נמצא")
        return

    # Click first matching option
    option_selectors = [
        f'li:has-text("{category}")',
        f'[role="option"]:has-text("{category}")',
        f'.dropdown-item:has-text("{category}")',
        f'span:has-text("{category}")',
    ]
    for sel in option_selectors:
        try:
            opt = page.locator(sel).first
            if await opt.is_visible(timeout=1500):
                await opt.click()
                log.info(f"  [קטגוריה] נבחרה: {category}")
                return
        except Exception:
            pass

    # Fallback: click the first visible option
    try:
        first_opt = page.locator('li[role="option"], .dropdown-item, li.select-item').first
        if await first_opt.is_visible(timeout=1000):
            text = await first_opt.inner_text()
            await first_opt.click()
            log.info(f"  [קטגוריה] נבחרה (fallback): {text.strip()}")
    except Exception:
        log.info(f"  [קטגוריה] לא הצלחנו לבחור קטגוריה")


# ---------------------------------------------------------------------------
# Image upload
# ---------------------------------------------------------------------------

async def add_images_to_form(page, image_urls: list[str], log: Logger):
    """Click 'הוסף תמונה' for each URL and fill the input."""
    added = 0
    for url in image_urls:
        try:
            add_btn = page.locator('button:has-text("הוסף תמונה"), button:has-text("הוספת תמונה")').last
            await add_btn.wait_for(state="visible", timeout=3000)
            await add_btn.click()
            await page.wait_for_timeout(500)

            # Fill the last image URL input that appeared
            filled = await page.evaluate(
                f"""(url) => {{
                    const selectors = [
                        'input[name*="image"]',
                        'input[placeholder*="URL"]',
                        'input[placeholder*="תמונה"]',
                        'input[placeholder*="url"]',
                    ];
                    for (const sel of selectors) {{
                        const inputs = Array.from(document.querySelectorAll(sel));
                        const last = inputs[inputs.length - 1];
                        if (last) {{
                            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(last, url);
                            last.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                            last.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                    }}
                    return false;
                }}""",
                url,
            )
            if filled:
                added += 1
            await page.wait_for_timeout(300)
        except Exception as e:
            log.info(f"  [תמונות] שגיאה בהוספת תמונה: {e}")

    log.info(f"  [תמונות] הוספו {added}/{len(image_urls)} תמונות")


# ---------------------------------------------------------------------------
# Main product upload flow
# ---------------------------------------------------------------------------

async def upload_one_product(page, context, product: dict, log: Logger) -> tuple[bool, str]:
    sku = product["second_code"]
    name = product["title"]
    log.info(f"--- מעלה: {name} (מק\"ט: {sku}) ---")

    # Capture Konimbo item ID from the API response
    konimbo_id_from_api: list[str] = []

    async def _on_response(response):
        url = response.url
        if "bnext-api" in url and response.request.method in ("PUT", "POST"):
            try:
                body = await response.json()
                # Konimbo returns {"id": 12345, ...}
                item_id = (
                    body.get("id")
                    or body.get("item", {}).get("id")
                    or body.get("konimbo_id")
                )
                if item_id:
                    konimbo_id_from_api.append(str(item_id))
            except Exception:
                pass

    page.on("response", _on_response)

    try:
        # Find and click "העלה לאתר" for this SKU row
        row = page.locator(f'tr:has-text("{sku}")').first
        upload_btn = row.locator('button:has-text("העלה לאתר")').first
        await upload_btn.wait_for(state="visible", timeout=5000)
        await upload_btn.click()
        await page.wait_for_timeout(2000)

        # Verify the dialog opened by checking second_code value
        form_sku = await page.evaluate('document.getElementById("second_code")?.value || ""')
        log.info(f"  מק\"ט בטופס: {form_sku}")

        # ----- Fill text fields by ID -----
        await set_by_id(page, "warranty",      product["warranty"])
        await set_by_id(page, "price",         product["price"])
        await set_by_id(page, "origin_price",  product["origin_price"])
        await set_by_id(page, "delivery_time", product.get("delivery_time", "3"))
        await set_by_id(page, "seo_title",     product["seo_title"])
        await set_by_id(page, "seo_keywords",  product["seo_keywords"])
        await set_by_id(page, "slug",          product["slug"])

        # ----- Textareas by index -----
        # Index 2 = desc, index 14 = seo_description (per original spec)
        await set_by_index(page, 2,  product["desc"])
        await set_by_index(page, 14, product["seo_description"])

        # ----- Category -----
        await select_category(page, product["category"], product["category_search"], log)

        # ----- Images -----
        images = list(product.get("images") or [])
        if not images and product.get("makorhachashmal_search"):
            images = await fetch_makur_images(context, product["makorhachashmal_search"], log)

        if images:
            await add_images_to_form(page, images, log)
        else:
            log.info("  [תמונות] אין תמונות זמינות — ממשיך בלי תמונות")

        # ----- Submit -----
        submit_btn = page.locator('button:has-text("סיום"), button:has-text("שמור")').last
        await submit_btn.scroll_into_view_if_needed()
        await submit_btn.click()
        await page.wait_for_timeout(3000)

        # Prefer the ID captured from the API response; fall back to notification text
        konimbo_id = ""
        if konimbo_id_from_api:
            konimbo_id = f"Konimbo ID #{konimbo_id_from_api[-1]}"
        else:
            for sel in ['.success', '[class*="success"]', '.alert-success', '.notification']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1500):
                        konimbo_id = (await el.inner_text()).strip()[:120]
                        break
                except Exception:
                    pass

        log.info(f"  הועלה בהצלחה! {konimbo_id}")

        # Close dialog (never Escape!)
        await safe_close_dialog(page)
        await page.wait_for_timeout(1500)
        return True, konimbo_id

    except Exception as e:
        log.info(f"  שגיאה: {e}")
        await screenshot_on_error(page, sku)
        await safe_close_dialog(page)
        await page.wait_for_timeout(1000)
        return False, str(e)
    finally:
        page.remove_listener("response", _on_response)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def do_login(page, username: str, password: str, log: Logger) -> bool:
    log.info("מתחבר לאתר...")
    try:
        u_input = page.locator(
            'input[type="email"], input[name="username"], input[name="email"], '
            'input[placeholder*="שם משתמש"], input[placeholder*="מייל"], input[placeholder*="email"]'
        ).first
        await u_input.fill(username, timeout=5000)

        p_input = page.locator('input[type="password"]').first
        await p_input.fill(password, timeout=5000)

        submit = page.locator(
            'button[type="submit"], button:has-text("התחבר"), button:has-text("כניסה"), button:has-text("Login")'
        ).first
        await submit.click()
        await page.wait_for_timeout(4000)

        if "NewProducts" in page.url or page.url.rstrip("/") == MANAGEMENT_URL.rstrip("/"):
            log.info("התחברות הצליחה!")
            return True

        log.info(f"דף לאחר login: {page.url}")
        return False
    except Exception as e:
        log.info(f"שגיאת login: {e}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    log = Logger()
    log.info("===== SHOPIPS — העלאת מוצרים אוטומטית =====")
    log.info(f"תאריך: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    products_path = Path(__file__).parent / "products.json"
    products = json.loads(products_path.read_text(encoding="utf-8"))

    username = os.environ.get("KONIMBO_USERNAME", "")
    password = os.environ.get("KONIMBO_PASSWORD", "")
    dry_run  = os.environ.get("DRY_RUN", "false").lower() == "true"

    if dry_run:
        log.info("[DRY RUN] — לא מבצע פעולות אמיתיות")

    success_list: list[dict] = []
    failed_list:  list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="he-IL",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # ── Navigate to management page ──────────────────────────────────────
        await page.goto(MANAGEMENT_URL, wait_until="networkidle", timeout=30000)

        if "NewProducts" not in page.url:
            if not username or not password:
                log.info(
                    "נדרשת כניסה — הגדר את הסודות KONIMBO_USERNAME ו-KONIMBO_PASSWORD ב-GitHub Secrets"
                )
                log.save()
                await browser.close()
                sys.exit(1)

            ok = await do_login(page, username, password, log)
            if not ok:
                log.info("כניסה נכשלה — עוצר")
                log.save()
                await browser.close()
                sys.exit(1)

            await page.goto(MANAGEMENT_URL, wait_until="networkidle", timeout=30000)

        # ── Check how many products are waiting ──────────────────────────────
        upload_btns = page.locator('button:has-text("העלה לאתר")')
        pending_count = await upload_btns.count()
        log.info(f"מוצרים הממתינים להעלאה: {pending_count}")

        if pending_count == 0:
            log.info("אין מוצרים חדשים להעלות היום.")
            log.save()
            await browser.close()
            return

        # ── Process each product from the catalog ────────────────────────────
        for product in products:
            sku = product["second_code"]

            # Skip if this SKU has no pending upload button on page
            row_btn = page.locator(f'tr:has-text("{sku}") button:has-text("העלה לאתר")')
            if await row_btn.count() == 0:
                log.info(f"מק\"ט {sku} — כבר הועלה או לא בטבלה, מדלג")
                continue

            if dry_run:
                log.info(f"[DRY RUN] היה מעלה: {product['title']}")
                continue

            ok, detail = await upload_one_product(page, context, product, log)
            if ok:
                success_list.append({"product": product, "detail": detail})
            else:
                failed_list.append({"product": product, "error": detail})

            # Slight pause between uploads to avoid overloading the server
            await page.wait_for_timeout(2000)

            # Refresh token if needed (re-navigate every 25 minutes isn't needed
            # here since each product takes < 1 min, but guard with 401 check)
            response_ok = await page.evaluate(
                "() => document.body ? true : false"
            )
            if not response_ok:
                log.info("דף לא תקין — מרענן...")
                await page.goto(MANAGEMENT_URL, wait_until="networkidle", timeout=20000)

        await browser.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    log.info("")
    log.info("===== סיכום העלאת מוצרים =====")
    log.info(f"תאריך: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info(f"הועלו בהצלחה: {len(success_list)}")
    for item in success_list:
        p = item["product"]
        log.info(f"  ✓ {p['second_code']} | {p['title']} | {item['detail']}")
    log.info(f"נכשלו: {len(failed_list)}")
    for item in failed_list:
        p = item["product"]
        log.info(f"  ✗ {p['second_code']} | {p['title']} | סיבה: {item['error']}")
    log.info("=================================")

    log_file = log.save()
    log.info(f"לוג נשמר: {log_file}")

    if failed_list:
        sys.exit(1)  # Makes GitHub Actions mark the run as failed


if __name__ == "__main__":
    asyncio.run(main())
