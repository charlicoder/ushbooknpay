"""app/payment_links — provider-agnostic payment links, webhooks and refunds."""

from app.payment_links.domain.registry import get_gateway  # noqa: F401  (public API)

# Importing the adapters registers them in the gateway registry.
from app.payment_links.infrastructure import deema_gateway as _deema  # noqa: F401
from app.payment_links.infrastructure import myfatoorah_gateway as _myfatoorah  # noqa: F401
from app.payment_links.infrastructure import tap_gateway as _tap  # noqa: F401
