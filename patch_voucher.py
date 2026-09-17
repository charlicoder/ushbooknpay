import re

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/voucher/infrastructure/models.py', 'r') as f:
    content = f.read()

pattern = r'(sender_id: Mapped\[uuid.UUID\] = mapped_column\(UUID\(as_uuid=True\), nullable=False\))'
replacement = r"""\1

    # ── Gift type discriminator ────────────────────────────────────────────
    # Added in Gift V2. Defaults to SERVICE for backward compatibility.
    # New purchases use DIGITAL, PHYSICAL, or SERVICE.
    gift_type: Mapped[str] = mapped_column(
        String(20), nullable=True, default="SERVICE", index=True
    )
"""

content = re.sub(pattern, replacement, content)

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/app/voucher/infrastructure/models.py', 'w') as f:
    f.write(content)

