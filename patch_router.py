import re

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/api/v1/router.py', 'r') as f:
    content = f.read()

content = content.replace(
    "from app.api.v1 import availability, bookings, payments",
    "from app.api.v1 import availability, bookings, payments\nfrom app.gifts.api.router import router as gifts_router"
)

content = content.replace(
    "api_router.include_router(shop_router)",
    "api_router.include_router(shop_router)\napi_router.include_router(gifts_router)"
)

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/api/v1/router.py', 'w') as f:
    f.write(content)

