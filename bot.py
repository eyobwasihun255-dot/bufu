

import os
import uuid
import json
from datetime import datetime, timezone
from dateutil import parser as dateparser
from math import radians, cos, sin, asin, sqrt

from flask import Flask, request, abort
import telebot
from telebot import types

import firebase_admin
from firebase_admin import credentials, db as rtdb, storage as fb_storage
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import json



load_dotenv()

# =============== CONFIG ===============

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")  # REQUIRED
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")  # optional
PORT = int(os.getenv("PORT", 5000))

# Admins: comma-separated list of telegram IDs (e.g. "12345,67890")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()]

ROOT_KEY = "foodbot"
USERS_KEY = "users"
RESTAURANTS_KEY = "restaurants"
ORDERS_KEY = "orders"
FOODS_KEY = "foods"
# Predefined food choices used during restaurant creation (editable)
PRESET_FOODS = [
    {"name": "Burger", "price": "5.00"},
    {"name": "Pizza", "price": "7.50"},
    {"name": "Sandwich", "price": "4.00"},
    {"name": "Pasta", "price": "6.50"},
    {"name": "Salad", "price": "3.50"},
]

# =============== INIT ===============

if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("<"):
    raise SystemExit("Set TELEGRAM_TOKEN env var or edit the file.")

if not WEBHOOK_URL or WEBHOOK_URL.startswith("<"):
    raise SystemExit("Set WEBHOOK_URL env var or edit the file.")

if not FIREBASE_DB_URL:
    raise SystemExit("Set FIREBASE_DB_URL env var (RTDB URL).")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
app = Flask(__name__)

# Initialize Firebase app (add storageBucket if provided)
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(os.getenv("FIREBASE_CRED_JSON")))
    fb_options = {"databaseURL": FIREBASE_DB_URL}
    if FIREBASE_STORAGE_BUCKET:
        fb_options["storageBucket"] = FIREBASE_STORAGE_BUCKET
    firebase_admin.initialize_app(cred, fb_options)

root_ref = rtdb.reference(ROOT_KEY)
foods_ref = rtdb.reference(FOODS_KEY)
scheduler = BackgroundScheduler()
scheduler.start()

# =============== UTILITIES ===============


def generate_id():
    return uuid.uuid4().hex

def make_order_id():
    return uuid.uuid4().hex[:12].upper()

def make_rest_id():
    return uuid.uuid4().hex[:12]

def haversine(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return 6371 * c

# RTDB reference helpers

def users_ref():
    return root_ref.child(USERS_KEY)

def restaurants_ref():
    return root_ref.child(RESTAURANTS_KEY)

def orders_ref():
    return root_ref.child(ORDERS_KEY)

def get_user_ref(user_id):
    return users_ref().child(str(user_id))

def get_restaurant_ref(restaurant_id):
    return restaurants_ref().child(str(restaurant_id))

def get_order_ref(order_id):
    return orders_ref().child(str(order_id))

def is_manager(user_id):
    u = get_user_ref(user_id).get() or {}
    return u.get("manager") == True

RTDB_TIMESTAMP = {".sv": "timestamp"}

# =============== Firebase Storage helper for photos ===============

def upload_telegram_photo_to_firebase(file_id, dest_path=None):
 
    
    try:
        if not FIREBASE_STORAGE_BUCKET:
            return None

        file_info = bot.get_file(file_id)
        file_path = file_info.file_path
        file_bytes = bot.download_file(file_path)

        bucket = fb_storage.bucket()
        # create a unique path if none provided
        if not dest_path:
            dest_path = f"restaurants/{uuid.uuid4().hex}.jpg"

        blob = bucket.blob(dest_path)
        blob.upload_from_string(file_bytes, content_type="image/jpeg")
        # Optionally make it public (depends on bucket rules). Here we create a gs:// path.
        return f"gs://{bucket.name}/{dest_path}"
    except Exception as e:
        print("upload_telegram_photo_to_firebase error:", e)
        return None

# Helper: add order to restaurant list

def add_order_to_restaurant(rest_id, order_id):
    ref = get_restaurant_ref(rest_id).child("orders")

    def txn(current):
        if current is None:
            return [order_id]
        if isinstance(current, dict):
            current = list(current.values())
        if order_id not in current:
            current.append(order_id)
        return current

    try:
        ref.transaction(txn)
    except Exception as e:
        print("add_order_to_restaurant error:", e)

def remove_order_from_restaurant(rest_id, order_id):
    ref = get_restaurant_ref(rest_id).child("orders")

    def txn(current):
        if not current:
            return None
        if isinstance(current, dict):
            current = list(current.values())
        return [o for o in current if o != order_id]

    try:
        ref.transaction(txn)
    except Exception as e:
        print("remove_order_from_restaurant error:", e)

def increment_rest_orders_count(rest_id, delta=1):
    ref = get_restaurant_ref(rest_id).child("orders_count")

    def txn(current):
        if current is None:
            current = 0
        return int(current) + int(delta)

    try:
        ref.transaction(txn)
    except Exception as e:
        print("increment_rest_orders_count error:", e)

# =============== NOTIFICATION ===============

def send_restaurant_notification(restaurant_chat_id, order_id):
    try:
        order = get_order_ref(order_id).get()
        if not order or order.get("status") == "served":
            return

        keyboard = types.InlineKeyboardMarkup()
        callback = json.dumps({"action": "mark_served", "order_id": order_id})
        keyboard.add(types.InlineKeyboardButton("Mark as served ✅", callback_data=callback))

        text = f"New order:\nOrder ID: {order_id}\nItems:\n"
        for item in order.get("items", []):
            text += f"- {item['name']} x{item['qty']} — {item['price']}\n"

        text += f"\nTotal: {order['total_price']}\nUser: {order['user_name']} — {order['phone']}"

        bot.send_message(restaurant_chat_id, text, reply_markup=keyboard)

    except Exception as e:
        print("send_restaurant_notification error:", e)

def schedule_order_notification(order_id, run_at_dt, restaurant_chat_id):
    if run_at_dt.tzinfo is None:
        run_at_dt = run_at_dt.replace(tzinfo=timezone.utc)

    scheduler.add_job(
        send_restaurant_notification,
        trigger='date',
        run_date=run_at_dt,
        args=[restaurant_chat_id, order_id],
        id=f"order_{order_id}",
        replace_existing=True
    )
PAGE_SIZE = 10

def build_restaurant_page(page=0, search=None):
    rests = list(get_all_restaurants().items())

    # search filter
    if search:
        rests = [(rid, r) for rid, r in rests if search.lower() in r["name"].lower()]

    # sort alphabetically
    rests.sort(key=lambda x: x[1]["name"].lower())

    total_pages = max(1, (len(rests) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    kb = types.InlineKeyboardMarkup()

    for rid, r in rests[start:end]:
        kb.add(types.InlineKeyboardButton(
            r["name"],
            callback_data=json.dumps({"action": "edit_rest", "rid": rid})
        ))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅ Prev", callback_data=json.dumps({
            "action": "edit_page", "page": page - 1, "search": search
        })))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ➡", callback_data=json.dumps({
            "action": "edit_page", "page": page + 1, "search": search
        })))

    if nav:
        kb.row(*nav)

    kb.add(types.InlineKeyboardButton("🔍 Search", callback_data=json.dumps({
        "action": "edit_search"
    })))

    return kb, total_pages

