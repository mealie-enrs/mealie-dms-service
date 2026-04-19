"""
Recipe Draft Service — generates a structured recipe draft from a food image.

Pipeline:
  1. validate_image()   blur check (Laplacian variance) + food check via Qdrant similarity
  2. compute_embedding() ResNet50 2048-D embedding (reuses inference.py)
  3. search_qdrant()    top-K nearest neighbours (reuses inference.py)
  4. generate_draft()   template-based recipe JSON assembled from top-K matches
  5. Returns DraftResult with full structured output and a UUID draft_id
     (draft_id is used downstream for feedback capture)
"""
from __future__ import annotations

import ast
import io
import logging
import re
import uuid
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

BLUR_THRESHOLD = 80.0       # Laplacian variance below this → blurry
FOOD_SCORE_THRESHOLD = 0.20 # Qdrant top-1 cosine score below this → likely not food
MIN_IMAGE_PIXELS = 64 * 64  # reject thumbnails / icons

DISCLAIMER = (
    "AI-generated draft based on visual similarity to known recipes. "
    "Always review ingredients for allergens, dietary restrictions, and accuracy "
    "before cooking or sharing."
)

# ---------------------------------------------------------------------------
# ImageNet food class indices (ResNet50 trained on ILSVRC-2012)
# Used as a fast secondary check when Qdrant confidence is ambiguous.
# ---------------------------------------------------------------------------
FOOD_IMAGENET_INDICES: frozenset[int] = frozenset([
    924, 925, 926, 927, 928, 929, 930, 931, 932, 933, 934, 935, 936, 937,
    938, 939, 940, 941, 942, 943, 944, 945, 946, 947, 948, 949, 950, 951,
    952, 953, 954, 955, 956, 957, 958, 959, 960, 961, 962, 963, 964, 965,
    966, 967, 968, 969, 970, 971, 972, 973, 974, 975, 976, 977, 978, 979,
    980, 981, 982, 983, 984, 985, 986, 987, 988, 989, 990, 991, 992, 993,
    # Also include food-adjacent objects commonly co-occurring with food images
    559, 560, 561, 567, 898, 899,  # plates, bowls, cups
])


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ImageValidation:
    is_valid: bool
    blur_score: float
    food_confidence: float       # top-1 Qdrant cosine similarity (0–1)
    rejection_reason: str | None = None
    width: int = 0
    height: int = 0


@dataclass
class DraftResult:
    draft_id: str
    validation: ImageValidation
    title: str
    ingredients: list[str]
    steps: list[str]
    tags: list[str]
    confidence: float            # 0.0–1.0 overall draft quality signal
    disclaimer: str
    top_k_matches: list[dict]   # raw Qdrant results for transparency


# ---------------------------------------------------------------------------
# Image validation helpers
# ---------------------------------------------------------------------------

def _laplacian_variance(gray: np.ndarray) -> float:
    """Compute Laplacian variance as a sharpness proxy. Higher = sharper."""
    kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float64)
    # Manual 2-D convolution via stride tricks to avoid cv2 dependency
    h, w = gray.shape
    padded = np.pad(gray, 1, mode="reflect")
    out = np.zeros_like(gray, dtype=np.float64)
    for dy in range(3):
        for dx in range(3):
            out += kernel[dy, dx] * padded[dy:dy + h, dx:dx + w]
    return float(np.var(out))


