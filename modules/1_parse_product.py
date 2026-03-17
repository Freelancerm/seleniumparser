"""
Single-file Selenium parser for Brain.com.ua.

Workflow:
1. Open the home page.
2. Enter the search query.
3. Click the search button.
4. Open the first product result.
5. Parse product data from the product page.
6. Save the product into Django database.
7. Print parsed data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

import load_django
from parser_app.models import Product

HOME_URL = "https://brain.com.ua/"
SEARCH_QUERY = "Apple iPhone 15 128GB Black"

DEFAULT_TEXT: Optional[str] = None
DEFAULT_PRICE: Optional[Decimal] = None
WAIT_TIMEOUT = 25

CHAR_COLOR = "Колір"
CHAR_MEMORY = "Вбудована пам'ять"
CHAR_MANUFACTURER = "Виробник"
CHAR_SCREEN_SIZE = "Діагональ екрану"
CHAR_SCREEN_RESOLUTION = "Роздільна здатність екрану"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class ProductData:
    """Structured DTO for parsed product data."""

    name: Optional[str] = DEFAULT_TEXT
    color: Optional[str] = DEFAULT_TEXT
    memory: Optional[str] = DEFAULT_TEXT
    manufacturer: Optional[str] = DEFAULT_TEXT
    price: Optional[Decimal] = DEFAULT_PRICE
    price_discount: Optional[Decimal] = DEFAULT_PRICE
    photos: Optional[list[str]] = None
    goods_code: Optional[str] = None
    reviews_count: Optional[int] = None
    screen_size: Optional[str] = DEFAULT_TEXT
    screen_resolution: Optional[str] = DEFAULT_TEXT
    characteristics: Optional[dict[str, str]] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert parsed product data to dictionary."""
        return asdict(self)


def clean_text(
    value: Optional[str], default: Optional[str] = DEFAULT_TEXT
) -> Optional[str]:
    """Normalize whitespace and return default if the value is empty."""
    if not value:
        return default
    normalized = " ".join(value.replace("\xa0", " ").split())
    return normalized if normalized else default


def to_decimal(
    value: Optional[str], default: Optional[Decimal] = DEFAULT_PRICE
) -> Optional[Decimal]:
    """Convert a price-like string to Decimal."""
    if not value:
        return default

    cleaned = (
        value.replace("₴", "")
        .replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
        .strip()
    )

    if not cleaned:
        return default

    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return default


def deduplicate_preserve_order(values: list[str]) -> list[str]:
    """Remove duplicates while preserving order."""
    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)

    return result


