/**
 * ecommerce.js – Sample e-commerce application (JavaScript / ES2020)
 *
 * Mirrors ecommerce.py with the same three distinct application flows:
 *   1. registerUser   – create account, verify email
 *   2. checkout       – build cart, apply coupon, process payment, create order
 *   3. trackOrder     – look up order, update status, send notification
 *
 * Used by FlowDelta to demonstrate AST analysis and flow identification
 * on JavaScript source code.
 */

'use strict';

const crypto = require('crypto');

// ---------------------------------------------------------------------------
// Domain models
// ---------------------------------------------------------------------------

class User {
  constructor(email, passwordHash) {
    this.userId = crypto.randomBytes(4).toString('hex');
    this.email = email;
    this.passwordHash = passwordHash;
    this.verified = false;
    this.createdAt = new Date().toISOString();
  }
}

class Product {
  constructor(productId, name, price, stock) {
    this.productId = productId;
    this.name = name;
    this.price = price;
    this.stock = stock;
  }
}

class CartItem {
  constructor(product, quantity) {
    this.product = product;
    this.quantity = quantity;
  }

  get subtotal() {
    return Math.round(this.product.price * this.quantity * 100) / 100;
  }
}

class Cart {
  constructor(userId) {
    this.userId = userId;
    this.items = [];
    this.couponCode = null;
    this.discount = 0;
  }

  get total() {
    const subtotal = this.items.reduce((sum, i) => sum + i.subtotal, 0);
    return Math.round((subtotal - this.discount) * 100) / 100;
  }
}

class Order {
  constructor(orderId, userId, items, total) {
    this.orderId = orderId;
    this.userId = userId;
    this.items = items;
    this.total = total;
    this.status = 'pending';
    this.paymentRef = null;
    this.updatedAt = new Date().toISOString();
  }
}

// ---------------------------------------------------------------------------
// In-memory "database"
// ---------------------------------------------------------------------------

const _users = {};
const _products = {
  p001: new Product('p001', 'Laptop',     999.99, 5),
  p002: new Product('p002', 'Headphones',  49.99, 20),
  p003: new Product('p003', 'Mouse',       19.99, 50),
};
const _orders = {};
const _coupons = { SAVE10: 10, SUMMER20: 20 };

// ---------------------------------------------------------------------------
// Flow 1 – User Registration
// ---------------------------------------------------------------------------

function hashPassword(password) {
  return crypto.createHash('sha256').update(password).digest('hex');
}

function createAccount(email, password) {
  if (_users[email]) {
    throw new Error(`Email already registered: ${email}`);
  }
  const user = new User(email, hashPassword(password));
  _users[email] = user;
  return user;
}

function sendVerificationEmail(user) {
  // Simulate sending – returns a 6-char token
  const token = crypto.createHash('md5').update(user.email).digest('hex').slice(0, 6).toUpperCase();
  return token;
}

function verifyEmail(user, token) {
  const expected = crypto.createHash('md5').update(user.email).digest('hex').slice(0, 6).toUpperCase();
  if (token === expected) {
    user.verified = true;
    _users[user.email] = user;
    return true;
  }
  return false;
}

function registerUser(email, password) {
  const user = createAccount(email, password);
  const token = sendVerificationEmail(user);
  verifyEmail(user, token);
  return user;
}

// ---------------------------------------------------------------------------
// Flow 2 – Product Checkout
// ---------------------------------------------------------------------------

function buildCart(userId, productQuantities) {
  const cart = new Cart(userId);
  for (const [pid, qty] of Object.entries(productQuantities)) {
    const product = _products[pid];
    if (!product) throw new Error(`Unknown product: ${pid}`);
    if (product.stock < qty) throw new Error(`Insufficient stock for ${pid}`);
    cart.items.push(new CartItem(product, qty));
  }
  return cart;
}

function applyCoupon(cart, couponCode) {
  const discount = _coupons[couponCode];
  if (discount === undefined) throw new Error(`Invalid coupon: ${couponCode}`);
  cart.couponCode = couponCode;
  cart.discount = discount;
  return cart;
}

function processPayment(cart, cardLast4) {
  if (cart.total <= 0) throw new Error('Cart total must be positive');
  const paymentRef = 'PAY-' + crypto.randomBytes(4).toString('hex').toUpperCase();
  // Deduct stock
  for (const item of cart.items) {
    _products[item.product.productId].stock -= item.quantity;
  }
  return paymentRef;
}

function createOrder(cart, paymentRef) {
  const orderId = 'ORD-' + crypto.randomBytes(3).toString('hex').toUpperCase();
  const order = new Order(orderId, cart.userId, [...cart.items], cart.total);
  order.paymentRef = paymentRef;
  order.status = 'confirmed';
  _orders[orderId] = order;
  return order;
}

function checkout(userId, productQuantities, coupon = null) {
  let cart = buildCart(userId, productQuantities);
  if (coupon) {
    cart = applyCoupon(cart, coupon);
  }
  const paymentRef = processPayment(cart, '4242');
  const order = createOrder(cart, paymentRef);
  return order;
}

// ---------------------------------------------------------------------------
// Flow 3 – Order Tracking
// ---------------------------------------------------------------------------

const VALID_TRANSITIONS = {
  confirmed: ['shipped'],
  shipped:   ['delivered'],
  delivered: ['closed'],
};

function lookupOrder(orderId) {
  const order = _orders[orderId];
  if (!order) throw new Error(`Order not found: ${orderId}`);
  return order;
}

function updateOrderStatus(order, newStatus) {
  const allowed = VALID_TRANSITIONS[order.status] || [];
  if (!allowed.includes(newStatus)) {
    throw new Error(
      `Invalid transition: ${order.status} → ${newStatus}. Allowed: ${allowed}`
    );
  }
  order.status = newStatus;
  order.updatedAt = new Date().toISOString();
  _orders[order.orderId] = order;
  return order;
}

function sendStatusNotification(order) {
  return {
    recipient: order.userId,
    message:   `Your order ${order.orderId} is now ${order.status}.`,
    sent:      true,
  };
}

function trackOrder(orderId, newStatus) {
  const order = lookupOrder(orderId);
  updateOrderStatus(order, newStatus);
  const notification = sendStatusNotification(order);
  return { order, notification };
}

// ---------------------------------------------------------------------------
// Module exports
// ---------------------------------------------------------------------------

module.exports = {
  // Models
  User, Product, CartItem, Cart, Order,
  // Flow 1
  hashPassword, createAccount, sendVerificationEmail, verifyEmail, registerUser,
  // Flow 2
  buildCart, applyCoupon, processPayment, createOrder, checkout,
  // Flow 3
  lookupOrder, updateOrderStatus, sendStatusNotification, trackOrder,
};