def build_food_page( page=0):
    foods = get_foods_ref()

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    kb = types.InlineKeyboardMarkup()
    for idx, f in enumerate(foods[start:end], start=start):
        kb.add(types.InlineKeyboardButton(
            f["name"],
            callback_data=json.dumps({
                "action": "add_existing_food",
                "rid": rid,
                "index": idx
            })
        ))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(
            "⬅ Prev",
            callback_data=json.dumps({"action": "food_page", "rid": rid, "page": page - 1})
        ))
    if end < len(foods):
        nav.append(types.InlineKeyboardButton(
            "Next ➡",
            callback_data=json.dumps({"action": "food_page", "rid": rid, "page": page + 1})
        ))

    if nav:
        kb.row(*nav)

    return kb

# =============== STATE HANDLING ===============

def set_user_state(user_id, state_dict):
    try:
        get_user_ref(user_id).child("state").update(state_dict)
    except:
        get_user_ref(user_id).child("state").set(state_dict)

def get_user_state(user_id):
    return get_user_ref(user_id).child("state").get() or {}

def clear_user_state(user_id):
    try:
        get_user_ref(user_id).child("state").set({})
    except:
        pass

# =============== /start COMMAND ===============

@bot.message_handler(commands=['start'])
def handle_start(message):
    user = message.from_user
    user_data = get_user_ref(user.id).get()

    if not user_data or not user_data.get("phone"):
        markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        markup.add(types.KeyboardButton("Share contact 📱", request_contact=True))
        bot.send_message(
            user.id,
            "Welcome! Please share your contact to continue.",
            reply_markup=markup
        )
        set_user_state(user.id, {"awaiting_contact": True})
        return

    bot.send_message(user.id, "Welcome back! Use /menu to continue.")
    set_user_state(user.id, {})

# =============== /menu COMMAND ===============

@bot.message_handler(commands=['menu'])
def handle_menu(message):
    user_data = get_user_ref(message.from_user.id).get()
    if not user_data or not user_data.get("phone"):
        bot.send_message(message.from_user.id, "Please register using /start")
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Search by restaurant", "Search by food")
    markup.add("Search by location")
    markup.add("Top-rated", "Least-ordered (fastest)")
    markup.add("Closest restaurants", "My orders")

    # show admin-only options if user is in ADMINS
    if message.from_user.id in ADMINS:
        markup.add("/addrestaurant", "/listrestaurants")

    bot.send_message(message.from_user.id, "Choose an option:", reply_markup=markup)

# =============== CONTACT ===============

@bot.message_handler(content_types=['contact'])
def handle_contact(message):
    contact = message.contact
    if not contact:
        bot.send_message(message.from_user.id, "Contact not received.")
        return

    user_doc = {
        "user_id": message.from_user.id,
        # ensure we store user's Telegram name as well
        "name": message.from_user.full_name,
        "phone": contact.phone_number,
        "registered_at": RTDB_TIMESTAMP
    }

    try:
        get_user_ref(message.from_user.id).update(user_doc)
    except:
        get_user_ref(message.from_user.id).set(user_doc)

    bot.send_message(message.from_user.id, "Registered! Use /menu to continue.")
    set_user_state(message.from_user.id, {})




