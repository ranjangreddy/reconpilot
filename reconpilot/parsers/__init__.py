"""PSP parsers package."""
from .adyen import AdyenParser
from .stripe import StripeParser
from .shift4 import Shift4Parser

PARSERS = {
    "adyen": AdyenParser,
    "stripe": StripeParser,
    "shift4": Shift4Parser,
}

__all__ = ["AdyenParser", "StripeParser", "Shift4Parser", "PARSERS"]
