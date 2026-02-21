"""Config flow for Wrist Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class WristAssistantConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wrist Assistant."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Wrist Assistant",
                data={"initial_setup_done": True},
            )

        return self.async_show_form(step_id="user")
