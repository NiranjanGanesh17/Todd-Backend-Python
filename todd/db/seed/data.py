"""Seed data: the category list and the merchant dictionary.

Slugs are stable and are what the API returns; names are what users read. The
slugs use underscores, matching the Node service's seed and therefore the
existing database. (`Todd-Frontend/lib/categories.ts` carries a local copy with
hyphens — it is only used for its display names, never matched against an API
response, but the two should be reconciled.)

The dictionary covers big brands only. Measured on 208 real transactions it
matched 12%: real spend is small local merchants and individuals, which is
what the VPA rungs and, eventually, the model are for (§9).
"""

from __future__ import annotations

from dataclasses import dataclass

from todd.schemas.enums import CategoryKind


@dataclass(frozen=True, slots=True)
class SeedCategory:
    slug: str
    name: str
    kind: CategoryKind
    sort_order: int


@dataclass(frozen=True, slots=True)
class SeedMerchant:
    normalized_name: str
    aliases: tuple[str, ...]
    category_slug: str


#: The slug the API reports for a transaction no rung could label. It is not a
#: row in `categories`: a synthetic bucket, so "we do not know" never looks
#: like a decision somebody made.
UNCATEGORIZED_SLUG = "uncategorized"
UNCATEGORIZED_NAME = "Uncategorized"

SEED_CATEGORIES: tuple[SeedCategory, ...] = (
    SeedCategory("rent_housing", "Rent & housing", CategoryKind.SPEND, 10),
    SeedCategory("groceries", "Groceries", CategoryKind.SPEND, 20),
    SeedCategory("food_delivery", "Food delivery", CategoryKind.SPEND, 30),
    SeedCategory("dining_out", "Dining out", CategoryKind.SPEND, 40),
    SeedCategory("shopping", "Shopping", CategoryKind.SPEND, 50),
    SeedCategory("travel_fuel", "Travel & fuel", CategoryKind.SPEND, 60),
    SeedCategory("utilities_bills", "Utilities & bills", CategoryKind.SPEND, 70),
    SeedCategory("subscriptions", "Subscriptions", CategoryKind.SPEND, 80),
    SeedCategory("emis_loans", "EMIs & loans", CategoryKind.SPEND, 90),
    SeedCategory("investments_sip", "Investments (SIP)", CategoryKind.SPEND, 100),
    SeedCategory("insurance", "Insurance", CategoryKind.SPEND, 110),
    SeedCategory("health", "Health", CategoryKind.SPEND, 120),
    SeedCategory("education", "Education", CategoryKind.SPEND, 130),
    SeedCategory("bank_charges", "Bank charges", CategoryKind.SPEND, 140),
    SeedCategory("cash_withdrawal", "Cash withdrawal", CategoryKind.SPEND, 150),
    SeedCategory("upi_to_people", "UPI to people", CategoryKind.SPEND, 160),
    SeedCategory("transfers_own", "Transfers (own accounts)", CategoryKind.TRANSFER, 170),
    SeedCategory("income_salary", "Income: salary", CategoryKind.INCOME, 180),
    SeedCategory("income_other", "Income: other", CategoryKind.INCOME, 190),
)

