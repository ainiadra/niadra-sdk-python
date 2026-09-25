from __future__ import annotations

from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from niadra import AuthenticationError, Niadra, NiadraError, PermissionDeniedError


class NiadraProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        """Checks the source key against the API with a read that needs no customer."""
        api_key = str(credentials.get("api_key") or "").strip()
        if not api_key:
            raise ToolProviderCredentialValidationError("The Niadra source key is required.")
        base_url = str(credentials.get("base_url") or "").strip() or None
        try:
            client = Niadra(api_key, base_url=base_url, strict=True)
        except NiadraError as exc:
            raise ToolProviderCredentialValidationError(
                f"Not a Niadra source key ({type(exc).__name__})."
            ) from exc
        try:
            client.agent_memory()
        except PermissionDeniedError:
            return  # a valid key without the agent memory scope
        except AuthenticationError as exc:
            raise ToolProviderCredentialValidationError("Niadra refused the source key.") from exc
        except NiadraError as exc:
            raise ToolProviderCredentialValidationError(
                f"Could not reach Niadra ({type(exc).__name__})."
            ) from exc
        finally:
            client.close()