@bot.message_handler(content_types=['location'])
def handle_location(message):
    loc = message.location
    if not loc:
        bot.send_message(message.from_user.id, "Location missing.")
        return

    # fetch state to detect if this location was requested for a special flow
    state = get_user_state(message.from_user.id) or {}
        # ===== MANAGER RESTAURANT REGISTRATION LOCATION STEP =====
    if state.get("reg_rest_step") == "location":
        data = state["new_rest"]
        data["location"] = {"lat": loc.latitude, "lon": loc.longitude}

        # Go to description step
        set_user_state(message.from_user.id, {
            "reg_rest_step": "description",
            "new_rest": data
        })

        bot.send_message(message.from_user.id, "Write restaurant description:")
        return

    # if user is in flow to add restaurant location (admin)
    if state.get("awaiting_rest_location"):
        # save pending restaurant location
        pending = state.get("pending_rest") or {}
        pending["location"] = {"lat": loc.latitude, "lon": loc.longitude}
        set_user_state(message.from_user.id, {"pending_rest": pending, "awaiting_rest_location": False})
        bot.send_message(message.from_user.id, "Restaurant location saved.")
        # continue the admin flow by asking for manager id
        bot.send_message(message.from_user.id, "Send the manager's TELEGRAM ID (numeric). If manager uses a chat id, send that number.")
        set_user_state(message.from_user.id, {"pending_rest": pending, "awaiting_rest_manager": True})
        return
    if state.get("add_rest") and state.get("step") == "location":
        data = state["data"]
        data["location"] = {
            "lat": loc.latitude,
            "lon": loc.longitude
        }

        set_user_state(message.from_user.id, {
            "add_rest": True,
            "step": "manager",
            "data": data
        })

        bot.send_message(
            message.from_user.id,
            "Send manager phone number or username:"
        )
        return



    # if user requested to share their own location for search
    if state.get("awaiting_search_by_location"):
        # save user last_location
        get_user_ref(message.from_user.id).child("last_location").set({
            "lat": loc.latitude,
            "lon": loc.longitude
        })
        set_user_state(message.from_user.id, {})
        # run the search (within 10 km for example)
        find_restaurants_near_user(message.from_user.id, loc.latitude, loc.longitude)
        return

    # default behavior: save last location
    get_user_ref(message.from_user.id).child("last_location").set({
        "lat": loc.latitude,
        "lon": loc.longitude
    })

    bot.send_message(message.from_user.id, "Location saved!")

# =============== NEW: Search by location helper ===============

def find_restaurants_near_user(user_id, lat, lon, radius_km=10.0):
    docs = restaurants_ref().get() or {}
    distances = []

    for rid, r in docs.items():
        pos = r.get("location")
        if not pos:
            continue
        dist = haversine(lon, lat, pos.get("lon"), pos.get("lat"))
        if dist <= radius_km:
            distances.append((dist, rid, r))

    distances.sort(key=lambda x: x[0])

    if not distances:
        bot.send_message(user_id, f"No restaurants found within {radius_km} km.")
        return

    msg = f"Restaurants within {radius_km} km:\n"
    kb = types.InlineKeyboardMarkup()
    for dist, rid, r in distances[:15]:
        msg += f"- {r['name']} — {dist:.2f} km\n"
        kb.add(types.InlineKeyboardButton(r["name"], callback_data=json.dumps({"action": "select_rest", "rid": rid})))

    bot.send_message(user_id, msg, reply_markup=kb)

# =============== /addrestaurant (admin) ===============
def get_all_restaurants():
    return restaurants_ref().get() or {}

def get_all_foods():
    return root_ref.child("foods").get() or {}

# =============== LIST RESTAURANTS (admin helper) ===============
@bot.message_handler(commands=['add'])
def cmd_add(message):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("🍽 Restaurant", "🍔 Food")
    bot.send_message(message.from_user.id, "What do you want to add?", reply_markup=kb)

    set_user_state(message.from_user.id, {
        "add_flow": True
    })

@bot.message_handler(commands=["edit"])
def edit_restaurant_cmd(message):
    kb, total = build_restaurant_page(page=0)
    bot.send_message(
        message.from_user.id,
        "✏️ Select restaurant to edit (Page 1):",
        reply_markup=kb
    )

@bot.message_handler(commands=["delete"])
def delete_restaurant_cmd(message):
    user_id = message.from_user.id

    kb = types.InlineKeyboardMarkup()
    for rid, r in get_all_restaurants().items():
        kb.add(types.InlineKeyboardButton(
            f"🗑 {r['name']}",
            callback_data=json.dumps({
                "action": "confirm_delete_rest",
                "rid": rid
            })
        ))

    bot.send_message(
        user_id,
        "⚠️ Select restaurant to delete:",
        reply_markup=kb
    )
@bot.message_handler(commands=["add_food"])
def add_food_cmd(message):
    user_id = message.from_user.id
    set_user_state(user_id, {"awaiting_food_data": True})

    bot.send_message(
        user_id,
        "🍔 Send food as:\n"
        "Name | People | Description\n\n"
        "Example:\nBurger | 1 | Beef burger with cheese"
    )

@bot.message_handler(commands=['listrestaurants'])
def cmd_list_restaurants(message):
    if message.from_user.id not in ADMINS:
        bot.send_message(message.from_user.id, "Not authorized.")
        return

    docs = restaurants_ref().get() or {}
    if not docs:
        bot.send_message(message.from_user.id, "No restaurants yet.")
        return

    msg = "Restaurants:\n"
    for rid, r in docs.items():
        msg += f"- {r.get('name')} (id: {rid})\n"
    bot.send_message(message.from_user.id, msg)

# =============== MAIN TEXT HANDLER ===============
def get_foods_ref():
    return rtdb.reference("foods")
def get_food_ref(fid):
    return get_foods_ref().child(fid)
def parse_price(text):
    text = text.strip()
    text = text.replace(",", "")  # allow "1,000"
    if not text.replace(".", "", 1).isdigit():
        return None
    return float(text)