SEED_MERCHANTS: tuple[SeedMerchant, ...] = (
    # food delivery
    SeedMerchant(
        "Swiggy",
        ("swiggy", "bundl technologies", "swiggyupi", "swiggy instamart"),
        "food_delivery",
    ),
    SeedMerchant("Zomato", ("zomato", "zomato ltd", "zomatoonline"), "food_delivery"),
    # groceries
    SeedMerchant("Blinkit", ("blinkit", "grofers", "blink commerce"), "groceries"),
    SeedMerchant("Zepto", ("zepto", "kiranakart"), "groceries"),
    SeedMerchant("BigBasket", ("bigbasket", "innovative retail", "bbdaily"), "groceries"),
    SeedMerchant("DMart", ("dmart", "avenue supermarts"), "groceries"),
    # shopping
    SeedMerchant("Amazon", ("amazon", "amazon pay", "amzn", "amazon seller"), "shopping"),
    SeedMerchant("Flipkart", ("flipkart", "fkrt", "flipkart internet"), "shopping"),
    SeedMerchant("Myntra", ("myntra", "myntra designs"), "shopping"),
    SeedMerchant("Nykaa", ("nykaa", "fsn ecommerce"), "shopping"),
    # travel and fuel
    SeedMerchant("Uber", ("uber", "uber india", "uberrides"), "travel_fuel"),
    SeedMerchant("Ola", ("ola", "ani technologies", "olacabs"), "travel_fuel"),
    SeedMerchant("Rapido", ("rapido", "roppen transportation"), "travel_fuel"),
    SeedMerchant("IRCTC", ("irctc", "indian railway"), "travel_fuel"),
    SeedMerchant("IndianOil", ("indianoil", "iocl", "indian oil"), "travel_fuel"),
    SeedMerchant("Indigo", ("indigo", "interglobe aviation", "6e"), "travel_fuel"),
    SeedMerchant("MakeMyTrip", ("makemytrip", "mmt", "goibibo"), "travel_fuel"),
    # subscriptions
    SeedMerchant("Netflix", ("netflix", "netflix com"), "subscriptions"),
    SeedMerchant("Spotify", ("spotify", "spotify india"), "subscriptions"),
    SeedMerchant(
        "YouTube Premium",
        ("google youtube", "youtubepremium", "youtube premium"),
        "subscriptions",
    ),
    SeedMerchant("Apple", ("apple com bill", "itunes", "apple services"), "subscriptions"),
    SeedMerchant("Google", ("google", "google play", "google india"), "subscriptions"),
    SeedMerchant("Amazon Prime", ("amazon prime", "primevideo", "prime video"), "subscriptions"),
    SeedMerchant("JioHotstar", ("hotstar", "disney hotstar", "jiohotstar"), "subscriptions"),
    # utilities
    SeedMerchant("Jio", ("jio", "reliance jio", "jio prepaid"), "utilities_bills"),
    SeedMerchant("Airtel", ("airtel", "bharti airtel", "airtel payments"), "utilities_bills"),
    SeedMerchant("Vodafone Idea", ("vodafone", "vi india", "idea cellular"), "utilities_bills"),
    SeedMerchant("Tata Power", ("tata power", "tatapower"), "utilities_bills"),
    SeedMerchant("BESCOM", ("bescom", "bangalore electricity"), "utilities_bills"),
    # health
    SeedMerchant("Apollo Pharmacy", ("apollo pharmacy", "apollo hospitals"), "health"),
    SeedMerchant("PharmEasy", ("pharmeasy", "api holdings"), "health"),
    SeedMerchant("Practo", ("practo",), "health"),
    SeedMerchant("Cult.fit", ("cultfit", "cure fit", "cult fit"), "health"),
    # insurance
    SeedMerchant("LIC", ("lic of india", "life insurance corp"), "insurance"),
    SeedMerchant("HDFC Life", ("hdfc life", "hdfc standard life"), "insurance"),
    # investments
    SeedMerchant("Zerodha", ("zerodha", "zerodha broking"), "investments_sip"),
    SeedMerchant("Groww", ("groww", "nextbillion technology"), "investments_sip"),
    SeedMerchant("Upstox", ("upstox", "rksv securities"), "investments_sip"),
    SeedMerchant("Kuvera", ("kuvera", "arevuk advisory"), "investments_sip"),
    SeedMerchant("Smallcase", ("smallcase",), "investments_sip"),
    SeedMerchant("INDmoney", ("indmoney", "finzoom"), "investments_sip"),
    SeedMerchant("Paytm Money", ("paytm money",), "investments_sip"),
    SeedMerchant("CAMS", ("cams", "computer age management"), "investments_sip"),
    SeedMerchant("KFintech", ("kfintech", "kfin technologies"), "investments_sip"),
    SeedMerchant(
        "Indian Clearing Corp (SIP)",
        (
            "indian clearing",
            "indian cle",  # as ICICI truncates it
            "icclsip",
            "bse sip",
            "mutualfunds",
            "nse clearing",
        ),
        "investments_sip",
    ),
    # education
    SeedMerchant("Unacademy", ("unacademy", "sorting hat"), "education"),
    SeedMerchant("Coursera", ("coursera",), "education"),
    # --- added from real statements: names that appeared in the unlabelled
    # --- rows and were recognisable without any model.
    SeedMerchant(
        "Bajaj Finance",
        ("bajaj finance", "bajaj fina", "bflautopay", "bajaj finserv"),
        "emis_loans",
    ),
    SeedMerchant(
        "BookMyShow",
        ("bookmyshow", "bigtree entertainment", "big tree entertainment"),
        "shopping",
    ),
    SeedMerchant("Bescom", ("bangalore electricity supply",), "utilities_bills"),
    SeedMerchant("HDFC Ergo", ("hdfc ergo", "ergo general"), "insurance"),
    SeedMerchant("ICICI Prudential", ("icici pru", "icici prudential"), "insurance"),
    SeedMerchant("Razorpay", ("razorpay", "rzpy"), "shopping"),
    SeedMerchant("Swiggy Instamart", ("instamart",), "groceries"),
    SeedMerchant("Licious", ("licious", "delightful gourmet"), "groceries"),
    SeedMerchant("Country Delight", ("country delight", "nutrilife"), "groceries"),
    SeedMerchant("Decathlon", ("decathlon",), "shopping"),
    SeedMerchant("Lenskart", ("lenskart",), "shopping"),
    SeedMerchant("Urban Company", ("urban company", "urbanclap"), "shopping"),
)
