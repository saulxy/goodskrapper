"""GoodSkrapper - ShopGoodwill -> eBay flip finder."""
__version__ = "1.0.0"

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

