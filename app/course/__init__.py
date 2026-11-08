"""
app/course
──────────
Course subscriptions: a customer buys a prepaid bundle of sessions of ONE service
in ONE room type (the course itself is defined in ushauth). This package owns the
subscription balance, purchase / payment / invoice, redemption (reserve → consume
or release) and expiry.

Layout follows the other domains: ``domain`` (pure rules, no I/O),
``infrastructure`` (ORM + repository), ``application`` (use cases + background
tasks) and ``api`` (FastAPI router + schemas).
"""