@bot.message_handler(func=lambda m: True)
def general_text_handler(message):
    user = message.from_user
    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""
    state = get_user_state(user.id)
    print(state)
    if state.get("add_food_mode") :
        rid = state["rid"]

        if state["step"] == "name":
            state["food"] = {"name": text}
            state["step"] = "price"
            set_user_state(user_id, state)
            bot.send_message(user_id, "💰 Send food price:")
            return

        if state["step"] == "price":
            price = parse_price(message.text)
            if price is None or price <= 0:
                bot.send_message(user_id, "❌ Invalid price. Send a number like 5 or 5.50")
                return

            rid = state["rid"]
            food = state["food"]
            food["price"] = price

            # ✅ SAVE INSIDE THE RESTAURANT
            food_id = uuid.uuid4().hex[:8]

            get_restaurant_ref(rid).child("foods").child(food_id).set(food)

            clear_user_state(user_id)
            bot.send_message(user_id, f"✅ {food['name']} added to restaurant.")
            return

    if state.get("awaiting_search"):
        search_type = state.get("awaiting_search_type")

        # clear search state FIRST (important)
        set_user_state(user.id, {})

        if search_type == "restaurant":
            handle_search_restaurant_query(user.id, text)
            return

        if search_type == "food":
            handle_search_food_query(user.id, text)
            return
    # ======================================================
    # ✏️ EDIT RESTAURANT TEXT FLOW
    # ======================================================
    if state.get("editing_rest"):
        rid = state["rid"]

        # ---- Edit name ----
        if text == "✏️ Name":
            set_user_state(user.id, {
                "editing_rest": True,
                "rid": rid,
                "edit_step": "name"
            })
            bot.send_message(user.id, "✏️ Send new restaurant name:")
            return

        # ---- Edit location ----
        if text == "📍 Location":
            set_user_state(user.id, {
                "editing_rest": True,
                "rid": rid,
                "edit_step": "location"
            })
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add(types.KeyboardButton("📍 Share location", request_location=True))
            bot.send_message(user.id, "📍 Share new location:", reply_markup=kb)
            return

        # ---- Edit image ----
        if text == "🖼 Image":
            set_user_state(user.id, {
                "editing_rest": True,
                "rid": rid,
                "edit_step": "image"
            })
            bot.send_message(user.id, "🖼 Send new restaurant photo:")
            return
        
        if text == "➕ Add Food":
            set_user_state(user.id, {
                "editing_rest": True,
                "rid": rid
                   })
            kb = types.InlineKeyboardMarkup()
           
            kb.add(
                types.InlineKeyboardButton(
                    "➕ New Food",
                    callback_data=json.dumps({
                        "action": "add_food_new",
                        "rid": rid
                    })
                )
            )

            bot.send_message(user.id, "🍔 How do you want to add food?", reply_markup=kb)
            return


        # ---- Cancel ----
        if text == "❌ Cancel":
            clear_user_state(user.id)
            bot.send_message(user.id, "❌ Edit cancelled.")
            return
    if state.get("editing_rest") and state.get("edit_step") == "name":
        rid = state["rid"]
        get_restaurant_ref(rid).child("name").set(text)

        clear_user_state(user.id)
        bot.send_message(user.id, "✅ Restaurant name updated.")
        return
    if state.get("awaiting_edit_search"):
        clear_user_state(user_id)
        kb, total = build_restaurant_page(0, text)
        bot.send_message(
            user_id,
            f"🔍 Search results for '{text}':",
            reply_markup=kb
        )
        return

    if state.get("awaiting_food_data"):
        try:
            name, people, desc = map(str.strip, message.text.split("|"))
        except ValueError:
            bot.send_message(
                user.id,
                "❌ Invalid format.\nUse:\nName | People | Description"
            )
            return

        fid = generate_id()

        get_foods_ref().child(fid).set({
            "name": name,
            "people": int(people),
            "description": desc
        })

        clear_user_state(user.id)
        bot.send_message(user.id, f"✅ Food '{name}' added successfully")
        return
    if state.get("reg_rest_step"):
        step = state["reg_rest_step"]
        data = state["new_rest"]

        # STEP 1: NAME
        if step == "name":
            name = text.strip()

            # check unique
            all_rest = restaurants_ref().get() or {}
            if any(r.get("name", "").lower() == name.lower() for r in all_rest.values()):
                bot.send_message(user.id, "❌ A restaurant with this name already exists. Send a different name.")
                return

            data["name"] = name
            set_user_state(user.id, {"reg_rest_step": "foods", "new_rest": data})

            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Add preset food", callback_data=json.dumps({"action": "mgr_add_preset"})))
            kb.add(types.InlineKeyboardButton("Add custom food", callback_data=json.dumps({"action": "mgr_add_custom"})))
            kb.add(types.InlineKeyboardButton("Done", callback_data=json.dumps({"action": "mgr_food_done"})))

            bot.send_message(user.id, "Now choose foods:", reply_markup=kb)
            return
        if step == "custom_food":
            try:
                name, ingredients, price, qty = [p.strip() for p in text.split("|")]
            except:
                bot.send_message(user.id, "❌ Invalid format. Use:\nName | Ingredients | Price | Quantity")
                return

            data.setdefault("foods", []).append({
                "name": name,
                "ingredients": ingredients,
                "price": price,
                "quantity": qty
            })

            set_user_state(user.id, {"reg_rest_step": "foods", "new_rest": data})
            bot.send_message(user.id, f"Added custom food: {name}")
            return
        
        if step == "description":
            data["description"] = text
            data["manager_id"] = user.id

            set_user_state(user.id, {})

            # Send to admin for approval
            admins = ADMINS
            kb = types.InlineKeyboardMarkup()
            cb = json.dumps({"action": "approve_rest", "data": data})
            kb.add(
                types.InlineKeyboardButton("Approve ✅", callback_data=cb),
                types.InlineKeyboardButton("Reject ❌", callback_data=json.dumps({"action": "reject_rest"}))
            )

            summary = f"New Restaurant Request:\n\n" \
                    f"Name: {data['name']}\n" \
                    f"Manager: {user.id}\n" \
                    f"Foods: {len(data.get('foods', []))}\n" \
                    f"Description: {data['description']}\n"

            for admin in admins:
                bot.send_message(admin, summary, reply_markup=kb)

            bot.send_message(user.id, "📝 Your restaurant was sent for admin approval.")
            return
    if state.get("add_flow"):
        if text == "🍽 Restaurant":
            set_user_state(user.id, {
                "add_rest": True,
                "step": "name",
                "data": {},
                "add_flow": False   # ✅ CLEAR FLOW
            })
            bot.send_message(user.id, "Send restaurant name:")
            return
        if text == "🍔 Food":
            set_user_state(user.id, {
                "add_food": True,
                "step": "name",
                "data": {},
                "add_flow": False   # ✅ CLEAR FLOW
            })
            bot.send_message(user.id, "Send food name:")
            return
    if state.get("add_rest") and state.get("step"):
        data = state.get("data", {})

        # STEP 1: NAME
        if state["step"] == "name":
            data["name"] = text
            set_user_state(user.id, {
                "add_rest": True,
                "step": "phone",
                "data": data
            })
            bot.send_message(user.id, "Send restaurant phone number:")
            return

        # STEP 2: PHONE
        if state["step"] == "phone":
            data["phone"] = text
            set_user_state(user.id, {
                "add_rest": True,
                "step": "location",
                "data": data
            })

            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add(types.KeyboardButton("📍 Share location", request_location=True))
            bot.send_message(user.id, "Share restaurant location:", reply_markup=kb)
            return

        # STEP 4: MANAGER SEARCH
        if state["step"] == "manager":
            query = text.lower()
            users = users_ref().get() or {}

            matches = []
            for uid, u in users.items():
                if query in str(u.get("phone", "")).lower() or query in str(u.get("name", "")).lower():
                    matches.append((uid, u))

            if not matches:
                bot.send_message(user.id, "❌ No user found. Send phone number or username again.")
                return

            kb = types.InlineKeyboardMarkup()
            for uid, u in matches[:5]:
                kb.add(types.InlineKeyboardButton(
                    f"{u.get('name')} ({u.get('phone')})",
                    callback_data=json.dumps({
                        "action": "add_rest_select_manager",
                        "uid": uid
                    })
                ))

            bot.send_message(user.id, "Select restaurant manager:", reply_markup=kb)
            return
    if state.get("add_food"):
        data = state["data"]

        if state["step"] == "name":
            data["name"] = text
            set_user_state(user.id, {"add_food": True, "step": "ingredients", "data": data})
            bot.send_message(user.id, "Send ingredients:")
            return

        if state["step"] == "ingredients":
            data["ingredients"] = text
            set_user_state(user.id, {"add_food": True, "step": "people", "data": data})
            bot.send_message(user.id, "How many people does this food serve?")
            return

        if state["step"] == "people":
            data["people"] = int(text)

            food_id = uuid.uuid4().hex[:8]
            root_ref.child("foods").child(food_id).set(data)

            clear_user_state(user.id)
            bot.send_message(user.id, "✅ Food added to global food list!")
            return
    if state.get("awaiting_food_price"):
        try:
            price = float(message.text)
        except:
            bot.send_message(user_id, "❌ Invalid price.")
            return

        rid = state["rid"]
        food = state["food_data"]
        food["price"] = price

        ref = get_restaurant_ref(rid).child("foods")

        def txn(current):
            if current is None:
                current = []
            if any(f["name"] == food["name"] for f in current):
                return current
            current.append(food)
            return current

        ref.transaction(txn)

        clear_user_state(user_id)
        bot.send_message(user_id, f"✅ *{food['name']}* added to restaurant.", parse_mode="Markdown")
        return

    if state.get("awaiting_schedule") and state.get("pending_order"):
        pending = state["pending_order"]

        if text.lower() in ("asap", "now"):
            run_at = datetime.utcnow().replace(tzinfo=timezone.utc)
        else:
            try:
                dt = dateparser.parse(text)
                if not dt:
                    raise Exception()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                run_at = dt
            except:
                bot.send_message(user.id, "Invalid time. Use 'ASAP' or YYYY-MM-DD HH:MM.")
                return

        order_id = make_order_id()
        pending.update({
            "order_id": order_id,
            "scheduled_for": run_at.isoformat(),
            "status": "scheduled",
            "created_at": RTDB_TIMESTAMP
        })

        get_order_ref(order_id).set(pending)

        rest_id = pending["restaurant_id"]
        add_order_to_restaurant(rest_id, order_id)
        increment_rest_orders_count(rest_id, 1)

        rest_data = get_restaurant_ref(rest_id).get() or {}
        rest_chat_id = rest_data.get("chat_id") or rest_data.get("manager_chat_id")

        schedule_order_notification(order_id, run_at, rest_chat_id)

        bot.send_message(user.id, f"Order placed! ID: {order_id}\nScheduled for {run_at}")
        set_user_state(user.id, {})
        return

    # ---------------- Menu options ----------------
    if text == "Search by restaurant":
        bot.send_message(user.id, "Send restaurant name:")
        set_user_state(user.id, {"awaiting_search": True, "awaiting_search_type": "restaurant"})
        return

    if text == "Search by food":
        bot.send_message(user.id, "Send food name:")
        set_user_state(user.id, {"awaiting_search": True, "awaiting_search_type": "food"})
        return

    if text == "Search by location":
        # ask user to share their location using keyboard
        markup = types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        markup.add(types.KeyboardButton("Share location 📍", request_location=True))
        bot.send_message(user.id, "Please share your location to find restaurants nearby.", reply_markup=markup)
        set_user_state(user.id, {"awaiting_search_by_location": True})
        return

    if text == "Top-rated":
        try:
            snap = restaurants_ref().order_by_child("rating").limit_to_last(10).get() or {}
            items = list(snap.items())
            items.sort(key=lambda x: x[1].get("rating", 0), reverse=True)
        except:
            all_rest = restaurants_ref().get() or {}
            items = sorted(all_rest.items(), key=lambda x: x[1].get("rating", 0), reverse=True)

        msg = "Top-rated restaurants:\n"
        kb = types.InlineKeyboardMarkup()
        for rid, r in items:
            msg += f"- {r['name']} ({r.get('rating', 'N/A')})\n"
            kb.add(types.InlineKeyboardButton(r["name"], callback_data=json.dumps({"action": "select_rest", "rid": rid})))

        bot.send_message(user.id, msg, reply_markup=kb)
        return

    if text == "Least-ordered (fastest)":
        try:
            snap = restaurants_ref().order_by_child("orders_count").limit_to_first(10).get() or {}
            items = list(snap.items())
            items.sort(key=lambda x: x[1].get("orders_count", 0))
        except:
            all_rest = restaurants_ref().get() or {}
            items = sorted(all_rest.items(), key=lambda x: x[1].get("orders_count", 0))

        msg = "Fastest restaurants:\n"
        kb = types.InlineKeyboardMarkup()
        for rid, r in items:
            msg += f"- {r['name']} ({r.get('orders_count', 0)} orders)\n"
            kb.add(types.InlineKeyboardButton(r["name"], callback_data=json.dumps({"action": "select_rest", "rid": rid})))

        bot.send_message(user.id, msg, reply_markup=kb)
        return

    if text == "Closest restaurants":
        user_data = get_user_ref(user.id).get() or {}
        loc = user_data.get("last_location")
        if not loc:
            bot.send_message(user.id, "Share location first.")
            return

        docs = restaurants_ref().get() or {}
        distances = []

        for rid, r in docs.items():
            pos = r.get("location")
            if not pos:
                continue
            dist = haversine(loc["lon"], loc["lat"], pos["lon"], pos["lat"])
            distances.append((dist, rid, r))

        distances.sort(key=lambda x: x[0])

        msg = "Closest restaurants:\n"
        kb = types.InlineKeyboardMarkup()

        for dist, rid, r in distances[:10]:
            msg += f"- {r['name']} — {dist:.2f} km\n"
            kb.add(types.InlineKeyboardButton(r["name"], callback_data=json.dumps({"action": "select_rest", "rid": rid})))

        bot.send_message(user.id, msg, reply_markup=kb)
        return

    if text == "My orders":
        all_orders = orders_ref().get() or {}
        msg = "Your orders:\n"
        found = False

        for oid, o in all_orders.items():
            if o.get("user_id") == str(user.id):
                found = True
                msg += f"- {oid}: {o['status']} at {o.get('scheduled_for')}\n"

        if not found:
            bot.send_message(user.id, "You have no orders.")
        else:
            bot.send_message(user.id, msg)
        return
  
    bot.send_message(user.id, "Unknown command. Use /menu.")

