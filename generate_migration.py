import os
import time

migration_content = """\
\"\"\"Add gifts v2 domain

Revision ID: 5eb04c0548be
Revises: 4db04c0548bd
Create Date: 2026-09-16 02:00:00.000000

\"\"\"
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '5eb04c0548be'
down_revision = '4db04c0548bd'
branch_labels = None
depends_on = None

def upgrade():
    # Gift voucher carts
    op.create_table(
        'gift_voucher_carts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('gift_type', sa.String(20), nullable=False, default='DIGITAL', index=True),
        sa.Column('status', sa.String(20), nullable=False, default='ACTIVE', index=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index('ix_gift_carts_customer_status', 'gift_voucher_carts', ['customer_id', 'status'])

    # Gift voucher cart items
    op.create_table(
        'gift_voucher_cart_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('cart_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gift_voucher_carts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('gift_type', sa.String(20), nullable=False, index=True),
        sa.Column('digital_gift_id', postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column('digital_gift_data', postgresql.JSONB, nullable=True),
        sa.Column('service_id', postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column('service_data', postgresql.JSONB, nullable=True),
        sa.Column('branch_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('branch_data', postgresql.JSONB, nullable=True),
        sa.Column('service_arrangement_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('service_arrangement_data', postgresql.JSONB, nullable=True),
        sa.Column('addons', postgresql.JSONB, nullable=True),
        sa.Column('extra_minutes', sa.Integer, nullable=False, default=0),
        sa.Column('price_for_extra_minutes', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('selected_video_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('custom_video_url', sa.String(1000), nullable=True),
        sa.Column('product_id', postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column('product_data', postgresql.JSONB, nullable=True),
        sa.Column('quantity', sa.Integer, nullable=False, default=1),
        sa.Column('unit_price', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('subtotal', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index('ix_gift_cart_items_cart', 'gift_voucher_cart_items', ['cart_id'])

    # Gift voucher purchases
    op.create_table(
        'gift_voucher_purchases',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('gift_type', sa.String(20), nullable=False, index=True),
        sa.Column('digital_gift_id', postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column('digital_gift_data', postgresql.JSONB, nullable=True),
        sa.Column('status', sa.String(30), nullable=False, default='PENDING_PAYMENT', index=True),
        sa.Column('expire_date', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('sender_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('sender_data', postgresql.JSONB, nullable=True),
        sa.Column('recipient_phone', sa.String(50), nullable=True, index=True),
        sa.Column('recipient_id', postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column('recipient_data', postgresql.JSONB, nullable=True),
        sa.Column('recipient_language', sa.String(10), nullable=False, default='ar'),
        sa.Column('total_amount', sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, default='KWD'),
        sa.Column('gift_message', sa.Text, nullable=True),
        sa.Column('gift_template', sa.String(100), nullable=True),
        sa.Column('secret_code_hash', sa.String(200), nullable=True),
        sa.Column('public_token', sa.String(64), nullable=False, unique=True),
        sa.Column('payment_id', sa.String(100), nullable=True, index=True),
        sa.Column('payment_data', postgresql.JSONB, nullable=True),
        sa.Column('payment_url', sa.Text, nullable=True),
        sa.Column('payment_provider', sa.String(20), nullable=True),
        sa.Column('payment_through', sa.String(10), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('redeemed_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index('ix_gift_purchases_sender_status', 'gift_voucher_purchases', ['sender_id', 'status'])

    # Gift voucher purchase items
    op.create_table(
        'gift_voucher_purchase_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('purchase_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gift_voucher_purchases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('gift_type', sa.String(20), nullable=False),
        sa.Column('service_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('service_data', postgresql.JSONB, nullable=True),
        sa.Column('branch_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('branch_data', postgresql.JSONB, nullable=True),
        sa.Column('service_arrangement_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('service_arrangement_data', postgresql.JSONB, nullable=True),
        sa.Column('addons', postgresql.JSONB, nullable=True),
        sa.Column('extra_minutes', sa.Integer, nullable=False, default=0),
        sa.Column('price_for_extra_minutes', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('total_duration', sa.Integer, nullable=False, default=0),
        sa.Column('selected_video_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('custom_video_url', sa.String(1000), nullable=True),
        sa.Column('product_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('product_data', postgresql.JSONB, nullable=True),
        sa.Column('quantity', sa.Integer, nullable=False, default=1),
        sa.Column('unit_price', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('subtotal', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Gift voucher recipients
    op.create_table(
        'gift_voucher_recipients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('purchase_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gift_voucher_purchases.id', ondelete='CASCADE'), nullable=False, unique=True, index=True),
        sa.Column('name', sa.String(200), nullable=True),
        sa.Column('phone_number', sa.String(50), nullable=True, index=True),
        sa.Column('message', sa.Text, nullable=True),
        sa.Column('language', sa.String(10), nullable=False, default='ar'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Gift voucher deliveries
    op.create_table(
        'gift_voucher_deliveries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('purchase_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gift_voucher_purchases.id', ondelete='CASCADE'), nullable=False, unique=True, index=True),
        sa.Column('address', postgresql.JSONB, nullable=True),
        sa.Column('status', sa.String(30), nullable=False, default='PROCESSING', index=True),
        sa.Column('tracking_reference', sa.String(200), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Gift voucher verifications
    op.create_table(
        'gift_voucher_verifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('purchase_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gift_voucher_purchases.id', ondelete='CASCADE'), nullable=False, unique=True, index=True),
        sa.Column('attempt_count', sa.Integer, nullable=False, default=0),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Gift voucher redemptions
    op.create_table(
        'gift_voucher_redemptions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('purchase_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gift_voucher_purchases.id', ondelete='CASCADE'), nullable=False, unique=True, index=True),
        sa.Column('redeemed_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('booking_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('booking_data', postgresql.JSONB, nullable=True),
        sa.Column('note', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint('uq_gift_redemptions_purchase', 'gift_voucher_redemptions', ['purchase_id'])

    # Gift voucher status history
    op.create_table(
        'gift_voucher_status_history',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('purchase_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gift_voucher_purchases.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('from_status', sa.String(30), nullable=True),
        sa.Column('to_status', sa.String(30), nullable=False),
        sa.Column('changed_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reason', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )

    # Add gift_type to gift_vouchers
    op.add_column('gift_vouchers', sa.Column('gift_type', sa.String(length=20), server_default='SERVICE', nullable=True))
    op.create_index('ix_gift_vouchers_gift_type', 'gift_vouchers', ['gift_type'], unique=False)

def downgrade():
    op.drop_index('ix_gift_vouchers_gift_type', table_name='gift_vouchers')
    op.drop_column('gift_vouchers', 'gift_type')
    
    op.drop_table('gift_voucher_status_history')
    op.drop_table('gift_voucher_redemptions')
    op.drop_table('gift_voucher_verifications')
    op.drop_table('gift_voucher_deliveries')
    op.drop_table('gift_voucher_recipients')
    op.drop_table('gift_voucher_purchase_items')
    op.drop_table('gift_voucher_purchases')
    op.drop_table('gift_voucher_cart_items')
    op.drop_table('gift_voucher_carts')
"""

with open('/Users/charlicoder/Documents/projects/live/ushspa_projects/microservices/ushbooknpay/alembic/versions/5eb04c0548be_add_gifts_v2_domain.py', 'w') as f:
    f.write(migration_content)