def create_driver() -> webdriver.Chrome:
    """Create and configure Chrome WebDriver."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(service=Service(), options=options)
    driver.set_page_load_timeout(60)
    return driver


class BrainSearchNavigator:
    """Navigate Brain.com.ua search flow with Selenium."""

    SEARCH_INPUT = ".header-bottom .quick-search-input"
    SEARCH_BUTTON = 'input.qsr-submit[type="submit"]'
    PRODUCT_WRAPPER = ".product-wrapper"
    FIRST_PRODUCT_LINK = ".br-pp-img.br-pp-img-grid a[href]"
    PRODUCT_NAME = ".fnp-product-name"

    def __init__(self, driver: WebDriver, wait: WebDriverWait) -> None:
        self.driver = driver
        self.wait = wait

    def open_first_product_from_search(self, home_url: str, query: str) -> None:
        """Open the home page, submit a search query, and open the first product result."""
        logger.info("Opening home page: %s", home_url)
        self.driver.get(home_url)

        search_input = self.wait.until(
            ec.presence_of_element_located((By.CSS_SELECTOR, self.SEARCH_INPUT))
        )
        search_input.clear()
        search_input.send_keys(query)
        logger.info("Entered search query: %s", query)

        search_button = self.wait.until(
            ec.element_to_be_clickable((By.CSS_SELECTOR, self.SEARCH_BUTTON))
        )
        search_button.click()
        logger.info("Clicked search button.")

        results = self.wait.until(
            ec.presence_of_all_elements_located((By.CSS_SELECTOR, self.PRODUCT_WRAPPER))
        )
        if not results:
            raise ValueError("Search results were not found.")

        first_result_link = results[0].find_element(
            By.CSS_SELECTOR, self.FIRST_PRODUCT_LINK
        )
        product_url = first_result_link.get_attribute("href")
        if not product_url:
            raise ValueError("The first product link does not contain href.")

        logger.info("Opening first product result: %s", product_url)
        self.driver.get(product_url)

        self.wait.until(
            ec.presence_of_element_located((By.CSS_SELECTOR, self.PRODUCT_NAME))
        )


class BrainProductParser:
    """Parse Brain.com.ua product fields from the current product page using DOM only."""

    PRODUCT_NAME = ".fnp-product-name"
    MAIN_RIGHT_BLOCK = ".main-right-block"
    CHARACTERISTICS_ROOT = "#br-pr-7"
    CHARACTERISTICS_ITEMS = ".br-pr-chr-item"
    CHARACTERISTICS_BUTTON = "#br-pr-7 .br-prs-button"
    REVIEWS_LINK = 'a.scroll-to-element[href="#reviews-list"]'
    PRODUCT_CODE_BLOCK = "#product_code"
    PRODUCT_CODE_VALUE = "span.br-pr-code-val"

    PRICE_SELECTORS = [
        ".br-pr-op .price-wrapper > span",
        ".br-pr-op .price-wrapper",
        ".price-wrapper > span",
        ".price-wrapper",
        ".price",
    ]
    DISCOUNT_PRICE_SELECTORS = [
        ".red-price",
        ".br-pr-np .red-price",
        ".old-price span",
    ]
    PHOTO_SELECTORS = [
        ".main-left-block .product-block-bottom img[src]",
        ".product-block-bottom img[src]",
        ".br-pr-sf img[src]",
        ".slick-slide img[src]",
    ]

    def __init__(self, driver: WebDriver, wait: WebDriverWait) -> None:
        self.driver = driver
        self.wait = wait

    def parse(self) -> ProductData:
        """Parse the full product data from the current product page."""
        self.wait.until(
            ec.presence_of_element_located((By.CSS_SELECTOR, self.PRODUCT_NAME))
        )

        self._expand_characteristics_if_needed()

        goods_code = self._parse_goods_code()
        characteristics = self._parse_characteristics()

        return ProductData(
            name=self._parse_name(),
            color=characteristics.get(CHAR_COLOR) if characteristics else None,
            memory=characteristics.get(CHAR_MEMORY) if characteristics else None,
            manufacturer=characteristics.get(CHAR_MANUFACTURER)
            if characteristics
            else None,
            price=self._parse_price(),
            price_discount=self._parse_price_discount(),
            photos=self._parse_photos(goods_code),
            goods_code=goods_code,
            reviews_count=self._parse_reviews_count(),
            screen_size=characteristics.get(CHAR_SCREEN_SIZE)
            if characteristics
            else None,
            screen_resolution=characteristics.get(CHAR_SCREEN_RESOLUTION)
            if characteristics
            else None,
            characteristics=characteristics,
        )

    def _find_optional(
        self,
        selector: str,
        parent: Optional[WebElement] = None,
    ) -> Optional[WebElement]:
        """Find one element by CSS selector and return None if it does not exist."""
        search_root = parent or self.driver
        try:
            return search_root.find_element(By.CSS_SELECTOR, selector)
        except NoSuchElementException:
            return None

    @staticmethod
    def _element_text(
        element: Optional[WebElement], default: Optional[str] = None
    ) -> Optional[str]:
        """Return normalized visible text from an element."""
        if element is None:
            return default
        return clean_text(element.text, default=default)

    @staticmethod
    def _element_text_content(
        element: Optional[WebElement], default: Optional[str] = None
    ) -> Optional[str]:
        """Return normalized textContent from an element."""
        if element is None:
            return default
        return clean_text(element.get_attribute("textContent"), default=default)

    def _get_text_by_selectors(
        self,
        selectors: list[str],
        default: Optional[str] = DEFAULT_TEXT,
        parent: Optional[WebElement] = None,
    ) -> Optional[str]:
        """Return the first non-empty text found by the provided CSS selectors."""
        for selector in selectors:
            element = self._find_optional(selector, parent=parent)
            text = self._element_text(element, default=None)
            if text:
                return text
        return default

    def _get_decimal_by_selectors(
        self,
        selectors: list[str],
        parent: Optional[WebElement] = None,
    ) -> Optional[Decimal]:
        """Return the first decimal parsed from the provided CSS selectors."""
        raw_value = self._get_text_by_selectors(selectors, default=None, parent=parent)
        return to_decimal(raw_value, default=None)

    def _expand_characteristics_if_needed(self) -> None:
        """Expand the characteristics section if the expand button is present."""
        button = self._find_optional(self.CHARACTERISTICS_BUTTON)
        if button is None:
            return

        button_text = self._element_text(button, default=None) or ""
        if "Всі характеристики" not in button_text:
            return

        logger.info("Expanding characteristics section.")
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", button
        )
        self.driver.execute_script("arguments[0].click();", button)

        self.wait.until(
            lambda driver: "Всі характеристики"
            not in clean_text(
                driver.find_element(By.CSS_SELECTOR, self.CHARACTERISTICS_BUTTON).text,
                default=None,
            )
        )

    def _parse_name(self) -> Optional[str]:
        """Parse product full name."""
        return self._get_text_by_selectors([self.PRODUCT_NAME], default=None)

    def _parse_price(self) -> Optional[Decimal]:
        """Parse regular price from the main right block."""
        container = self._find_optional(self.MAIN_RIGHT_BLOCK)
        if container is None:
            logger.warning("Regular price container not found.")
            return None

        price = self._get_decimal_by_selectors(self.PRICE_SELECTORS, parent=container)
        if price is None:
            logger.warning("Regular price not found.")
        return price

    def _parse_price_discount(self) -> Optional[Decimal]:
        """Parse old price shown before discount."""
        container = self._find_optional(self.MAIN_RIGHT_BLOCK)
        if container is None:
            return None

        return self._get_decimal_by_selectors(
            self.DISCOUNT_PRICE_SELECTORS, parent=container
        )

    def _parse_goods_code(self) -> Optional[str]:
        """Parse product code from the dedicated product code block."""
        try:
            product_code_block = WebDriverWait(self.driver, 10).until(
                ec.presence_of_element_located(
                    (By.CSS_SELECTOR, self.PRODUCT_CODE_BLOCK)
                )
            )
            value_element = product_code_block.find_element(
                By.CSS_SELECTOR, self.PRODUCT_CODE_VALUE
            )
            goods_code = self._element_text_content(value_element, default=None)
            if goods_code:
                return goods_code
        except (TimeoutException, NoSuchElementException):
            pass

        logger.warning("Product code not found.")
        return None

    def _parse_reviews_count(self) -> Optional[int]:
        """Parse reviews count from the reviews link span."""
        for link in self.driver.find_elements(By.CSS_SELECTOR, self.REVIEWS_LINK):
            try:
                raw_value = clean_text(
                    link.find_element(By.TAG_NAME, "span").text, default=None
                )
                if raw_value:
                    return int(raw_value)
            except (NoSuchElementException, ValueError):
                continue
        return None

    def _parse_photos(self, goods_code: Optional[str]) -> Optional[list[str]]:
        """Parse unique product photo URLs from gallery DOM elements."""
        photos: list[str] = []

        for selector in self.PHOTO_SELECTORS:
            for image in self.driver.find_elements(By.CSS_SELECTOR, selector):
                src = (image.get_attribute("src") or "").strip()
                if not src:
                    continue
                if goods_code and goods_code not in src:
                    continue
                photos.append(src)

        if not photos:
            return None
        return deduplicate_preserve_order(photos)

    def _parse_characteristics(self) -> Optional[dict[str, str]]:
        """Parse all product characteristics as a flat key-value dictionary."""
        characteristics: dict[str, str] = {}

        root = self._find_optional(self.CHARACTERISTICS_ROOT)
        if root is None:
            logger.warning("Characteristics root block '#br-pr-7' not found.")
            return None

        items = root.find_elements(By.CSS_SELECTOR, self.CHARACTERISTICS_ITEMS)
        if not items:
            logger.warning("No characteristic groups found inside '#br-pr-7'.")
            return None

        for item in items:
            for row in item.find_elements(By.XPATH, "./div/div"):
                parsed_row = self._parse_characteristic_row(row)
                if parsed_row:
                    key, value = parsed_row
                    characteristics[key] = value

        return characteristics or None

    @staticmethod
    def _parse_characteristic_row(row: WebElement) -> Optional[tuple[str, str]]:
        """Parse one characteristic row into a (key, value) tuple."""
        spans = row.find_elements(By.XPATH, "./span")
        if len(spans) < 2:
            return None

        key = clean_text(spans[0].text, default=None)
        value = clean_text(spans[1].text, default=None)
        if not key:
            return None

        return key, value


def save_product(product_data: ProductData) -> Product:
    """Save product, avoiding duplicate goods_code unique constraint errors.

    Uses get_or_create to return existing product when a product with the
    same goods_code already exists, otherwise creates a new one.
    """
    if not product_data.goods_code:
        raise ValueError("Cannot save product without goods_code.")

    defaults = {
        "name": product_data.name,
        "color": product_data.color,
        "memory": product_data.memory,
        "manufacturer": product_data.manufacturer,
        "price": product_data.price,
        "price_discount": product_data.price_discount,
        "photos": product_data.photos,
        "reviews_count": product_data.reviews_count,
        "screen_size": product_data.screen_size,
        "screen_resolution": product_data.screen_resolution,
        "characteristics": product_data.characteristics,
    }

    product, created = Product.objects.get_or_create(
        goods_code=product_data.goods_code,
        defaults=defaults,
    )

    if not created:
        # Optionally update fields if parsed data is newer/complete — here we
        # update only when parsed value is not None to avoid overwriting with
        # empty values.
        updated = False
        for field, value in defaults.items():
            if value is not None and getattr(product, field) != value:
                setattr(product, field, value)
                updated = True
        if updated:
            product.save()
            logger.info("Updated existing product with goods_code=%s", product.goods_code)
        else:
            logger.info("Found existing product with goods_code=%s, no changes applied", product.goods_code)
    else:
        logger.info("Created product with goods_code=%s", product.goods_code)

    return product


class ProductParseService:
    """Orchestrate search, parse, and save flow."""

    def __init__(self) -> None:
        self.driver = create_driver()
        self.wait = WebDriverWait(self.driver, WAIT_TIMEOUT)
        self.navigator = BrainSearchNavigator(self.driver, self.wait)
        self.parser = BrainProductParser(self.driver, self.wait)

    def execute(self, home_url: str, query: str) -> ProductData:
        """Run the full workflow."""
        try:
            self.navigator.open_first_product_from_search(home_url, query)
            product_data = self.parser.parse()
            save_product(product_data)
            return product_data
        finally:
            logger.info("Closing browser.")
            self.driver.quit()


def main() -> None:
    """Application entry point."""
    service = ProductParseService()

    try:
        product_data = service.execute(HOME_URL, SEARCH_QUERY)
    except TimeoutException as exc:
        logger.exception("Timeout while interacting with the website: %s", exc)
        raise
    except WebDriverException as exc:
        logger.exception("Selenium WebDriver error: %s", exc)
        raise

    print(json.dumps(product_data.to_dict(), ensure_ascii=False, indent=4, default=str))


if __name__ == "__main__":
    main()