# =============== SEARCH HELPERS ===============

def handle_search_restaurant_query(user_id, query):
    query = query.lower()
    docs = restaurants_ref().get() or {}

    results = [
        (rid, r) for rid, r in docs.items()
        if query in r.get("name", "").lower()
    ]

    if not results:
        bot.send_message(user_id, "No restaurants found.")
        return

    kb = types.InlineKeyboardMarkup()
    for rid, r in results[:10]:
        kb.add(types.InlineKeyboardButton(
            f"{r['name']} — {r.get('address', '')}",
            callback_data=json.dumps({"action": "select_rest", "rid": rid})
        ))

    bot.send_message(user_id, "Results:", reply_markup=kb)

def handle_search_food_query(user_id, query):
    query = query.lower()
    docs = restaurants_ref().get() or {}

    results = []
    for rid, r in docs.items():
        for f in r.get("foods", []):
            if query in f.get("name", "").lower():
                results.append((rid, r, f))
                break

    if not results:
        bot.send_message(user_id, "No matches.")
        return

    kb = types.InlineKeyboardMarkup()
    for rid, r, f in results[:10]:
        kb.add(types.InlineKeyboardButton(
            f"{r['name']} — has {f['name']}",
            callback_data=json.dumps({
                "action": "select_rest_food",
                "rid": rid,
                "food_name": f["name"]
            })
        ))

    bot.send_message(user_id, "Results:", reply_markup=kb)