def validate_image(raw_bytes: bytes, qdrant_top_score: float = 0.0) -> ImageValidation:
    """
    Return an ImageValidation.  Call this *before* generating the draft.
    qdrant_top_score is the cosine similarity of the closest Qdrant match
    (0–1); pass 0 if Qdrant hasn't been queried yet.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    except Exception as exc:
        return ImageValidation(
            is_valid=False,
            blur_score=0.0,
            food_confidence=0.0,
            rejection_reason=f"Cannot decode image: {exc}",
        )

    w, h = img.size
    if w * h < MIN_IMAGE_PIXELS:
        return ImageValidation(
            is_valid=False,
            blur_score=0.0,
            food_confidence=qdrant_top_score,
            rejection_reason="Image too small (minimum 64×64 pixels).",
            width=w, height=h,
        )

    gray = np.array(img.convert("L"), dtype=np.float64)
    blur_score = _laplacian_variance(gray)

    if blur_score < BLUR_THRESHOLD:
        return ImageValidation(
            is_valid=False,
            blur_score=blur_score,
            food_confidence=qdrant_top_score,
            rejection_reason=(
                f"Image is too blurry (sharpness={blur_score:.1f}, "
                f"minimum={BLUR_THRESHOLD}). Please upload a clearer photo."
            ),
            width=w, height=h,
        )

    if qdrant_top_score < FOOD_SCORE_THRESHOLD:
        return ImageValidation(
            is_valid=False,
            blur_score=blur_score,
            food_confidence=qdrant_top_score,
            rejection_reason=(
                "Image does not appear to contain food "
                f"(food confidence={qdrant_top_score:.2f}). "
                "Please upload a photo of a dish or ingredients."
            ),
            width=w, height=h,
        )

    return ImageValidation(
        is_valid=True,
        blur_score=blur_score,
        food_confidence=qdrant_top_score,
        width=w, height=h,
    )


# ---------------------------------------------------------------------------
# Ingredient parsing
# ---------------------------------------------------------------------------

def _parse_ingredients(raw: str) -> list[str]:
    """
    Parse the raw ingredients string stored in Qdrant payload.
    Handles stringified Python lists e.g. "['1 cup flour', '2 eggs']"
    and plain comma-separated strings.
    """
    if not raw:
        return []

    stripped = raw.strip()

    # Try as a Python literal (covers most Kaggle CSV rows)
    if stripped.startswith("["):
        try:
            parsed = ast.literal_eval(stripped)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (ValueError, SyntaxError):
            pass

    # Strip surrounding quotes/brackets and split
    cleaned = re.sub(r"[\[\]'\"]", "", stripped)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return parts


def _merge_ingredients(matches: list[dict], top_n: int = 3) -> list[str]:
    """
    Merge + deduplicate ingredients from the top-N matches, weighted by score.
    Higher-scoring matches get their ingredients listed first.
    """
    seen: set[str] = set()
    merged: list[str] = []

    for match in matches[:top_n]:
        raw = match.get("ingredients", "") or match.get("cleaned_ingredients", "")
        for ing in _parse_ingredients(raw):
            key = ing.lower().strip()
            # Remove quantity prefix for dedup key: "2 cups flour" → "flour"
            key_norm = re.sub(r"^[\d\s/¼½¾⅓⅔⅛]+(cup|tbsp|tsp|oz|lb|g|kg|ml|l|clove|slice|piece|pinch|dash)s?\s*", "", key).strip()
            if key_norm and key_norm not in seen:
                seen.add(key_norm)
                merged.append(ing)

    return merged


# ---------------------------------------------------------------------------
# Step generation (template-based)
# ---------------------------------------------------------------------------

_COOKING_KEYWORDS = {
    "bake":   ["bak", "oven", "roast", "casserole", "bread", "cake", "pie", "cookie", "muffin", "pastry"],
    "fry":    ["fry", "sauté", "saute", "stir-fry", "pan", "skillet", "crispy", "fried"],
    "boil":   ["boil", "soup", "stew", "simmer", "broth", "pasta", "noodle", "rice", "porridge"],
    "grill":  ["grill", "bbq", "barbecue", "char", "kebab", "skewer"],
    "raw":    ["salad", "slaw", "ceviche", "tartare", "smoothie", "juice", "raw"],
    "steam":  ["steam", "dim sum", "dumpling"],
}

_STEP_TEMPLATES: dict[str, list[str]] = {
    "bake": [
        "Preheat oven to 375°F (190°C) and lightly grease a baking dish.",
        "In a large bowl, combine dry ingredients and mix well.",
        "Add wet ingredients and stir until a smooth batter forms.",
        "Pour into the prepared dish and spread evenly.",
        "Bake for 25–35 minutes, or until a toothpick inserted in the center comes out clean.",
        "Allow to cool for 10 minutes before serving.",
    ],
    "fry": [
        "Heat 2 tablespoons of oil in a large skillet over medium-high heat.",
        "Season ingredients with salt, pepper, and any desired spices.",
        "Add ingredients to the hot pan and cook for 3–5 minutes per side.",
        "Stir occasionally to ensure even browning.",
        "Remove from heat, drain on paper towels if needed, and serve immediately.",
    ],
    "boil": [
        "Bring a large pot of salted water (or broth) to a boil.",
        "Add ingredients and reduce heat to a gentle simmer.",
        "Cook for 15–25 minutes, stirring occasionally, until tender.",
        "Taste and adjust seasoning with salt and pepper.",
        "Ladle into bowls and garnish before serving.",
    ],
    "grill": [
        "Preheat grill to medium-high heat and lightly oil the grates.",
        "Season ingredients generously on all sides.",
        "Grill for 4–6 minutes per side, or until desired doneness.",
        "Let rest for 5 minutes before slicing or serving.",
        "Serve with your choice of sides.",
    ],
    "raw": [
        "Wash and dry all produce thoroughly.",
        "Chop, slice, or shred ingredients to desired size.",
        "Combine all ingredients in a large bowl.",
        "Drizzle with dressing or seasoning and toss to coat.",
        "Serve immediately or refrigerate for up to 2 hours.",
    ],
    "steam": [
        "Fill a pot with 2 inches of water and bring to a boil.",
        "Place ingredients in a steamer basket above the water.",
        "Cover and steam for 8–15 minutes until cooked through.",
        "Remove carefully and season to taste.",
        "Serve with dipping sauce if desired.",
    ],
    "default": [
        "Prepare and measure all ingredients before starting.",
        "Combine ingredients in a suitable pan or bowl.",
        "Cook over medium heat, stirring occasionally, for 15–20 minutes.",
        "Taste and adjust seasoning with salt, pepper, and herbs.",
        "Serve warm and garnish as desired.",
    ],
}


def _detect_method(title: str, ingredients: list[str]) -> str:
    """Heuristic: guess the cooking method from title and ingredient text."""
    text = (title + " " + " ".join(ingredients)).lower()
    for method, keywords in _COOKING_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return method
    return "default"


def _generate_steps(title: str, ingredients: list[str]) -> list[str]:
    method = _detect_method(title, ingredients)
    return _STEP_TEMPLATES.get(method, _STEP_TEMPLATES["default"])


# ---------------------------------------------------------------------------
# Tag inference
# ---------------------------------------------------------------------------

_TAG_RULES: list[tuple[str, list[str]]] = [
    ("vegetarian",  ["tofu", "tempeh", "vegetable", "cheese", "egg", "lentil", "bean", "chickpea"]),
    ("vegan",       ["tofu", "tempeh", "lentil", "chickpea", "oat milk", "coconut milk", "flaxseed"]),
    ("gluten-free", ["rice flour", "almond flour", "cornmeal", "quinoa", "potato starch"]),
    ("dairy-free",  ["oat milk", "almond milk", "coconut milk", "soy milk", "vegan butter"]),
    ("spicy",       ["chili", "jalapeño", "cayenne", "sriracha", "hot sauce", "pepper flake"]),
    ("dessert",     ["sugar", "chocolate", "vanilla", "cake", "cookie", "brownie", "ice cream", "cream"]),
    ("breakfast",   ["oat", "egg", "bacon", "pancake", "waffle", "toast", "cereal", "granola"]),
    ("soup",        ["broth", "stock", "soup", "stew", "chowder", "bisque"]),
    ("salad",       ["lettuce", "arugula", "spinach", "salad", "dressing", "vinaigrette"]),
    ("quick",       ["minute", "quick", "easy", "simple", "fast", "instant", "no-bake"]),
    ("seafood",     ["salmon", "tuna", "shrimp", "prawn", "cod", "fish", "crab", "lobster", "clam"]),
    ("chicken",     ["chicken", "poultry", "turkey"]),
    ("beef",        ["beef", "steak", "ground beef", "brisket", "ribeye"]),
    ("pasta",       ["pasta", "spaghetti", "linguine", "fettuccine", "penne", "noodle", "macaroni"]),
]


def _infer_tags(title: str, ingredients: list[str]) -> list[str]:
    text = (title + " " + " ".join(ingredients)).lower()
    tags: list[str] = []
    for tag, keywords in _TAG_RULES:
        if any(kw in text for kw in keywords):
            tags.append(tag)
    return tags[:6]  # cap at 6 tags to keep response clean


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _compute_confidence(matches: list[dict], validation: ImageValidation) -> float:
    """
    Aggregate confidence signal:
    - 60% from top-3 Qdrant cosine similarity (weighted: 0.5 / 0.3 / 0.2)
    - 40% from image clarity (blur_score normalised, capped at 1.0)
    """
    if not matches:
        return 0.0

    scores = [m["score"] for m in matches]
    weights = [0.5, 0.3, 0.2]
    qdrant_conf = sum(s * w for s, w in zip(scores, weights[:len(scores)]))

    # Normalise blur score: 80 = threshold (0.0), 800+ = excellent (1.0)
    blur_norm = min(1.0, max(0.0, (validation.blur_score - BLUR_THRESHOLD) / 720.0))
    image_conf = blur_norm

    return round(qdrant_conf * 0.6 + image_conf * 0.4, 3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_draft(
    raw_bytes: bytes,
    matches: list[dict],
    swift_key: str | None = None,
) -> DraftResult:
    """
    Given raw image bytes and Qdrant top-K matches, produce a complete DraftResult.

    Call validate_image() first to gate on blur / food checks.
    The matches list comes from inference.search_qdrant().
    """
    draft_id = str(uuid.uuid4())

    top_score = matches[0]["score"] if matches else 0.0
    validation = validate_image(raw_bytes, qdrant_top_score=top_score)

    if not validation.is_valid:
        return DraftResult(
            draft_id=draft_id,
            validation=validation,
            title="",
            ingredients=[],
            steps=[],
            tags=[],
            confidence=0.0,
            disclaimer=DISCLAIMER,
            top_k_matches=matches,
        )

    # --- Title ---
    title = matches[0]["title"] if matches else "Unnamed Recipe"

    # --- Ingredients ---
    ingredients = _merge_ingredients(matches, top_n=3)
    if not ingredients:
        # Fallback: use title as hint
        ingredients = ["See recipe source for exact ingredients."]

    # --- Steps ---
    steps = _generate_steps(title, ingredients)

    # --- Tags ---
    tags = _infer_tags(title, ingredients)

    # --- Confidence ---
    confidence = _compute_confidence(matches, validation)

    log.info(
        "Draft generated: draft_id=%s title=%r ingredients=%d steps=%d confidence=%.3f",
        draft_id, title, len(ingredients), len(steps), confidence,
    )

    return DraftResult(
        draft_id=draft_id,
        validation=validation,
        title=title,
        ingredients=ingredients,
        steps=steps,
        tags=tags,
        confidence=confidence,
        disclaimer=DISCLAIMER,
        top_k_matches=matches,
    )
