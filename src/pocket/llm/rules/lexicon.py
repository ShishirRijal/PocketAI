"""Word lists for the offline parser. English + romanized Nepali, tuned for
Tallinn / Porto / Tampere / Kathmandu spending."""

from __future__ import annotations

# fmt: off

# token -> ISO code. Longer tokens first matters for the regex, handled at build time.
CURRENCY_WORDS: dict[str, str] = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "eu": "EUR",
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "bucks": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "quid": "GBP",
    "₨": "NPR",
    "npr": "NPR",
    "rs": "NPR",
    "rs.": "NPR",
    "rupee": "NPR",
    "rupees": "NPR",
    "rupiya": "NPR",
    "rupaiya": "NPR",
    "rupaya": "NPR",
    "rupaiyaa": "NPR",
    "₹": "INR",
    "inr": "INR",
    "sek": "SEK",
    "nok": "NOK",
    "dkk": "DKK",
    "chf": "CHF",
    "pln": "PLN",
    "zł": "PLN",
    "czk": "CZK",
    "jpy": "JPY",
    "yen": "JPY",
    "¥": "JPY",
}

# category name -> keywords. Names line up with DEFAULT_CATEGORIES.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Groceries": [
        "grocery", "groceries", "supermarket", "rimi", "selver", "maxima", "prisma", "lidl",
        "k-market", "kmarket", "s-market", "coop", "aldi", "pingo doce", "continente",
        "bhatbhateni", "vegetables", "veggies", "tarkari", "fruits", "milk", "bread", "eggs",
        "sabji", "kirana", "mini market", "minimarket", "alepa", "coop prisma",
    ],
    "Cafes": [
        "coffee", "cafe", "café", "latte", "cappuccino", "espresso", "chiya", "chai", "tea",
        "starbucks", "costa", "boba", "bubble tea", "croissant", "pastry", "bakery",
    ],
    "Restaurants": [
        "lunch", "dinner", "breakfast", "restaurant", "pizza", "burger", "momo", "momos",
        "sushi", "kebab", "takeaway", "takeout", "wolt", "bolt food", "foodora", "food",
        "sandwich", "brunch", "khana", "khaja", "dal bhat", "thakali", "mcdonalds", "kfc",
        "ramen", "noodles", "chowmein", "bar", "beer", "drinks", "pub",
    ],
    "Transport": [
        "metro", "bus", "tram", "taxi", "uber", "bolt", "train", "fuel", "petrol", "gas",
        "parking", "ticket", "transit", "ferry", "scooter", "tuul", "lime", "vr", "cp",
        "pathao", "indrive", "micro", "tempo", "transport",
    ],
    "Rent": ["rent", "bhada", "landlord", "deposit"],
    "Utilities": [
        "electricity", "water bill", "internet", "wifi", "phone bill", "mobile", "utilities",
        "heating", "gas bill", "elisa", "telia", "ncell", "ntc", "recharge", "top up", "topup",
    ],
    "Subscriptions": [
        "spotify", "netflix", "icloud", "youtube premium", "chatgpt", "claude", "github",
        "subscription", "disney", "hbo", "prime", "notion", "gym membership",
    ],
    "Shopping": [
        "clothes", "shoes", "amazon", "zara", "h&m", "ikea", "shopping", "jacket", "shirt",
        "tshirt", "t-shirt", "electronics", "headphones", "daraz", "kapada", "lugaa",
    ],
    "Health": [
        "pharmacy", "apteek", "apteekki", "medicine", "doctor", "dentist", "hospital",
        "vitamins", "aushadhi", "ausadhi", "clinic", "gym", "haircut", "barber",
    ],
    "Entertainment": [
        "movie", "cinema", "concert", "netflix", "game", "games", "steam", "museum",
        "theatre", "theater", "party", "event", "festival", "bowling",
    ],
    "Travel": [
        "flight", "hotel", "airbnb", "hostel", "booking", "ryanair", "finnair", "airbaltic",
        "tap", "trip", "visa", "luggage",
    ],
    "Education": ["book", "books", "course", "udemy", "tuition", "exam", "fees", "stationery"],
    "Gifts": ["gift", "present", "birthday", "donation", "charity", "upahar"],
    "Salary": ["salary", "paycheck", "payslip", "wage", "wages", "talab", "tala"],
}

INCOME_WORDS = [
    "salary", "got paid", "paid me", "received", "receive", "income", "refund", "refunded",
    "reimbursed", "reimbursement", "earned", "talab", "cashback", "sold", "freelance payment",
    "paycheck", "bonus", "interest", "dividend",
]
TRANSFER_WORDS = ["transfer", "transferred", "moved to savings", "to savings", "sent to savings"]

# words that don't tell us anything about what was bought
STOPWORDS = {
    "a", "an", "the", "at", "for", "on", "in", "to", "from", "of", "and", "then", "with",
    "w/", "i", "my", "me", "spent", "spend", "paid", "pay", "bought", "buy", "got", "some",
    "today", "yesterday", "tonight", "this", "morning", "evening", "afternoon", "night",
    "aja", "aaja", "hijo", "hijoko", "ajako", "ma", "ko", "ra", "ani", "le", "lai", "was",
    "it", "just", "also", "about", "around", "approx", "like", "total", "cost", "costs",
    "for the", "by", "via", "card", "cash", "kharcha", "gare", "garyo", "bhayo", "tireko",
    "tire", "kinnu", "kineko", "kine", "lagyo", "last", "ago", "days", "day", "week",
    "sanga", "sang", "saath",
}

KNOWN_MERCHANTS = {
    "rimi": "Rimi", "selver": "Selver", "maxima": "Maxima", "prisma": "Prisma", "lidl": "Lidl",
    "k-market": "K-Market", "kmarket": "K-Market", "s-market": "S-Market", "alepa": "Alepa",
    "coop": "Coop", "aldi": "Aldi", "pingo doce": "Pingo Doce", "continente": "Continente",
    "bhatbhateni": "Bhatbhateni", "starbucks": "Starbucks", "costa": "Costa", "wolt": "Wolt",
    "bolt food": "Bolt Food", "bolt": "Bolt", "uber": "Uber", "foodora": "Foodora",
    "spotify": "Spotify", "netflix": "Netflix", "amazon": "Amazon", "ikea": "IKEA",
    "zara": "Zara", "h&m": "H&M", "ryanair": "Ryanair", "finnair": "Finnair",
    "airbaltic": "airBaltic", "airbnb": "Airbnb", "mcdonalds": "McDonald's", "kfc": "KFC",
    "elisa": "Elisa", "telia": "Telia", "ncell": "Ncell", "daraz": "Daraz", "pathao": "Pathao",
    "tuul": "Tuul", "lime": "Lime", "icloud": "iCloud", "github": "GitHub", "steam": "Steam",
}

WEEKDAYS = {
    "monday": 0, "mon": 0, "sombar": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "mangalbar": 1,
    "wednesday": 2, "wed": 2, "budhabar": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "bihibar": 3,
    "friday": 4, "fri": 4, "sukrabar": 4,
    "saturday": 5, "sat": 5, "sanibar": 5,
    "sunday": 6, "sun": 6, "aitabar": 6,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