# =============== CALLBACK HANDLER ===============
def save_restaurant_and_finish(user_id, data):
    rest_id = make_rest_id()
    data["id"] = rest_id
    data["foods"] = data.get("foods", [])
    data["created_at"] = RTDB_TIMESTAMP

    restaurants_ref().child(rest_id).set(data)
    clear_user_state(user_id)

    bot.send_message(
        user_id,
        f"✅ Restaurant *{data['name']}* added successfully!",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id

    # ======================================================
    # 1️⃣ PLAIN STRING CALLBACKS (MUST COME FIRST)
    # ======================================================
    if call.data == "add_rest_send_image":
        state = get_user_state(user_id)
        set_user_state(user_id, {
            **state,
            "awaiting_rest_image": True
        })
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "📷 Please send the restaurant photo.")
        return

    if call.data == "add_rest_skip_image":
        state = get_user_state(user_id)
        save_restaurant_and_finish(user_id, state["data"])
        bot.answer_callback_query(call.id)
        return

    # ======================================================
    # 2️⃣ JSON CALLBACKS (SAFE PARSE)
    # ======================================================
    try:
        data = json.loads(call.data)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid callback data")
        return

    action = data.get("action")
    # ======================================================
    # ✏️ EDIT RESTAURANT
    # ======================================================
    if action == "edit_page":
        page = data["page"]
        search = data.get("search")
        kb, total = build_restaurant_page(page, search)
        bot.edit_message_text(
            f"✏️ Select restaurant to edit (Page {page+1}):",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)  # ✅ ADD
        return
    
    if action == "food_page":
        kb = build_food_page(data["rid"], data["page"])
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)
        return
    
    if action == "add_food_new":
        set_user_state(user_id, {
            "add_food_mode": True,
            "rid": data["rid"],
            "step": "name"
        })
        bot.send_message(user_id, "🍔 Send food name:")
        bot.answer_callback_query(call.id)
        return

        
    
    if action == "add_food_existing":
        page = data.get("page", 0)

        kb = build_food_page( page)

        bot.send_message(
            user_id,
            "📦 Select food to add:",
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)
        return


    if action == "edit_search":
        set_user_state(user_id, {"awaiting_edit_search": True})
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "🔍 Send restaurant name:")
        return

    if action == "edit_rest":
        rid = data["rid"]
        rest = get_restaurant_ref(rid).get()

        if not rest:
            bot.answer_callback_query(call.id, "Restaurant not found")
            return

        set_user_state(user_id, {
            "editing_rest": True,
            "rid": rid
        })

        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("✏️ Name", "📍 Location")
        kb.add("🖼 Image", "➕ Add Food")
        kb.add("❌ Cancel")


        bot.send_message(
            user_id,
            f"Editing *{rest['name']}*.\nChoose what to edit:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
        return
    if action == "cancel_add_rest_food":
        clear_user_state(user_id)
        bot.answer_callback_query(call.id, "Cancelled")
        bot.send_message(user_id, "❌ Food adding cancelled.")
        return

    if action == "confirm_delete_rest":
        rid = data["rid"]
        get_restaurant_ref(rid).delete()
        bot.answer_callback_query(call.id, "Restaurant deleted")
        bot.send_message(user_id, "✅ Restaurant removed")
        return   # 🔥 REQUIRED
    if action == "add_existing_food":
        rid = data["rid"]
        index = data["index"]

        rest = get_restaurant_ref(rid).get() or {}
        foods = rest.get("foods") or []

        if index >= len(foods):
            bot.answer_callback_query(call.id, "Food not found")
            return

        food = foods[index]

        set_user_state(user_id, {
            "awaiting_food_price": True,
            "rid": rid,
            "food_data": food
        })

        bot.send_message(
            user_id,
            f"💰 Send price for *{food['name']}*:",
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
        return


# 👤 SELECT MANAGER FOR RESTAURANT (ADMIN FLOW)
# ======================================================
    if action == "add_rest_select_manager":
        uid = data["uid"]

        state = get_user_state(user_id)
        pending = state.get("pending_rest") or state.get("data") or {}

        pending["manager_chat_id"] = int(uid)

        set_user_state(user_id, {
            "pending_rest": pending
        })

        bot.answer_callback_query(call.id, "Manager selected ✅")
        bot.send_message(
            user_id,
            f"✅ Manager assigned (Telegram ID: {uid})"
        )

        # try auto-finish if everything exists
        attempt_finish_after_state_change(user_id)
        return

    # ======================================================
    # 🍽 RESTAURANT SELECTED (SHOW IMAGE + DETAILS)
    # ======================================================
    if action == "select_rest":
        rid = data["rid"]
        rest = get_restaurant_ref(rid).get()

        if not rest:
            bot.answer_callback_query(call.id, "Restaurant not found")
            return

        caption = (
            f"🍽 *{rest.get('name', 'Unknown')}*\n"
            f"⭐ Rating: {rest.get('rating', 'N/A')}\n\n"
            f"{rest.get('description', '')}"
        )

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                "🛒 Order now",
                callback_data=json.dumps({
                    "action": "view_menu",
                    "rid": rid
                })
            ),
            types.InlineKeyboardButton(
                "📋 Food list",
                callback_data=json.dumps({
                    "action": "view_menu",
                    "rid": rid
                })
            )
        )

        if rest.get("image_file_id"):
            bot.send_photo(
                user_id,
                rest["image_file_id"],
                caption=caption,
                reply_markup=kb,
                parse_mode="Markdown"
            )
        else:
            bot.send_message(
                user_id,
                caption,
                reply_markup=kb,
                parse_mode="Markdown"
            )

        bot.answer_callback_query(call.id)
        return

    # ======================================================
    # 📋 VIEW MENU
    # ======================================================
    if action == "view_menu":
        rid = data["rid"]
        rest = get_restaurant_ref(rid).get()

        if not rest or not rest.get("foods"):
            bot.answer_callback_query(call.id, "No foods available")
            return

        kb = types.InlineKeyboardMarkup()
        for f in rest["foods"]:
            kb.add(types.InlineKeyboardButton(
                f"{f['name']} — {f.get('price', 'N/A')}",
                callback_data=json.dumps({
                    "action": "choose_food",
                    "rid": rid,
                    "food_name": f["name"]
                })
            ))

        bot.send_message(
            user_id,
            f"📋 *{rest['name']} Menu*",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
        return

    # ======================================================
    # 🍔 SELECT REST FOOD (FROM SEARCH)
    # ======================================================
    if action == "select_rest_food":
        rid = data["rid"]
        fname = data["food_name"]
        rest = get_restaurant_ref(rid).get()

        if not rest:
            bot.answer_callback_query(call.id, "Restaurant not found.")
            return

        kb = types.InlineKeyboardMarkup()
        for f in rest.get("foods", []):
            if f["name"] == fname:
                kb.add(types.InlineKeyboardButton(
                    f"{f['name']} — {f.get('price', 'N/A')}",
                    callback_data=json.dumps({
                        "action": "choose_food",
                        "rid": rid,
                        "food_name": f["name"]
                    })
                ))

        bot.send_message(
            user_id,
            f"{rest['name']} menu:",
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)
        return

    # ======================================================
    # 🍽 FOOD → QUANTITY
    # ======================================================
    if action == "choose_food":
        rid = data["rid"]
        fname = data["food_name"]

        kb = types.InlineKeyboardMarkup()
        for qty in range(1, 6):
            kb.add(types.InlineKeyboardButton(
                str(qty),
                callback_data=json.dumps({
                    "action": "pick_qty",
                    "rid": rid,
                    "food_name": fname,
                    "qty": qty
                })
            ))

        bot.send_message(
            user_id,
            f"How many *{fname}*?",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
        return

    # ======================================================
    # 🧾 QUANTITY SELECTED → CREATE ORDER
    # ======================================================
    if action == "pick_qty":
        rid = data["rid"]
        fname = data["food_name"]
        qty = int(data["qty"])

        rest = get_restaurant_ref(rid).get()
        food = next((f for f in rest.get("foods", []) if f["name"] == fname), None)

        if not food:
            bot.answer_callback_query(call.id, "Food not found")
            return

        price = float(food.get("price", 0)) * qty

        pending = {
            "restaurant_id": rid,
            "restaurant_name": rest.get("name"),
            "items": [{"name": fname, "qty": qty, "price": price}],
            "total_price": price,
            "user_id": str(user_id),
            "user_name": call.from_user.full_name,
            "phone": get_user_ref(user_id).child("phone").get()
        }

        set_user_state(user_id, {
            "awaiting_schedule": True,
            "pending_order": pending
        })

        bot.send_message(
            user_id,
            "⏰ When should we schedule?\nSend *ASAP* or `YYYY-MM-DD HH:MM`",
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
        return

    # ======================================================
    # ❓ FALLBACK
    # ======================================================
    bot.answer_callback_query(call.id, "Unknown action")

# =============== PHOTO handler (for admin restaurant photo) ===============

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    state = get_user_state(message.from_user.id)
    if not state:
        return
    if state.get("awaiting_rest_image") and state.get("add_rest"):
        data = state["data"]
        file_id = message.photo[-1].file_id

        storage_path = upload_telegram_photo_to_firebase(file_id)
        if storage_path:
            data["image_storage_path"] = storage_path
        else:
            data["image_file_id"] = file_id

        save_restaurant_and_finish(message.from_user.id, data)
        return

    # admin sending restaurant photo
    if state.get("awaiting_rest_photo"):
        pending = state.get("pending_rest", {})
        # take highest resolution
        file_id = message.photo[-1].file_id
        # attempt upload to Firebase Storage (if configured)
        storage_path = upload_telegram_photo_to_firebase(file_id)
        # save either storage path or file_id
        if storage_path:
            pending["image_storage_path"] = storage_path
        else:
            pending["image_file_id"] = file_id

        set_user_state(message.from_user.id, {"pending_rest": pending, "awaiting_rest_photo": False})
        bot.send_message(message.from_user.id, "Photo saved.")
        bot.send_message(message.from_user.id, "Send the manager's TELEGRAM ID (numeric).")
        set_user_state(message.from_user.id, {"pending_rest": pending, "awaiting_rest_manager": True})
        return

# =============== CALLBACK finish: when admin provided all data, save restaurant ===============

# We'll save the restaurant when all required fields are present: name, foods (may be empty), manager_chat_id, location
def try_finalize_pending_rest(admin_id):
    state = get_user_state(admin_id)
    pending = state.get("pending_rest", {})
    # check required fields
    if not pending.get("name"):
        return False, "Name missing"
    if "manager_chat_id" not in pending:
        return False, "Manager missing"
    if "location" not in pending:
        return False, "Location missing"

    # everything ok — create restaurant record
    rest_id = make_rest_id()
    record = {
        "id": rest_id,
        "name": pending.get("name"),
        "foods": pending.get("foods", []),
        "manager_chat_id": pending.get("manager_chat_id"),
        "location": pending.get("location"),
        "created_at": RTDB_TIMESTAMP,
        # include image fields if present
    }
    if pending.get("image_storage_path"):
        record["image_storage_path"] = pending.get("image_storage_path")
    if pending.get("image_file_id"):
        record["image_file_id"] = pending.get("image_file_id")

    try:
        restaurants_ref().child(rest_id).set(record)
        clear_user_state(admin_id)
        bot.send_message(admin_id, f"Restaurant '{record['name']}' registered with id {rest_id}.")
        return True, rest_id
    except Exception as e:
        print("Error saving restaurant:", e)
        return False, str(e)

def attempt_finish_after_state_change(user_id):
    state = get_user_state(user_id)
    pending = state.get("pending_rest", {})
    if pending and pending.get("name") and pending.get("manager_chat_id") and pending.get("location"):
        ok, info = try_finalize_pending_rest(user_id)
        if not ok:
            bot.send_message(user_id, f"Error saving restaurant: {info}")


def restore_scheduled_orders():
    orders = orders_ref().get() or {}

    for oid, o in orders.items():
        if o.get("status") != "scheduled":
            continue

        try:
            run_at = dateparser.parse(o["scheduled_for"])
            if not run_at:
                continue
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)

            rest = get_restaurant_ref(o["restaurant_id"]).get() or {}
            rest_chat_id = rest.get("chat_id") or rest.get("manager_chat_id")

            schedule_order_notification(oid, run_at, rest_chat_id)
            print(f"Restored schedule: {oid}")

        except Exception as e:
            print("restore error:", e)

restore_scheduled_orders()

# =============== WEBHOOK ===============
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    print("🔥 UPDATE RECEIVED")

    update = telebot.types.Update.de_json(
        request.get_data(as_text=True)
    )
    bot.process_new_updates([update])

    return "OK", 200

@app.route("/set_webhook")
def set_webhook():
 
    bot.remove_webhook()
    ok = bot.set_webhook(f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}")

    return f"Webhook set: {ok}"

# =============== RUN ===============

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
