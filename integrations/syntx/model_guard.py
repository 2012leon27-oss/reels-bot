"""Model selection and verification helpers for the SyntX web UI."""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from integrations.syntx.config import SyntXSelectors
from integrations.syntx.errors import ModelMismatchError

logger = logging.getLogger(__name__)


class PlaywrightPage(Protocol):
    async def locator(self, selector: str) -> Any: ...

    async def wait_for_selector(self, selector: str, **kwargs: Any) -> Any: ...


class ModelGuard:
    """Ensures the active SyntX chat uses the expected Claude model."""

    def __init__(self, selectors: SyntXSelectors) -> None:
        self.selectors = selectors

    @staticmethod
    def normalize_model_name(name: str) -> str:
        return re.sub(r"\s+", " ", name.strip().lower())

    def model_matches(self, displayed: str, expected: str) -> bool:
        normalized_display = self.normalize_model_name(displayed)
        normalized_expected = self.normalize_model_name(expected)
        return normalized_expected in normalized_display

    async def read_displayed_model(self, page: Any) -> str:
        locator = page.locator(self.selectors.model_display).first
        if await locator.count() == 0:
            return ""
        return (await locator.inner_text()).strip()

    async def select_model(self, page: Any, model_name: str) -> None:
        selector_button = page.locator(self.selectors.model_selector).first
        await selector_button.wait_for(state="visible", timeout=30_000)
        await selector_button.click()

        option = page.get_by_role("option", name=model_name)
        if await option.count() > 0:
            await option.first.click()
            return

        option = page.locator(f"text={model_name}").first
        await option.wait_for(state="visible", timeout=15_000)
        await option.click()

    async def ensure_model(
        self,
        page: Any,
        model_name: str,
        *,
        strict_model: bool,
    ) -> bool:
        displayed = await self.read_displayed_model(page)
        if self.model_matches(displayed, model_name):
            return True

        logger.info(
            "Displayed model %r does not match %r; attempting selection",
            displayed,
            model_name,
        )
        await self.select_model(page, model_name)
        displayed = await self.read_displayed_model(page)
        ok = self.model_matches(displayed, model_name)
        if not ok and strict_model:
            raise ModelMismatchError(
                f"Expected model containing {model_name!r}, got {displayed!r}"
            )
        return ok
