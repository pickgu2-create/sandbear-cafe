# -*- coding: utf-8 -*-
"""
샌드베어 카페 (언양) — 매장 운영 사이트
- 손님: 테이블 QR → 모바일 메뉴판(/?t=3) → 옵션 선택 → 결제(토스페이먼츠 or 카운터) → 제조 완료 알림
- 직원: 출퇴근(/staff, 개인 PIN) · 오픈/마감 체크리스트 · 주문 화면(/kds, 카운터·제조용)
- 사장님: /admin (비밀번호) — 매출 분석·메뉴·재고/유통기한·레시피 원가·생산일지·발주·직원·적립·예약·리뷰·판매 예측
파이썬 기본 기능만 사용 (설치할 라이브러리 없음) · 데이터는 data/sandbear.db (SQLite)
사용: python3 app.py                  실행 (SB_PORT, 기본 8095)
      python3 app.py --set-owner-pw   사장님 비밀번호 설정/변경
결제(선택): 환경변수 TOSS_CLIENT_KEY · TOSS_SECRET_KEY 를 넣으면 손님 폰 카드/간편결제 사용, 없으면 '카운터 결제'
"""
import base64, getpass, hashlib, hmac, json, os, re, secrets, sqlite3, sys, threading, time
import urllib.request, urllib.error
from datetime import datetime, timedelta, timezone, date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DATA = os.environ.get("SB_DATA", os.path.join(BASE, "data"))
DB = os.path.join(DATA, "sandbear.db")
PORT = int(os.environ.get("SB_PORT", "8095"))
HOST = os.environ.get("SB_HOST", "0.0.0.0")
TOSS_CK = os.environ.get("TOSS_CLIENT_KEY", "").strip()
TOSS_SK = os.environ.get("TOSS_SECRET_KEY", "").strip()
KST = timezone(timedelta(hours=9))
PAGES = {"/": "menu.html", "/menu": "menu.html", "/reserve": "reserve.html", "/kds": "kds.html", "/staff": "staff.html", "/admin": "admin.html"}
FILES = {"/style.css": ("style.css", "text/css; charset=utf-8"), "/common.js": ("common.js", "text/javascript; charset=utf-8"),
         "/icon.svg": ("icon.svg", "image/svg+xml"),
         "/qrcode.js": ("qrcode.js", "text/javascript; charset=utf-8")}   # QR 생성 (qrcode-generator, MIT)
OWNER_H, STAFF_H = 12, 16          # 로그인 유지 시간
os.makedirs(DATA, exist_ok=True)

def now(): return datetime.now(KST)
def ts(d=None): return (d or now()).strftime("%Y-%m-%d %H:%M:%S")
def today(): return now().strftime("%Y-%m-%d")

# ---------------------------------------------------------------- DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS menu(id INTEGER PRIMARY KEY, cat TEXT, name TEXT, price INTEGER, descr TEXT DEFAULT '',
  options TEXT DEFAULT '', emoji TEXT DEFAULT '', soldout INTEGER DEFAULT 0, stock INTEGER, active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ingredients(id INTEGER PRIMARY KEY, name TEXT, unit TEXT, min_qty REAL DEFAULT 0, unit_cost REAL DEFAULT 0,
  supplier TEXT DEFAULT '', memo TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY, ing_id INTEGER, qty REAL, expiry TEXT, received TEXT, price INTEGER DEFAULT 0, memo TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS recipes(menu_id INTEGER, ing_id INTEGER, amount REAL, PRIMARY KEY(menu_id, ing_id));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, token TEXT UNIQUE, toss_id TEXT, table_no TEXT, created TEXT, status TEXT,
  total INTEGER, pay TEXT, paid INTEGER DEFAULT 0, payment_key TEXT, phone TEXT DEFAULT '', memo TEXT DEFAULT '', ready_at TEXT, done_at TEXT,
  deducted INTEGER DEFAULT 0, points_given INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY, order_id INTEGER, menu_id INTEGER, name TEXT, opts TEXT, unit_price INTEGER, qty INTEGER);
CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY, name TEXT, pin_salt TEXT, pin_hash TEXT, pin_tag TEXT, wage INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY, staff_id INTEGER, clock_in TEXT, clock_out TEXT, memo TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS checklist_items(id INTEGER PRIMARY KEY, kind TEXT, text TEXT, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS checklist_log(day TEXT, item_id INTEGER, staff_id INTEGER, done_at TEXT, PRIMARY KEY(day, item_id));
CREATE TABLE IF NOT EXISTS production(id INTEGER PRIMARY KEY, day TEXT, menu_id INTEGER, made INTEGER DEFAULT 0, wasted INTEGER DEFAULT 0, memo TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS customers(phone TEXT PRIMARY KEY, points INTEGER DEFAULT 0, spent INTEGER DEFAULT 0, visits INTEGER DEFAULT 0, created TEXT, last TEXT);
CREATE TABLE IF NOT EXISTS point_log(id INTEGER PRIMARY KEY, phone TEXT, delta INTEGER, memo TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, item TEXT, qty INTEGER, pickup TEXT, memo TEXT,
  status TEXT DEFAULT '접수', created TEXT);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, order_id INTEGER, menu_id INTEGER, rating INTEGER, text TEXT, created TEXT);
"""
LOCK = threading.RLock()
con = sqlite3.connect(DB, check_same_thread=False)
con.row_factory = sqlite3.Row
con.executescript(SCHEMA)
if "acked" not in [r[1] for r in con.execute("PRAGMA table_info(orders)")]:   # 직원이 새 주문을 확인했는지 (알림 끄기용)
    con.execute("ALTER TABLE orders ADD COLUMN acked INTEGER DEFAULT 0"); con.execute("UPDATE orders SET acked=1"); con.commit()
if "resv" not in [r[1] for r in con.execute("PRAGMA table_info(menu)")]:      # 예약 페이지에 보일 메뉴 (처음엔 디저트만)
    con.execute("ALTER TABLE menu ADD COLUMN resv INTEGER DEFAULT 0"); con.execute("UPDATE menu SET resv=1 WHERE stock IS NOT NULL"); con.commit()
if "amount" not in [r[1] for r in con.execute("PRAGMA table_info(reservations)")]:   # 예약 예상 금액
    con.execute("ALTER TABLE reservations ADD COLUMN amount INTEGER DEFAULT 0"); con.commit()

def q(sql, *a):
    with LOCK: return [dict(r) for r in con.execute(sql, a).fetchall()]
def q1(sql, *a):
    r = q(sql, *a); return r[0] if r else None
def x(sql, *a):
    with LOCK:
        c = con.execute(sql, a); con.commit(); return c.lastrowid

def setting(k, default=""):
    r = q1("SELECT v FROM settings WHERE k=?", k); return r["v"] if r else default
def set_setting(k, v): x("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", k, str(v))

def seed():
    """처음 켤 때 예시 메뉴·체크리스트 (사장님 화면에서 고치거나 지우면 됨)"""
    if q1("SELECT 1 FROM menu"): return
    if not setting("cafe_name"): set_setting("cafe_name", "샌드베어")
    set_setting("points_rate", "3"); set_setting("tables", "10"); set_setting("expiry_days", "3")
    temp = "온도*: HOT, ICE"
    items = [
        ("커피", "아메리카노", 4500, "고소한 하우스 블렌드", temp + "\n샷 추가: 1샷+500, 2샷+1000\n사이즈*: 레귤러, 라지+1000", "☕"),
        ("커피", "카페라떼", 5000, "", temp + "\n우유*: 일반, 오트+700\n샷 추가: 1샷+500", "🥛"),
        ("커피", "바닐라라떼", 5500, "", temp + "\n샷 추가: 1샷+500", "🍦"),
        ("논커피", "딸기라떼", 6000, "생딸기 수제청", "", "🍓"),
        ("논커피", "얼그레이 밀크티", 5800, "", temp, "🫖"),
        ("디저트", "소금빵", 3500, "매일 아침 구워요", "", "🥐"),
        ("디저트", "휘낭시에", 3000, "", "", "🧈"),
        ("디저트", "바스크 치즈케이크", 6500, "", "", "🍰"),
    ]
    for i, (c, n, p, d, o, e) in enumerate(items):
        x("INSERT INTO menu(cat,name,price,descr,options,emoji,stock,sort) VALUES(?,?,?,?,?,?,?,?)",
          c, n, p, d, o, e, 0 if c == "디저트" else None, i)
    for i, t in enumerate(["포스·태블릿 켜기", "커피머신 예열·추출 테스트", "디저트 진열장 정리", "우유·재료 유통기한 확인", "매장 청소·테이블 정리"]):
        x("INSERT INTO checklist_items(kind,text,sort) VALUES('open',?,?)", t, i)
    for i, t in enumerate(["커피머신 청소(백플러시)", "남은 디저트 폐기량 기록", "냉장고 온도 확인", "쓰레기 정리", "가스·전기·문 잠금 확인"]):
        x("INSERT INTO checklist_items(kind,text,sort) VALUES('close',?,?)", t, i)
seed()

# ---------------------------------------------------------------- 비밀번호 · 로그인
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return base64.b64encode(salt).decode(), base64.b64encode(h).decode()
def check_pw(pw, salt, h):
    return hmac.compare_digest(hash_pw(pw, base64.b64decode(salt))[1], h)

def secret_key():
    k = setting("secret")
    if not k: k = secrets.token_hex(32); set_setting("secret", k)
    return k.encode()
def pin_tag(pin):   # 직원 PIN 으로 바로 찾기용 (HMAC — PIN 자체는 저장 안 함)
    return hmac.new(secret_key(), b"pin:" + pin.encode(), hashlib.sha256).hexdigest()

def make_cookie(role, sid, hours):
    exp = int(time.time() + hours * 3600)
    body = f"{role}.{sid}.{exp}.{setting('auth_ver', '1')}"
    return body + "." + hmac.new(secret_key(), body.encode(), hashlib.sha256).hexdigest()
def read_cookie(cookie, name):
    m = re.search(r"(?:^|;\s*)" + name + r"=([^;]+)", cookie or "")
    if not m: return None
    try:
        role, sid, exp, ver, sig = m.group(1).split(".")
        body = f"{role}.{sid}.{exp}.{ver}"
        if not hmac.compare_digest(sig, hmac.new(secret_key(), body.encode(), hashlib.sha256).hexdigest()): return None
        if int(exp) < time.time() or ver != setting("auth_ver", "1"): return None
        return role, int(sid)
    except Exception: return None

FAILS = {}   # ip → [실패 횟수, 잠금 해제 시각]
def too_many(ip):
    f = FAILS.get(ip); return bool(f and f[0] >= 5 and f[1] > time.time())
def fail(ip):
    f = FAILS.get(ip, [0, 0])
    if f[1] < time.time() and f[0] >= 5: f = [0, 0]
    f[0] += 1; f[1] = time.time() + 900; FAILS[ip] = f
def ok(ip): FAILS.pop(ip, None)

def find_staff(pin):
    if not re.fullmatch(r"\d{4,6}", pin or ""): return None
    s = q1("SELECT * FROM staff WHERE pin_tag=? AND active=1", pin_tag(pin))
    return s if s and check_pw(pin, s["pin_salt"], s["pin_hash"]) else None

# ---------------------------------------------------------------- 메뉴 · 옵션 · 품절
def parse_options(text):
    """사장님이 쓰는 옵션 형식 → 구조
    한 줄 = 옵션 묶음 · '이름*:' 은 꼭 하나 고르기 · '이름:' 은 골라도 되고 안 골라도 됨(여러 개 가능)
    예) 온도*: HOT, ICE      샷 추가: 1샷+500, 2샷+1000"""
    groups = []
    for line in (text or "").splitlines():
        if ":" not in line: continue
        name, rest = line.split(":", 1); name = name.strip()
        req = name.endswith("*"); name = name.rstrip("*").strip()
        ch = []
        for c in rest.split(","):
            c = c.strip()
            if not c: continue
            m = re.match(r"^(.*?)\s*\+\s*(\d+)\s*원?$", c)
            ch.append({"n": m.group(1).strip(), "p": int(m.group(2))} if m else {"n": c, "p": 0})
        if name and ch: groups.append({"name": name, "required": req, "choices": ch})
    return groups

def ing_stock():
    return {r["ing_id"]: r["s"] for r in q("SELECT ing_id, SUM(qty) s FROM lots WHERE qty>0 GROUP BY ing_id")}

def soldout_map():
    """품절 = 사장님이 직접 품절 · 개수 관리 메뉴(디저트)는 남은 개수 0 · 레시피 메뉴(음료)는 재료가 1잔 분량보다 적을 때"""
    st = ing_stock(); rec = {}
    for r in q("SELECT * FROM recipes"): rec.setdefault(r["menu_id"], []).append(r)
    out = {}
    for m in q("SELECT id, soldout, stock FROM menu"):
        why = ""
        if m["soldout"]: why = "manual"
        elif m["stock"] is not None and m["stock"] <= 0: why = "stock"
        elif m["stock"] is None and any(st.get(r["ing_id"], 0) < r["amount"] for r in rec.get(m["id"], [])): why = "ingredient"
        out[m["id"]] = why
    return out

def deduct_ing(ing_id, amount):
    """유통기한 빠른 것부터 차감 (FIFO)"""
    left = amount
    for l in q("SELECT id, qty FROM lots WHERE ing_id=? AND qty>0 ORDER BY (expiry IS NULL OR expiry=''), expiry, id", ing_id):
        use = min(l["qty"], left)
        x("UPDATE lots SET qty=? WHERE id=?", round(l["qty"] - use, 4), l["id"]); left -= use
        if left <= 1e-9: return

def apply_stock(order_id, sign):
    """주문이 확정되면 재고 차감(sign=-1), 취소되면 되돌림(sign=+1)
    - 개수 관리 메뉴(디저트): 메뉴 남은 개수만 (재료는 생산일지에서 이미 차감됨)
    - 레시피 메뉴(음료): 재료 차감"""
    with LOCK:
        o = q1("SELECT deducted FROM orders WHERE id=?", order_id)
        if (sign < 0 and o["deducted"]) or (sign > 0 and not o["deducted"]): return
        for it in q("SELECT oi.menu_id, oi.qty, m.stock FROM order_items oi LEFT JOIN menu m ON m.id=oi.menu_id WHERE order_id=?", order_id):
            if it["stock"] is not None:
                x("UPDATE menu SET stock=MAX(0, stock+?) WHERE id=?", sign * it["qty"], it["menu_id"])
            else:
                for r in q("SELECT * FROM recipes WHERE menu_id=?", it["menu_id"]):
                    amt = r["amount"] * it["qty"]
                    if sign < 0: deduct_ing(r["ing_id"], amt)
                    else: x("INSERT INTO lots(ing_id,qty,expiry,received,memo) VALUES(?,?,NULL,?,?)", r["ing_id"], amt, ts(), "주문 취소로 되돌림")
        x("UPDATE orders SET deducted=? WHERE id=?", 1 if sign < 0 else 0, order_id)

def give_points(order_id):
    o = q1("SELECT * FROM orders WHERE id=?", order_id)
    if not o or not o["phone"] or o["points_given"] or not o["paid"]: return
    pts = int(o["total"] * float(setting("points_rate", "0") or 0) / 100)
    with LOCK:
        if not q1("SELECT 1 FROM customers WHERE phone=?", o["phone"]):
            x("INSERT INTO customers(phone,created) VALUES(?,?)", o["phone"], ts())
        x("UPDATE customers SET points=points+?, spent=spent+?, visits=visits+1, last=? WHERE phone=?", pts, o["total"], ts(), o["phone"])
        if pts: x("INSERT INTO point_log(phone,delta,memo,at) VALUES(?,?,?,?)", o["phone"], pts, f"주문 #{order_id} 적립", ts())
        x("UPDATE orders SET points_given=1 WHERE id=?", order_id)

def norm_phone(p):
    p = re.sub(r"\D", "", p or "")
    return p if re.fullmatch(r"01\d{8,9}", p) else ""

# ---------------------------------------------------------------- 손님 API
def api_menu(h, d):
    so = soldout_map()
    items = []
    for m in q("SELECT * FROM menu WHERE active=1 ORDER BY sort, id"):
        items.append(dict(id=m["id"], cat=m["cat"], name=m["name"], price=m["price"], descr=m["descr"], emoji=m["emoji"],
                          options=parse_options(m["options"]), soldout=bool(so.get(m["id"])),
                          left=m["stock"] if m["stock"] is not None and 0 < m["stock"] <= 3 else None))
    cats = []
    for it in items:
        if it["cat"] not in cats: cats.append(it["cat"])
    rv = {r["menu_id"]: (round(r["a"], 1), r["n"]) for r in q("SELECT menu_id, AVG(rating) a, COUNT(*) n FROM reviews GROUP BY menu_id")}
    for it in items: it["rating"] = rv.get(it["id"])
    wifi = dict(ssid=setting("wifi_ssid"), pw=setting("wifi_pw")) if setting("wifi_show", "1") == "1" and setting("wifi_ssid") else None
    return dict(cafe=setting("cafe_name", "샌드베어"), notice=setting("notice"), cats=cats, items=items, wifi=wifi,
                toss=TOSS_CK or None, points_rate=float(setting("points_rate", "0") or 0))

def api_order(h, d):
    lines = d.get("items") or []
    if not lines or len(lines) > 40: return 400, {"error": "담긴 메뉴가 없어요"}
    so = soldout_map(); total = 0; rows = []
    for ln in lines:
        m = q1("SELECT * FROM menu WHERE id=? AND active=1", int(ln.get("id", 0)))
        qty = int(ln.get("qty", 1))
        if not m or not 1 <= qty <= 30: return 400, {"error": "메뉴를 다시 확인해 주세요"}
        if so.get(m["id"]): return 409, {"error": f"'{m['name']}' 이(가) 방금 품절됐어요"}
        if m["stock"] is not None and m["stock"] < qty: return 409, {"error": f"'{m['name']}' 은(는) {m['stock']}개 남았어요"}
        groups = parse_options(m["options"]); sel = ln.get("opts") or {}
        price = m["price"]; picked = []
        for gi, g in enumerate(groups):
            ch = sel.get(str(gi)) or []
            if not isinstance(ch, list): ch = [ch]
            ch = [int(c) for c in ch if str(c).isdigit() and int(c) < len(g["choices"])]
            if g["required"] and len(ch) != 1: return 400, {"error": f"'{m['name']}' {g['name']}을(를) 골라 주세요"}
            for c in ch:
                price += g["choices"][c]["p"]; picked.append(f"{g['name']}:{g['choices'][c]['n']}")
        rows.append((m["id"], m["name"], ", ".join(picked), price, qty)); total += price * qty
    pay = "toss" if d.get("pay") == "toss" and TOSS_CK and TOSS_SK else "counter"
    token = secrets.token_urlsafe(12); toss_id = "SB" + now().strftime("%y%m%d%H%M%S") + secrets.token_hex(4)
    table = re.sub(r"[^0-9A-Za-z가-힣\- ]", "", str(d.get("table") or ""))[:10] or "포장"
    with LOCK:
        oid = x("INSERT INTO orders(token,toss_id,table_no,created,status,total,pay,phone,memo) VALUES(?,?,?,?,?,?,?,?,?)",
                token, toss_id, table, ts(), "pending_payment" if pay == "toss" else "new", total, pay,
                norm_phone(d.get("phone")), str(d.get("memo") or "")[:100])
        for r in rows: x("INSERT INTO order_items(order_id,menu_id,name,opts,unit_price,qty) VALUES(?,?,?,?,?,?)", oid, *r)
        if pay == "counter": apply_stock(oid, -1)    # 카운터 결제는 접수 순간 재고 확보
    name = rows[0][1] + (f" 외 {sum(r[4] for r in rows) - 1}개" if sum(r[4] for r in rows) > 1 else "")
    return 200, dict(token=token, no=oid, total=total, pay=pay, toss_id=toss_id, order_name=name)

def order_public(o):
    items = q("SELECT menu_id, name, opts, unit_price, qty FROM order_items WHERE order_id=?", o["id"])
    reviewed = bool(q1("SELECT 1 FROM reviews WHERE order_id=?", o["id"]))
    return dict(no=o["id"], table=o["table_no"], status=o["status"], total=o["total"], pay=o["pay"], paid=bool(o["paid"]),
                created=o["created"], ready_at=o["ready_at"], items=items, reviewed=reviewed)

def api_order_get(h, d, token):
    o = q1("SELECT * FROM orders WHERE token=?", token)
    return (200, order_public(o)) if o else (404, {"error": "주문을 찾을 수 없어요"})

def api_pay_confirm(h, d):
    """토스페이먼츠 결제 승인 — 금액은 서버에 저장된 주문 금액과 반드시 같아야 함"""
    o = q1("SELECT * FROM orders WHERE toss_id=?", str(d.get("orderId", "")))
    if not o: return 404, {"error": "주문 없음"}
    if o["paid"]: return 200, {"token": o["token"]}
    if int(d.get("amount", -1)) != o["total"]: return 400, {"error": "결제 금액이 주문과 달라요"}
    body = json.dumps({"paymentKey": d.get("paymentKey"), "orderId": o["toss_id"], "amount": o["total"]}).encode()
    req = urllib.request.Request("https://api.tosspayments.com/v1/payments/confirm", data=body, method="POST", headers={
        "Authorization": "Basic " + base64.b64encode((TOSS_SK + ":").encode()).decode(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: msg = json.loads(e.read()).get("message", "결제 승인 실패")
        except Exception: msg = "결제 승인 실패"
        return 400, {"error": msg}
    except Exception: return 502, {"error": "결제사 연결 실패 — 잠시 후 다시 시도해 주세요"}
    x("UPDATE orders SET paid=1, status='new', payment_key=?, created=? WHERE id=?", res.get("paymentKey", d.get("paymentKey")), ts(), o["id"])
    apply_stock(o["id"], -1); give_points(o["id"])
    return 200, {"token": o["token"]}

def api_pay_fail(h, d):
    o = q1("SELECT * FROM orders WHERE toss_id=? AND status='pending_payment'", str(d.get("orderId", "")))
    if o: x("UPDATE orders SET status='canceled' WHERE id=?", o["id"])
    return 200, {}

def api_points(h, d):
    p = norm_phone(d.get("phone"))
    if not p: return 400, {"error": "휴대폰 번호를 확인해 주세요"}
    if too_many(h.ip): return 429, {"error": "잠시 후 다시 시도해 주세요"}
    c = q1("SELECT points, visits FROM customers WHERE phone=?", p)
    if not c: fail(h.ip)   # 번호 마구 넣어보기 방지
    return 200, c or {"points": 0, "visits": 0}

def resv_conf():
    hours = setting("resv_hours", "11:00-19:00")
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", hours)
    try: o, c = f"{int(m.group(1)):02d}:{m.group(2)}", f"{int(m.group(3)):02d}:{m.group(4)}"
    except Exception: o, c = "11:00", "19:00"
    dates = lambda k: sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", setting(k))))
    return dict(open=o, close=c, min_days=max(0, int(num(setting("resv_min_days", "1"), 1))), max_days=60,
                closed=[w for w in "월화수목금토일" if w in setting("resv_closed", "화")],
                open_dates=dates("open_dates"), off_dates=dates("off_dates"))

def is_open_day(day, c):
    """영업일: 쉬는 요일이면 '이날은 영업'으로 고른 날만 · 그 외엔 '임시 휴무'로 고른 날 빼고"""
    s = day.isoformat()
    if s in c["off_dates"]: return False
    return "월화수목금토일"[day.weekday()] not in c["closed"] or s in c["open_dates"]

def api_reserve_info(h, d):
    items = [dict(id=m["id"], cat=m["cat"], name=m["name"], price=m["price"], descr=m["descr"], emoji=m["emoji"])
             for m in q("SELECT * FROM menu WHERE resv=1 ORDER BY sort, id")]
    return 200, dict(cafe=setting("cafe_name", "샌드베어"), notice=setting("resv_notice"), items=items, **resv_conf())

def api_reservation(h, d):
    """예약 신청: 메뉴를 골라서(items) + 직접 쓴 요청(item) · 픽업 날짜·시간은 영업시간·쉬는 날·며칠 전 규칙 확인"""
    name = str(d.get("name", "")).strip()[:20]; phone = norm_phone(d.get("phone")); extra = str(d.get("item", "")).strip()[:100]
    pickup = str(d.get("pickup", ""))[:16].replace("T", " ")
    parts, qty, amount = [], 0, 0
    for it in (d.get("items") or [])[:20]:
        m = q1("SELECT * FROM menu WHERE id=? AND resv=1", int(it.get("id") or 0)); n = int(it.get("qty") or 0)
        if m and 1 <= n <= 50: parts.append(f"{m['name']} {n}개"); qty += n; amount += m["price"] * n
    if extra: parts.append(extra)
    if not name: return 400, {"error": "이름을 적어 주세요"}
    if not phone: return 400, {"error": "연락처(휴대폰 번호)를 확인해 주세요"}
    if not parts: return 400, {"error": "예약할 메뉴를 골라 주세요"}
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", pickup): return 400, {"error": "픽업 날짜와 시간을 골라 주세요"}
    c = resv_conf(); day = datetime.strptime(pickup[:10], "%Y-%m-%d").date(); gap = (day - now().date()).days
    if gap < c["min_days"]: return 400, {"error": f"예약은 {c['min_days']}일 전까지 받아요" if c["min_days"] else "픽업 시간이 지났어요"}
    if gap > c["max_days"] or not is_open_day(day, c): return 400, {"error": "그날은 쉬는 날이에요 — 다른 날을 골라 주세요"}
    if not c["open"] <= pickup[11:] <= c["close"] or pickup < ts()[:16]: return 400, {"error": f"픽업 시간은 {c['open']}~{c['close']} 사이로 골라 주세요"}
    if len(q("SELECT 1 FROM reservations WHERE phone=? AND created>?", phone, ts(now() - timedelta(days=1)))) >= 5: return 429, {"error": "예약이 너무 많아요 — 매장으로 연락 주세요"}
    x("INSERT INTO reservations(name,phone,item,qty,pickup,memo,created,amount) VALUES(?,?,?,?,?,?,?,?)",
      name, phone, ", ".join(parts)[:300], max(1, qty), pickup, str(d.get("memo", ""))[:200], ts(), amount)
    return 200, {"item": ", ".join(parts), "amount": amount, "pickup": pickup}

def api_review(h, d):
    o = q1("SELECT * FROM orders WHERE token=?", str(d.get("token", "")))
    if not o or o["status"] not in ("ready", "done"): return 400, {"error": "제조가 끝난 주문만 후기를 남길 수 있어요"}
    if q1("SELECT 1 FROM reviews WHERE order_id=?", o["id"]): return 400, {"error": "이미 후기를 남겼어요"}
    mids = {r["menu_id"] for r in q("SELECT menu_id FROM order_items WHERE order_id=?", o["id"])}
    n = 0
    for r in (d.get("reviews") or [])[:20]:
        mid = int(r.get("menu_id", 0)); rt = int(r.get("rating", 0))
        if mid in mids and 1 <= rt <= 5:
            x("INSERT INTO reviews(order_id,menu_id,rating,text,created) VALUES(?,?,?,?,?)", o["id"], mid, rt, str(r.get("text", ""))[:300], ts()); n += 1
    return 200, {"n": n}

# ---------------------------------------------------------------- 직원 API
def api_staff_login(h, d):
    if too_many(h.ip): return 429, {"error": "5번 틀려서 15분 동안 잠겼어요"}
    s = find_staff(str(d.get("pin", "")))
    if not s: fail(h.ip); return 401, {"error": "PIN 이 맞지 않아요"}
    ok(h.ip); h.set_cookie("sb_staff", make_cookie("staff", s["id"], STAFF_H), STAFF_H)
    return 200, {"name": s["name"]}

def api_clock(h, d):
    if too_many(h.ip): return 429, {"error": "5번 틀려서 15분 동안 잠겼어요"}
    s = find_staff(str(d.get("pin", "")))
    if not s: fail(h.ip); return 401, {"error": "PIN 이 맞지 않아요"}
    ok(h.ip)
    open_ = q1("SELECT * FROM attendance WHERE staff_id=? AND clock_out IS NULL ORDER BY id DESC", s["id"])
    act = d.get("action")
    if act == "in":
        if open_: return 400, {"error": f"{s['name']}님은 {open_['clock_in'][5:16]} 에 이미 출근했어요"}
        x("INSERT INTO attendance(staff_id,clock_in) VALUES(?,?)", s["id"], ts())
        return 200, {"msg": f"{s['name']}님 출근 {ts()[11:16]} — 오늘도 화이팅! 🐻"}
    if act == "out":
        if not open_: return 400, {"error": f"{s['name']}님은 출근 기록이 없어요"}
        x("UPDATE attendance SET clock_out=? WHERE id=?", ts(), open_["id"])
        mins = int((now() - datetime.strptime(open_["clock_in"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)).total_seconds() // 60)
        return 200, {"msg": f"{s['name']}님 퇴근 {ts()[11:16]} — 오늘 {mins // 60}시간 {mins % 60}분 수고했어요"}
    return 400, {"error": "?"}

def kds_orders():
    since = ts(now() - timedelta(hours=14))
    out = []
    for o in q("SELECT * FROM orders WHERE created>? AND status IN ('new','making','ready') ORDER BY id", since):
        p = order_public(o); p["id"] = o["id"]; p["memo"] = o["memo"]; p["phone4"] = o["phone"][-4:] if o["phone"] else ""; p["acked"] = bool(o["acked"])
        out.append(p)
    done = q("SELECT id, table_no, total, done_at FROM orders WHERE created>? AND status='done' ORDER BY id DESC LIMIT 15", since)
    return {"orders": out, "done": done}

def api_kds_update(h, d):
    o = q1("SELECT * FROM orders WHERE id=?", int(d.get("id", 0)))
    if not o: return 404, {"error": "주문 없음"}
    a = d.get("action")
    if a in ("paid", "making", "ready", "done", "cancel", "ack"): x("UPDATE orders SET acked=1 WHERE id=?", o["id"])   # 버튼을 누르면 확인한 것 → 알림 끔
    if a == "paid": x("UPDATE orders SET paid=1 WHERE id=?", o["id"]); give_points(o["id"])
    elif a == "making": x("UPDATE orders SET status='making' WHERE id=?", o["id"])
    elif a == "ready": x("UPDATE orders SET status='ready', ready_at=? WHERE id=?", ts(), o["id"])
    elif a == "done": x("UPDATE orders SET status='done', done_at=? WHERE id=?", ts(), o["id"])
    elif a == "back": x("UPDATE orders SET status='new' WHERE id=?", o["id"])
    elif a == "cancel":
        if o["pay"] == "toss" and o["paid"]: return 400, {"error": "카드 결제 주문은 토스페이먼츠 관리자에서 환불 후 사장님 화면에서 취소해 주세요"}
        x("UPDATE orders SET status='canceled' WHERE id=?", o["id"]); apply_stock(o["id"], +1)
    return 200, kds_orders()

def api_checklist(h, d, staff_id=None):
    day = today()
    items = q("SELECT * FROM checklist_items WHERE active=1 ORDER BY kind DESC, sort, id")
    log = {r["item_id"]: r for r in q("SELECT l.*, s.name FROM checklist_log l LEFT JOIN staff s ON s.id=l.staff_id WHERE day=?", day)}
    for it in items:
        l = log.get(it["id"]); it["done"] = bool(l); it["by"] = (l or {}).get("name") or ("사장님" if l else ""); it["at"] = (l or {}).get("done_at", "")[11:16]
    return 200, {"day": day, "items": items}

def api_checklist_toggle(h, d, staff_id):
    iid = int(d.get("item_id", 0)); day = today()
    if q1("SELECT 1 FROM checklist_log WHERE day=? AND item_id=?", day, iid): x("DELETE FROM checklist_log WHERE day=? AND item_id=?", day, iid)
    else: x("INSERT INTO checklist_log(day,item_id,staff_id,done_at) VALUES(?,?,?,?)", day, iid, staff_id, ts())
    return api_checklist(h, d)

# ---------------------------------------------------------------- 사장님 API
def api_admin_login(h, d):
    if too_many(h.ip): return 429, {"error": "5번 틀려서 15분 동안 잠겼어요"}
    salt, hh = setting("owner_salt"), setting("owner_hash")
    if not hh: return 400, {"error": "비밀번호가 아직 없어요 — 서버에서 python3 app.py --set-owner-pw 실행"}
    if not check_pw(str(d.get("password", "")), salt, hh): fail(h.ip); return 401, {"error": "비밀번호가 맞지 않아요"}
    ok(h.ip); h.set_cookie("sb_owner", make_cookie("owner", 0, OWNER_H), OWNER_H)
    return 200, {}

PAID = "o.status NOT IN ('canceled','pending_payment')"

def sales_by(fmt, since):
    return q(f"SELECT strftime('{fmt}', created) k, SUM(total) amt, COUNT(*) n FROM orders o WHERE {PAID} AND created>=? GROUP BY k ORDER BY k", since)

def api_dashboard(h, d):
    t = today(); since30 = (now() - timedelta(days=29)).strftime("%Y-%m-%d"); since12 = (now().replace(day=1) - timedelta(days=330)).strftime("%Y-%m-01")
    frm = d.get("from") or since30; to = d.get("to") or t
    today_s = q1(f"SELECT COALESCE(SUM(total),0) amt, COUNT(*) n FROM orders o WHERE {PAID} AND created>=?", t)
    yday = (now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yday_s = q1(f"SELECT COALESCE(SUM(total),0) amt FROM orders o WHERE {PAID} AND created>=? AND created<?", yday, t)
    month_s = q1(f"SELECT COALESCE(SUM(total),0) amt, COUNT(*) n FROM orders o WHERE {PAID} AND created>=?", t[:8] + "01")
    popular = q(f"""SELECT oi.menu_id, oi.name, SUM(oi.qty) qty, SUM(oi.qty*oi.unit_price) amt FROM order_items oi JOIN orders o ON o.id=oi.order_id
                    WHERE {PAID} AND o.created>=? AND o.created<? GROUP BY oi.menu_id ORDER BY qty DESC LIMIT 15""", frm, to + " 99")
    hourly = q(f"SELECT CAST(strftime('%H', created) AS INTEGER) k, SUM(total) amt, COUNT(*) n FROM orders o WHERE {PAID} AND created>=? AND created<? GROUP BY k ORDER BY k", frm, to + " 99")
    weekday = q(f"SELECT CAST(strftime('%w', created) AS INTEGER) k, SUM(total) amt, COUNT(DISTINCT substr(created,1,10)) days FROM orders o WHERE {PAID} AND created>=? AND created<? GROUP BY k", frm, to + " 99")
    unpaid = q("SELECT id, table_no, total, created FROM orders WHERE pay='counter' AND paid=0 AND status NOT IN ('canceled','pending_payment') ORDER BY id DESC LIMIT 20")
    return 200, dict(today=today_s, yesterday=yday_s, month=month_s, daily=sales_by("%Y-%m-%d", since30), monthly=sales_by("%Y-%m", since12),
                     popular=popular, hourly=hourly, weekday=weekday, unpaid=unpaid, alerts=alerts(), frm=frm, to=to,
                     pending_res=q1("SELECT COUNT(*) n FROM reservations WHERE status='접수'")["n"])

def ingredients_full():
    st = ing_stock(); warn = int(setting("expiry_days", "3") or 3); t = today(); soon = (now() + timedelta(days=warn)).strftime("%Y-%m-%d")
    out = []
    for i in q("SELECT * FROM ingredients ORDER BY name"):
        i["qty"] = round(st.get(i["id"], 0), 3)
        i["lots"] = q("SELECT * FROM lots WHERE ing_id=? AND qty>0 ORDER BY (expiry IS NULL OR expiry=''), expiry, id", i["id"])
        for l in i["lots"]: l["state"] = "expired" if l["expiry"] and l["expiry"] < t else ("soon" if l["expiry"] and l["expiry"] <= soon else "")
        i["low"] = i["min_qty"] > 0 and i["qty"] <= i["min_qty"]
        out.append(i)
    return out

def alerts():
    a = []
    for i in ingredients_full():
        if i["low"]: a.append({"kind": "low", "text": f"{i['name']} 재고 {fmt_n(i['qty'])}{i['unit']} (최소 {fmt_n(i['min_qty'])}{i['unit']})"})
        for l in i["lots"]:
            if l["state"] == "expired": a.append({"kind": "expired", "text": f"{i['name']} {fmt_n(l['qty'])}{i['unit']} 유통기한 지남 ({l['expiry']})"})
            elif l["state"] == "soon": a.append({"kind": "soon", "text": f"{i['name']} {fmt_n(l['qty'])}{i['unit']} 유통기한 {l['expiry']}"})
    for mid, why in soldout_map().items():
        if why in ("stock", "ingredient"):
            m = q1("SELECT name, active FROM menu WHERE id=?", mid)
            if m and m["active"]: a.append({"kind": "soldout", "text": f"{m['name']} 자동 품절 ({'남은 개수 0' if why == 'stock' else '재료 부족'})"})
    return a

def fmt_n(v): return int(v) if float(v).is_integer() else round(v, 2)

def recipe_costs():
    ings = {i["id"]: i for i in q("SELECT * FROM ingredients")}
    out = []
    for m in q("SELECT * FROM menu ORDER BY sort, id"):
        lines = q("SELECT * FROM recipes WHERE menu_id=?", m["id"]); cost = 0
        for l in lines:
            i = ings.get(l["ing_id"]) or {}; l["name"] = i.get("name", "?"); l["unit"] = i.get("unit", "")
            l["cost"] = round(l["amount"] * (i.get("unit_cost") or 0), 1); cost += l["cost"]
        out.append(dict(menu_id=m["id"], name=m["name"], price=m["price"], cat=m["cat"], lines=lines, cost=round(cost),
                        margin=m["price"] - round(cost), ratio=round(cost / m["price"] * 100, 1) if m["price"] else 0))
    return out

def api_production(h, d):
    day = d.get("day") or today()
    rows = q("SELECT p.*, m.name FROM production p LEFT JOIN menu m ON m.id=p.menu_id WHERE day=? ORDER BY p.id", day)
    sold = {r["menu_id"]: r["n"] for r in q(f"""SELECT oi.menu_id, SUM(oi.qty) n FROM order_items oi JOIN orders o ON o.id=oi.order_id
                                               WHERE {PAID} AND substr(o.created,1,10)=? GROUP BY oi.menu_id""", day)}
    summary = {}
    for r in rows:
        s = summary.setdefault(r["menu_id"], dict(menu_id=r["menu_id"], name=r["name"], made=0, wasted=0)); s["made"] += r["made"]; s["wasted"] += r["wasted"]
    for s in summary.values(): s["sold"] = sold.get(s["menu_id"], 0)
    hist = q("""SELECT day, SUM(made) made, SUM(wasted) wasted FROM production WHERE day>=? GROUP BY day ORDER BY day DESC""",
             (now() - timedelta(days=30)).strftime("%Y-%m-%d"))
    menus = q("SELECT id, name, stock FROM menu WHERE active=1 ORDER BY sort, id")
    return 200, dict(day=day, rows=rows, summary=list(summary.values()), hist=hist, menus=menus)

def api_production_save(h, d):
    """생산 기록: 만든 개수만큼 메뉴 남은 개수 +, 레시피 재료 차감 · 폐기는 남은 개수 -"""
    mid = int(d.get("menu_id", 0)); made = max(0, int(d.get("made") or 0)); wasted = max(0, int(d.get("wasted") or 0))
    m = q1("SELECT * FROM menu WHERE id=?", mid)
    if not m or not (made or wasted): return 400, {"error": "메뉴와 개수를 넣어 주세요"}
    day = d.get("day") or today()
    with LOCK:
        x("INSERT INTO production(day,menu_id,made,wasted,memo) VALUES(?,?,?,?,?)", day, mid, made, wasted, str(d.get("memo", ""))[:100])
        if m["stock"] is None: x("UPDATE menu SET stock=0 WHERE id=?", mid)
        x("UPDATE menu SET stock=MAX(0, stock+?) WHERE id=?", made - wasted, mid)
        if made:
            for r in q("SELECT * FROM recipes WHERE menu_id=?", mid): deduct_ing(r["ing_id"], r["amount"] * made)
    return api_production(h, {"day": day})

def api_production_delete(h, d):
    p = q1("SELECT * FROM production WHERE id=?", int(d.get("id", 0)))
    if p:
        with LOCK:
            x("UPDATE menu SET stock=MAX(0, stock-?) WHERE id=? AND stock IS NOT NULL", p["made"] - p["wasted"], p["menu_id"])
            if p["made"]:
                for r in q("SELECT * FROM recipes WHERE menu_id=?", p["menu_id"]):
                    x("INSERT INTO lots(ing_id,qty,expiry,received,memo) VALUES(?,?,NULL,?,?)", r["ing_id"], r["amount"] * p["made"], ts(), "생산 기록 삭제로 되돌림")
            x("DELETE FROM production WHERE id=?", p["id"])
    return api_production(h, {"day": d.get("day")})

def api_purchase(h, d):
    rows = []
    for i in ingredients_full():
        if i["low"] or any(l["state"] == "expired" for l in i["lots"]):
            usable = sum(l["qty"] for l in i["lots"] if l["state"] != "expired")
            need = max(i["min_qty"] * 2 - usable, i["min_qty"])
            rows.append(dict(id=i["id"], name=i["name"], unit=i["unit"], qty=fmt_n(round(usable, 2)), min_qty=fmt_n(i["min_qty"]),
                             need=fmt_n(round(need, 2)), supplier=i["supplier"], cost=round(need * i["unit_cost"])))
    return 200, {"rows": rows}

def api_attendance(h, d):
    month = d.get("month") or today()[:7]
    rows = q("SELECT a.*, s.name, s.wage FROM attendance a LEFT JOIN staff s ON s.id=a.staff_id WHERE substr(clock_in,1,7)=? ORDER BY clock_in DESC", month)
    tot = {}
    for r in rows:
        mins = 0
        if r["clock_out"]:
            mins = int((datetime.strptime(r["clock_out"], "%Y-%m-%d %H:%M:%S") - datetime.strptime(r["clock_in"], "%Y-%m-%d %H:%M:%S")).total_seconds() // 60)
        r["mins"] = max(0, mins)
        t = tot.setdefault(r["staff_id"], dict(name=r["name"], wage=r["wage"] or 0, mins=0, days=set())); t["mins"] += r["mins"]; t["days"].add(r["clock_in"][:10])
    summary = [dict(name=t["name"], days=len(t["days"]), mins=t["mins"], pay=round(t["mins"] / 60 * t["wage"])) for t in tot.values()]
    working = q("SELECT a.clock_in, s.name FROM attendance a JOIN staff s ON s.id=a.staff_id WHERE clock_out IS NULL")
    return 200, dict(month=month, rows=rows, summary=summary, working=working)

# ---------------------------------------------------------------- 월급 (정산 기간 · 월급날 · 주휴수당)
def pay_conf():
    return dict(start=min(28, max(1, int(num(setting("pay_start", "1"), 1)))), day=min(31, max(1, int(num(setting("pay_day", "10"), 10)))),
                juhyu=setting("pay_juhyu", "1") == "1", tax=setting("pay_tax", "0") == "1")

def add_month(y, m, k=1):
    m += k; return y + (m - 1) // 12, (m - 1) % 12 + 1

def month_len(y, m):
    y2, m2 = add_month(y, m); return (date(y2, m2, 1) - timedelta(days=1)).day

def pay_period(ym, c):
    """ym(정산 시작 달)의 정산 기간과 월급날: 시작일 1 → 1일~말일, 21 → 21일~다음 달 20일 · 월급날 = 기간이 끝난 뒤 처음 오는 그날"""
    y, m = map(int, ym.split("-")); start = date(y, m, c["start"])
    y2, m2 = add_month(y, m); end = date(y2, m2, c["start"]) - timedelta(days=1)
    py, pm = end.year, end.month
    while True:
        pay = date(py, pm, min(c["day"], month_len(py, pm)))
        if pay > end: return start, end, pay
        py, pm = add_month(py, pm)

def current_period_ym(c):
    t = now().date(); y, m = t.year, t.month
    if t.day < c["start"]: y, m = add_month(y, m, -1)
    return f"{y}-{m:02d}"

def att_mins(r):
    if not r["clock_out"]: return 0
    return max(0, int((datetime.strptime(r["clock_out"], "%Y-%m-%d %H:%M:%S") - datetime.strptime(r["clock_in"], "%Y-%m-%d %H:%M:%S")).total_seconds() // 60))

def api_payroll(h, d):
    """월급 계산: 기본급(근무시간×시급) + 주휴수당(한 주 15시간 이상이면 min(주 시간,40)/40×8×시급, 그 주 일요일이 들어간 정산 기간에)"""
    c = pay_conf(); ym = d.get("period") or current_period_ym(c)
    if not re.fullmatch(r"\d{4}-\d{2}", ym): ym = current_period_ym(c)
    start, end, pay = pay_period(ym, c)
    wk0 = start - timedelta(days=start.weekday()); wk1 = end + timedelta(days=6 - end.weekday())   # 걸친 주 전체 (월~일)
    rows = q("SELECT a.*, s.name, s.wage, s.active FROM attendance a JOIN staff s ON s.id=a.staff_id WHERE substr(clock_in,1,10) BETWEEN ? AND ?",
             wk0.isoformat(), wk1.isoformat())
    per = {}
    for r in rows:
        day = date.fromisoformat(r["clock_in"][:10]); mins = att_mins(r)
        p = per.setdefault(r["staff_id"], dict(id=r["staff_id"], name=r["name"], wage=r["wage"] or 0, mins=0, days=set(), weeks={}, open=0))
        sun = day + timedelta(days=6 - day.weekday()); p["weeks"][sun] = p["weeks"].get(sun, 0) + mins
        if start <= day <= end:
            p["mins"] += mins; p["days"].add(day)
            if not r["clock_out"]: p["open"] += 1
    out = []
    for p in per.values():
        base = round(p["mins"] / 60 * p["wage"])
        jw = [(s, m) for s, m in p["weeks"].items() if start <= s <= end and m >= 15 * 60] if c["juhyu"] else []
        juhyu = round(sum(min(m, 40 * 60) / (40 * 60) * 8 * p["wage"] for _, m in jw))
        total = base + juhyu
        tax = (total * 33 // 1000) // 10 * 10 if c["tax"] else 0      # 3.3% (10원 미만 버림)
        if p["mins"] or p["open"]:
            out.append(dict(name=p["name"], wage=p["wage"], days=len(p["days"]), mins=p["mins"], base=base, juhyu=juhyu, juhyu_weeks=len(jw),
                            total=total, tax=tax, net=total - tax, open=p["open"]))
    out.sort(key=lambda r: -r["total"])
    prev = "%d-%02d" % add_month(*map(int, ym.split("-")), -1); nxt = "%d-%02d" % add_month(*map(int, ym.split("-")), 1)
    return 200, dict(period=ym, start=start.isoformat(), end=end.isoformat(), payday=pay.isoformat(), dday=(pay - now().date()).days,
                     rows=out, conf=c, prev=prev, next=nxt, sum=sum(r["total"] for r in out), sum_net=sum(r["net"] for r in out))

def api_forecast(h, d):
    """판매 예측: 최근 8주 같은 요일 판매량 평균 (최근 주일수록 가중치 ↑) — 데이터가 쌓일수록 정확해짐"""
    start = (now() - timedelta(days=56)).strftime("%Y-%m-%d")
    rows = q(f"""SELECT substr(o.created,1,10) day, oi.menu_id, oi.name, SUM(oi.qty) n FROM order_items oi JOIN orders o ON o.id=oi.order_id
                 WHERE {PAID} AND o.created>=? AND o.created<? GROUP BY day, oi.menu_id""", start, today())
    rev = q(f"SELECT substr(created,1,10) day, SUM(total) amt FROM orders o WHERE {PAID} AND created>=? AND created<? GROUP BY day", start, today())
    days_with = sorted({r["day"] for r in rev})
    names = {m["id"]: m["name"] for m in q("SELECT id, name FROM menu WHERE active=1")}
    by = {}
    for r in rows: by.setdefault(r["menu_id"], {})[r["day"]] = r["n"]
    revd = {r["day"]: r["amt"] for r in rev}
    out = []
    for k in range(7):
        dd = (now() + timedelta(days=k)).date(); same = [dd - timedelta(days=7 * w) for w in range(1, 9)]
        same = [s.isoformat() for s in same if s.isoformat() in days_with]
        wts = [1 / (i + 1) ** 0.5 for i in range(len(same))]; W = sum(wts) or 1
        items = []
        for mid, nm in names.items():
            v = sum(by.get(mid, {}).get(s, 0) * w for s, w in zip(same, wts)) / W if same else 0
            if v >= 0.5: items.append(dict(name=nm, qty=round(v)))
        items.sort(key=lambda r: -r["qty"])
        out.append(dict(day=dd.isoformat(), wd="월화수목금토일"[dd.weekday()], basis=len(same),
                        revenue=round(sum(revd.get(s, 0) * w for s, w in zip(same, wts)) / W) if same else 0, items=items[:8]))
    return 200, {"days": out, "data_days": len(days_with)}

def owner_get(h, d, sec):
    if sec == "dashboard": return api_dashboard(h, d)
    if sec == "menu":
        so = soldout_map(); rows = q("SELECT * FROM menu ORDER BY sort, id")
        for r in rows: r["auto_soldout"] = so.get(r["id"], "")
        return 200, {"rows": rows}
    if sec == "ingredients": return 200, {"rows": ingredients_full(), "expiry_days": setting("expiry_days", "3")}
    if sec == "recipes": return 200, {"rows": recipe_costs(), "ings": q("SELECT id, name, unit, unit_cost FROM ingredients ORDER BY name")}
    if sec == "production": return api_production(h, d)
    if sec == "purchase": return api_purchase(h, d)
    if sec == "staff": return 200, {"rows": q("SELECT id, name, wage, active FROM staff ORDER BY active DESC, name")}
    if sec == "attendance": return api_attendance(h, d)
    if sec == "checklist":
        day = d.get("day") or today()
        items = q("SELECT * FROM checklist_items WHERE active=1 ORDER BY kind DESC, sort, id")
        log = {r["item_id"]: r for r in q("SELECT l.*, s.name FROM checklist_log l LEFT JOIN staff s ON s.id=l.staff_id WHERE day=?", day)}
        for it in items:
            l = log.get(it["id"]); it["done"] = bool(l); it["by"] = (l or {}).get("name") or ("사장님" if l else ""); it["at"] = (l or {}).get("done_at", "")[11:16]
        return 200, {"day": day, "items": items}
    if sec == "customers":
        return 200, {"rows": q("SELECT * FROM customers ORDER BY last DESC LIMIT 300"), "rate": setting("points_rate", "0")}
    if sec == "reservations": return 200, {"rows": q("SELECT * FROM reservations ORDER BY CASE WHEN status IN ('완료','취소') THEN 1 ELSE 0 END, pickup LIMIT 300")}
    if sec == "reviews":
        return 200, {"rows": q("SELECT r.*, m.name FROM reviews r LEFT JOIN menu m ON m.id=r.menu_id ORDER BY r.id DESC LIMIT 300"),
                     "avg": q("SELECT m.name, ROUND(AVG(r.rating),2) a, COUNT(*) n FROM reviews r JOIN menu m ON m.id=r.menu_id GROUP BY r.menu_id ORDER BY a DESC")}
    if sec == "forecast": return api_forecast(h, d)
    if sec == "payroll": return api_payroll(h, d)
    if sec == "orders":
        day = d.get("day") or today()
        rows = q("SELECT * FROM orders WHERE substr(created,1,10)=? ORDER BY id DESC", day)
        for r in rows: r["items"] = q("SELECT name, opts, unit_price, qty FROM order_items WHERE order_id=?", r["id"])
        return 200, {"day": day, "rows": rows}
    if sec == "settings":
        return 200, dict(wifi_ssid=setting("wifi_ssid"), wifi_pw=setting("wifi_pw"), wifi_show=setting("wifi_show", "1"),
                         cafe_name=setting("cafe_name"), notice=setting("notice"), points_rate=setting("points_rate"), tables=setting("tables", "10"),
                         expiry_days=setting("expiry_days", "3"), toss=bool(TOSS_CK and TOSS_SK),
                         resv_hours=setting("resv_hours", "11:00-19:00"), resv_min_days=setting("resv_min_days", "1"),
                         resv_closed=setting("resv_closed", "화"), resv_notice=setting("resv_notice"), **{"cal": resv_conf()})
    return 404, {"error": "없음"}

def num(v, default=0.0):
    try: return float(v)
    except Exception: return default

def owner_post(h, d, act):
    i = int(d.get("id") or 0)
    if act == "logout":
        h.set_cookie("sb_owner", "x", 0); h.set_cookie("sb_staff", "x", 0); return 200, {}
    if act == "logout_all":
        set_setting("auth_ver", str(int(setting("auth_ver", "1")) + 1)); return 200, {}
    if act == "menu_save":
        stock = None if d.get("track") != "count" else int(num(d.get("stock")))
        vals = (str(d.get("cat", "기타")).strip() or "기타", str(d.get("name", "")).strip(), int(num(d.get("price"))), str(d.get("descr", "")),
                str(d.get("options", "")), str(d.get("emoji", ""))[:4], 1 if d.get("soldout") else 0, stock, 1 if d.get("active", True) else 0, int(num(d.get("sort"))),
                1 if d.get("resv") else 0)
        if not vals[1]: return 400, {"error": "메뉴 이름을 넣어 주세요"}
        if i: x("UPDATE menu SET cat=?,name=?,price=?,descr=?,options=?,emoji=?,soldout=?,stock=?,active=?,sort=?,resv=? WHERE id=?", *vals, i)
        else: x("INSERT INTO menu(cat,name,price,descr,options,emoji,soldout,stock,active,sort,resv) VALUES(?,?,?,?,?,?,?,?,?,?,?)", *vals)
        return owner_get(h, d, "menu")
    if act == "menu_soldout":
        x("UPDATE menu SET soldout=? WHERE id=?", 1 if d.get("soldout") else 0, i); return owner_get(h, d, "menu")
    if act == "menu_delete":
        if q1("SELECT 1 FROM order_items WHERE menu_id=?", i): x("UPDATE menu SET active=0 WHERE id=?", i)   # 주문 기록이 있으면 숨기기만
        else: x("DELETE FROM menu WHERE id=?", i); x("DELETE FROM recipes WHERE menu_id=?", i)
        return owner_get(h, d, "menu")
    if act == "ing_save":
        vals = (str(d.get("name", "")).strip(), str(d.get("unit", "")).strip() or "개", num(d.get("min_qty")), num(d.get("unit_cost")),
                str(d.get("supplier", "")), str(d.get("memo", "")))
        if not vals[0]: return 400, {"error": "재료 이름을 넣어 주세요"}
        if i: x("UPDATE ingredients SET name=?,unit=?,min_qty=?,unit_cost=?,supplier=?,memo=? WHERE id=?", *vals, i)
        else:
            i = x("INSERT INTO ingredients(name,unit,min_qty,unit_cost,supplier,memo) VALUES(?,?,?,?,?,?)", *vals)
            if num(d.get("qty")) > 0:
                x("INSERT INTO lots(ing_id,qty,expiry,received,price) VALUES(?,?,?,?,?)", i, num(d.get("qty")), d.get("expiry") or None, ts(), 0)
        return owner_get(h, d, "ingredients")
    if act == "ing_delete":
        x("DELETE FROM ingredients WHERE id=?", i); x("DELETE FROM lots WHERE ing_id=?", i); x("DELETE FROM recipes WHERE ing_id=?", i)
        return owner_get(h, d, "ingredients")
    if act == "lot_add":
        """입고: 수량 · 유통기한 · 산 금액(선택) → 금액이 있으면 단위 원가 자동 갱신"""
        ing = int(d.get("ing_id") or 0); qty = num(d.get("qty")); price = int(num(d.get("price")))
        if not q1("SELECT 1 FROM ingredients WHERE id=?", ing) or qty <= 0: return 400, {"error": "수량을 넣어 주세요"}
        x("INSERT INTO lots(ing_id,qty,expiry,received,price,memo) VALUES(?,?,?,?,?,?)", ing, qty, d.get("expiry") or None, ts(), price, str(d.get("memo", "")))
        if price > 0: x("UPDATE ingredients SET unit_cost=? WHERE id=?", round(price / qty, 4), ing)
        return owner_get(h, d, "ingredients")
    if act == "lot_update":   # 재고 조사: 실제 남은 수량·유통기한으로 고치기
        x("UPDATE lots SET qty=?, expiry=? WHERE id=?", max(0, num(d.get("qty"))), d.get("expiry") or None, i); return owner_get(h, d, "ingredients")
    if act == "lot_delete":
        x("DELETE FROM lots WHERE id=?", i); return owner_get(h, d, "ingredients")
    if act == "recipe_save":
        mid = int(d.get("menu_id") or 0)
        with LOCK:
            x("DELETE FROM recipes WHERE menu_id=?", mid)
            for l in d.get("lines") or []:
                if int(l.get("ing_id") or 0) and num(l.get("amount")) > 0:
                    x("INSERT OR REPLACE INTO recipes(menu_id,ing_id,amount) VALUES(?,?,?)", mid, int(l["ing_id"]), num(l["amount"]))
        return owner_get(h, d, "recipes")
    if act == "production_save": return api_production_save(h, d)
    if act == "production_delete": return api_production_delete(h, d)
    if act == "staff_save":
        name = str(d.get("name", "")).strip(); pin = str(d.get("pin", "")).strip()
        if not name: return 400, {"error": "이름을 넣어 주세요"}
        if pin:
            if not re.fullmatch(r"\d{4,6}", pin): return 400, {"error": "PIN 은 숫자 4~6자리"}
            other = q1("SELECT id FROM staff WHERE pin_tag=?", pin_tag(pin))
            if other and other["id"] != i: return 400, {"error": "다른 직원이 쓰는 PIN 이에요"}
        if not i and not pin: return 400, {"error": "새 직원은 PIN 이 필요해요"}
        if i: x("UPDATE staff SET name=?, wage=?, active=? WHERE id=?", name, int(num(d.get("wage"))), 1 if d.get("active", True) else 0, i)
        else: i = x("INSERT INTO staff(name,wage,active) VALUES(?,?,1)", name, int(num(d.get("wage"))))
        if pin:
            s, hh = hash_pw(pin); x("UPDATE staff SET pin_salt=?, pin_hash=?, pin_tag=? WHERE id=?", s, hh, pin_tag(pin), i)
        return owner_get(h, d, "staff")
    if act == "staff_delete":
        x("UPDATE staff SET active=0 WHERE id=?", i); return owner_get(h, d, "staff")
    if act == "attendance_save":
        ci, co = str(d.get("clock_in", "")).replace("T", " "), str(d.get("clock_out", "") or "").replace("T", " ")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?", ci): return 400, {"error": "출근 시각을 넣어 주세요"}
        ci = ci[:16] + ":00"; co = (co[:16] + ":00") if co else None
        if co and co < ci: return 400, {"error": "퇴근이 출근보다 빨라요"}
        if i: x("UPDATE attendance SET staff_id=?, clock_in=?, clock_out=?, memo=? WHERE id=?", int(d.get("staff_id") or 0), ci, co, str(d.get("memo", "")), i)
        else: x("INSERT INTO attendance(staff_id,clock_in,clock_out,memo) VALUES(?,?,?,?)", int(d.get("staff_id") or 0), ci, co, str(d.get("memo", "")))
        return api_attendance(h, {"month": ci[:7]})
    if act == "attendance_delete":
        x("DELETE FROM attendance WHERE id=?", i); return api_attendance(h, d)
    if act == "checklist_save":
        if not str(d.get("text", "")).strip(): return 400, {"error": "내용을 넣어 주세요"}
        x("INSERT INTO checklist_items(kind,text,sort) VALUES(?,?,?)", "close" if d.get("kind") == "close" else "open", str(d["text"]).strip(), 99)
        return owner_get(h, d, "checklist")
    if act == "checklist_delete":
        x("UPDATE checklist_items SET active=0 WHERE id=?", i); return owner_get(h, d, "checklist")
    if act == "checklist_toggle":
        return api_checklist_toggle(h, d, 0)
    if act == "points":
        p = norm_phone(d.get("phone")); delta = int(num(d.get("delta")))
        c = q1("SELECT * FROM customers WHERE phone=?", p)
        if not c: return 400, {"error": "등록된 번호가 아니에요"}
        if c["points"] + delta < 0: return 400, {"error": f"포인트가 부족해요 (보유 {c['points']})"}
        x("UPDATE customers SET points=points+? WHERE phone=?", delta, p)
        x("INSERT INTO point_log(phone,delta,memo,at) VALUES(?,?,?,?)", p, delta, str(d.get("memo") or ("사용" if delta < 0 else "수동 적립")), ts())
        return owner_get(h, d, "customers")
    if act == "reservation_status":
        x("UPDATE reservations SET status=? WHERE id=?", str(d.get("status", "접수"))[:10], i); return owner_get(h, d, "reservations")
    if act == "review_delete":
        x("DELETE FROM reviews WHERE id=?", i); return owner_get(h, d, "reviews")
    if act == "order_cancel":
        o = q1("SELECT * FROM orders WHERE id=?", i)
        if o and o["status"] != "canceled":
            x("UPDATE orders SET status='canceled' WHERE id=?", i); apply_stock(i, +1)
            if o["points_given"] and o["phone"]:
                pts = int(o["total"] * float(setting("points_rate", "0") or 0) / 100)
                x("UPDATE customers SET points=MAX(0, points-?), spent=spent-?, visits=MAX(0, visits-1) WHERE phone=?", pts, o["total"], o["phone"])
                x("INSERT INTO point_log(phone,delta,memo,at) VALUES(?,?,?,?)", o["phone"], -pts, f"주문 #{i} 취소", ts())
        return owner_get(h, {"day": (o or {}).get("created", "")[:10]}, "orders")
    if act == "day_toggle":   # 캘린더에서 날짜 누르기: 쉬는 요일 → 이날만 영업 / 영업일 → 임시 휴무
        dt = str(d.get("date", ""))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt): return 400, {"error": "날짜 오류"}
        c = resv_conf(); weekly_off = "월화수목금토일"[date.fromisoformat(dt).weekday()] in c["closed"]
        k = "open_dates" if weekly_off else "off_dates"
        cur = set(c[k]); cur.symmetric_difference_update({dt})
        keep = {x_ for x_ in cur if x_ >= (now() - timedelta(days=60)).strftime("%Y-%m-%d")}   # 지난 날짜는 정리
        set_setting(k, ",".join(sorted(keep)))
        return 200, {"cal": resv_conf()}
    if act == "settings":
        for k in ("pay_start", "pay_day", "pay_juhyu", "pay_tax", "wifi_ssid", "wifi_pw", "wifi_show", "cafe_name", "notice", "points_rate", "tables", "expiry_days", "resv_hours", "resv_min_days", "resv_closed", "resv_notice"):
            if k in d: set_setting(k, str(d[k])[:300])
        if d.get("new_password"):
            if len(d["new_password"]) < 6: return 400, {"error": "비밀번호는 6자 이상"}
            s, hh = hash_pw(d["new_password"]); set_setting("owner_salt", s); set_setting("owner_hash", hh)
            set_setting("auth_ver", str(int(setting("auth_ver", "1")) + 1))
            h.set_cookie("sb_owner", make_cookie("owner", 0, OWNER_H), OWNER_H)
        return owner_get(h, d, "settings")
    return 404, {"error": "없음"}

# ---------------------------------------------------------------- HTTP
def page(name):
    """화면 파일 + css·js 주소 뒤에 ?v=수정시각 → 업데이트하면 폰·PC 가 예전 파일을 안 쓰고 바로 새로 받음"""
    html = open(os.path.join(STATIC, name), encoding="utf-8").read()
    for url, (f, _) in FILES.items():
        v = int(os.path.getmtime(os.path.join(STATIC, f)))
        html = html.replace(f'"{url}"', f'"{url}?v={v}"').replace(f"'{url}'", f"'{url}?v={v}'")
    return html.encode()

class H(BaseHTTPRequestHandler):
    server_version = "sandbear"
    def log_message(self, *a): pass

    @property
    def ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def set_cookie(self, name, val, hours):
        self._cookies.append(f"{name}={val}; Path=/; Max-Age={int(hours * 3600)}; HttpOnly; SameSite=Lax"
                             + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))

    def send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
        if isinstance(body, (dict, list)): body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        for c in self._cookies: self.send_header("Set-Cookie", c)
        self.end_headers(); self.wfile.write(body)

    def role(self):
        c = self.headers.get("Cookie")
        if read_cookie(c, "sb_owner"): return "owner", 0
        s = read_cookie(c, "sb_staff")
        if s and q1("SELECT 1 FROM staff WHERE id=? AND active=1", s[1]): return "staff", s[1]
        return None, None

    def do_GET(self):
        self._cookies = []
        u = urlparse(self.path); p = u.path.rstrip("/") or "/"; d = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p in PAGES:
                return self.send(200, page(PAGES[p]), "text/html; charset=utf-8")
            if p in FILES:
                f, ct = FILES[p]; return self.send(200, open(os.path.join(STATIC, f), "rb").read(), ct, "public, max-age=300")
            if p == "/api/menu": return self.send(*self.wrap(api_menu(self, d)))
            m = re.fullmatch(r"/api/order/([\w-]{8,40})", p)
            if m: return self.send(*self.wrap(api_order_get(self, d, m.group(1))))
            if p == "/api/points": return self.send(*self.wrap(api_points(self, d)))
            if p == "/api/reserve-info": return self.send(*self.wrap(api_reserve_info(self, d)))
            role, sid = self.role()
            if p == "/api/me": return self.send(200, {"role": role, "name": (q1("SELECT name FROM staff WHERE id=?", sid) or {}).get("name") if role == "staff" else ("사장님" if role else None)})
            if p == "/api/kds/orders":
                return self.send(200, kds_orders()) if role else self.send(401, {"error": "로그인 필요"})
            if p == "/api/checklist":
                return self.send(*self.wrap(api_checklist(self, d))) if role else self.send(401, {"error": "로그인 필요"})
            m = re.fullmatch(r"/api/admin/(\w+)", p)
            if m:
                if role != "owner": return self.send(401, {"error": "사장님 로그인 필요"})
                return self.send(*self.wrap(owner_get(self, d, m.group(1))))
            self.send(404, {"error": "없는 주소"})
        except Exception as e:
            print("GET 오류", p, repr(e), flush=True); self.send(500, {"error": "서버 오류"})

    def do_POST(self):
        self._cookies = []
        p = urlparse(self.path).path.rstrip("/")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000: return self.send(413, {"error": "too large"})
            d = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(d, dict): d = {}
        except Exception: return self.send(400, {"error": "잘못된 요청"})
        # 다른 사이트에서 몰래 보내는 요청 막기 (Origin 이 있으면 같은 주소여야 함)
        org = self.headers.get("Origin")
        if org and urlparse(org).netloc != self.headers.get("Host"): return self.send(403, {"error": "forbidden"})
        try:
            pub = {"/api/order": api_order, "/api/pay/confirm": api_pay_confirm, "/api/pay/fail": api_pay_fail, "/api/reservation": api_reservation,
                   "/api/review": api_review, "/api/staff/login": api_staff_login, "/api/staff/clock": api_clock, "/api/admin/login": api_admin_login}
            if p in pub: return self.send(*self.wrap(pub[p](self, d)))
            role, sid = self.role()
            if p in ("/api/logout", "/api/staff/logout"):   # 로그아웃 = 직원·사장님 로그인 둘 다 풀기
                self.set_cookie("sb_staff", "x", 0); self.set_cookie("sb_owner", "x", 0); return self.send(200, {})
            if p == "/api/kds/update":
                return self.send(*self.wrap(api_kds_update(self, d))) if role else self.send(401, {"error": "로그인 필요"})
            if p == "/api/checklist/toggle":
                return self.send(*self.wrap(api_checklist_toggle(self, d, sid or 0))) if role else self.send(401, {"error": "로그인 필요"})
            m = re.fullmatch(r"/api/admin/(\w+)", p)
            if m:
                if role != "owner": return self.send(401, {"error": "사장님 로그인 필요"})
                return self.send(*self.wrap(owner_post(self, d, m.group(1))))
            self.send(404, {"error": "없는 주소"})
        except (ValueError, TypeError) as e:
            self.send(400, {"error": "입력값을 확인해 주세요"})
        except Exception as e:
            print("POST 오류", p, repr(e), flush=True); self.send(500, {"error": "서버 오류"})

    @staticmethod
    def wrap(r): return r if isinstance(r, tuple) else (200, r)

def set_owner_pw():
    pw = getpass.getpass("사장님 비밀번호 (6자 이상): ")
    if len(pw) < 6 or pw != getpass.getpass("한 번 더: "): sys.exit("❌ 6자 이상, 두 번 똑같이 넣어 주세요")
    s, h = hash_pw(pw); set_setting("owner_salt", s); set_setting("owner_hash", h)
    set_setting("auth_ver", str(int(setting("auth_ver", "1")) + 1))
    print("✅ 사장님 비밀번호 저장 (기존 로그인은 모두 풀림)")

if __name__ == "__main__":
    if "--set-owner-pw" in sys.argv: set_owner_pw(); sys.exit()
    print(f"🐻 샌드베어 사이트 시작 http://{HOST}:{PORT}  (결제: {'토스페이먼츠' if TOSS_CK and TOSS_SK else '카운터 결제'})", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
