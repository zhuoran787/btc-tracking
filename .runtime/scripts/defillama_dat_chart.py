"""Refresh the weekly DAT chart from DefiLlama during a manual report build."""
import base64
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import struct
from tempfile import NamedTemporaryFile, TemporaryDirectory


URL = "https://defillama.com/digital-asset-treasuries"
TITLE = "Digital asset treasury inflows by asset"


def export_weekly_png(directory):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        response = page.goto(URL, wait_until="domcontentloaded", timeout=45_000)
        if not response or not response.ok:
            raise RuntimeError(f"DefiLlama returned HTTP {response.status if response else 'unknown'}")
        page.get_by_role("heading", name=TITLE, exact=True).wait_for()
        weekly = page.get_by_role("radio", name="Weekly", exact=True)
        if not weekly.is_checked():
            weekly.check()
        page.locator("canvas").first.wait_for(state="visible")
        page.get_by_role("button", name=".png", exact=True).click()
        with page.expect_download() as download:
            page.get_by_text("Download image", exact=True).click()
        output = directory / Path(download.value.suggested_filename).name
        download.value.save_as(output)
        return output


def refresh_weekly_chart(directory, supplied_png=None):
    with TemporaryDirectory() as temp:
        image = Path(supplied_png) if supplied_png else export_weekly_png(Path(temp))
        name = re.fullmatch(r"digital-asset-treasuries-inflows-by-asset_(\d{4}-\d{2}-\d{2})(?: \(\d+\))?\.png", image.name)
        allowed_dates = {date.today().isoformat(), datetime.now(timezone.utc).date().isoformat()}
        if not name or name[1] not in allowed_dates:
            raise ValueError("Expected today's original DefiLlama weekly chart export")
        png = image.read_bytes()
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or png[12:16] != b"IHDR" or b"IEND" not in png[-12:]:
            raise ValueError("Invalid or incomplete PNG")
        width, height = struct.unpack(">II", png[16:24])
        if min(width, height) < 200:
            raise ValueError("Exported image is too small for the DAT chart")
        snapshot = {"source": URL, "export_date": name[1], "image": "data:image/png;base64," + base64.b64encode(png).decode()}
        with NamedTemporaryFile(mode="w", dir=directory, delete=False) as f:
            json.dump(snapshot, f)
            f.write("\n")
        Path(f.name).replace(directory / "defillama_dat_chart.json")
