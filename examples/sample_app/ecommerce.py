"""
Sample e-commerce application used to demonstrate FlowDelta.

Contains three distinct flows:
  1. user-registration   – create account, verify email
  2. product-checkout    – add to cart, apply coupon, process payment
  3. order-tracking      – look up order, update status, send notification
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

@dataclass
class User:
    email: str
    password_hash: str
    user_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    verified: bool = False
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class Product:
    product_id: str
    name: str
    price: float
    stock: int


@dataclass
class CartItem:
    product: Product
    quantity: int

    @property
    def subtotal(self) -> float:
        return round(self.product.price * self.quantity, 2)


@dataclass
class Cart:
    user_id: str
    items: List[CartItem] = field(default_factory=list)
    coupon_code: Optional[str] = None
    discount: float = 0.0

    @property
    def total(self) -> float:
        subtotal = sum(i.subtotal for i in self.items)
        return round(subtotal - self.discount, 2)


@dataclass
class Order:
    order_id: str
    user_id: str
    items: List[CartItem]
    total: float
    status: str = "pending"
    payment_ref: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ---------------------------------------------------------------------------
# In-memory "database"
# ---------------------------------------------------------------------------

_users: Dict[str, User] = {}
_products: Dict[str, Product] = {
    "p001": Product("p001", "Laptop",   999.99, 5),
    "p002": Product("p002", "Headphones", 49.99, 20),
    "p003": Product("p003", "Mouse",     19.99, 50),
}
_orders: Dict[str, Order] = {}
_coupons = {"SAVE10": 10.0, "SUMMER20": 20.0}


# ---------------------------------------------------------------------------
# Flow 1 – User Registration
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_account(email: str, password: str) -> User:
    if email in _users:
        raise ValueError(f"Email already registered: {email}")
    user = User(email=email, password_hash=hash_password(password))
    _users[email] = user
    return user


def send_verification_email(user: User) -> str:
    """Simulate sending verification; returns a token."""
    token = hashlib.md5(user.email.encode()).hexdigest()[:6].upper()
    return token


def verify_email(user: User, token: str) -> bool:
    expected = hashlib.md5(user.email.encode()).hexdigest()[:6].upper()
    if token == expected:
        user.verified = True
        _users[user.email] = user
        return True
    return False


def register_user(email: str, password: str) -> User:
    """Entry point – Flow 1: user-registration."""
    user = create_account(email, password)
    token = send_verification_email(user)
    verify_email(user, token)
    return user


# ---------------------------------------------------------------------------
# Flow 2 – Product Checkout
# ---------------------------------------------------------------------------

def build_cart(user_id: str, product_quantities: Dict[str, int]) -> Cart:
    cart = Cart(user_id=user_id)
    for pid, qty in product_quantities.items():
        product = _products.get(pid)
        if not product:
            raise ValueError(f"Unknown product: {pid}")
        if product.stock < qty:
            raise ValueError(f"Insufficient stock for {pid}")
        cart.items.append(CartItem(product=product, quantity=qty))
    return cart


def apply_coupon(cart: Cart, coupon_code: str) -> Cart:
    discount = _coupons.get(coupon_code, 0.0)
    if discount == 0.0:
        raise ValueError(f"Invalid coupon: {coupon_code}")
    cart.coupon_code = coupon_code
    cart.discount = discount
    return cart


def process_payment(cart: Cart, card_last4: str) -> str:
    """Simulate payment processing; returns a payment reference."""
    if cart.total <= 0:
        raise ValueError("Cart total must be positive")
    payment_ref = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    # Deduct stock
    for item in cart.items:
        _products[item.product.product_id].stock -= item.quantity
    return payment_ref


def create_order(cart: Cart, payment_ref: str) -> Order:
    order = Order(
        order_id=f"ORD-{uuid.uuid4().hex[:6].upper()}",
        user_id=cart.user_id,
        items=list(cart.items),
        total=cart.total,
        payment_ref=payment_ref,
        status="confirmed",
    )
    _orders[order.order_id] = order
    return order


def checkout(user_id: str, product_quantities: Dict[str, int], coupon: Optional[str] = None) -> Order:
    """Entry point – Flow 2: product-checkout."""
    cart = build_cart(user_id, product_quantities)
    if coupon:
        cart = apply_coupon(cart, coupon)
    payment_ref = process_payment(cart, card_last4="4242")
    order = create_order(cart, payment_ref)
    return order


# ---------------------------------------------------------------------------
# Flow 3 – Order Tracking
# ---------------------------------------------------------------------------

def lookup_order(order_id: str) -> Order:
    order = _orders.get(order_id)
    if not order:
        raise ValueError(f"Order not found: {order_id}")
    return order


def update_order_status(order: Order, new_status: str) -> Order:
    valid_transitions = {
        "confirmed": ["shipped"],
        "shipped": ["delivered"],
        "delivered": ["closed"],
    }
    allowed = valid_transitions.get(order.status, [])
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {order.status} → {new_status}. Allowed: {allowed}"
        )
    order.status = new_status
    order.updated_at = datetime.utcnow().isoformat()
    _orders[order.order_id] = order
    return order


def send_status_notification(order: Order) -> dict:
    """Simulate sending an email/SMS notification."""
    return {
        "recipient": order.user_id,
        "message": f"Your order {order.order_id} is now {order.status}.",
        "sent": True,
    }


def track_order(order_id: str, new_status: str) -> dict:
    """Entry point – Flow 3: order-tracking."""
    order = lookup_order(order_id)
    order = update_order_status(order, new_status)
    notification = send_status_notification(order)
    return {"order": order, "notification": notification}
