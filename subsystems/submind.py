import asyncio
from datetime import datetime
import re
import urllib.parse
import urllib.request
from ddgs import DDGS
from openai import OpenAI

# ==============================================================================
# SEARCH & WEATHER TRIGGER DEFINITIONS
# ==============================================================================
WEATHER_KEYWORDS = {"weather", "temp", "temperature", "forecast", "outside"}

SEARCH_KEYWORDS = {
    "search",
    "look up",
    "lookup",
    "google",
    "find out",
    "where can i",
    "where could i",
    "where to find",
    "where to buy",
    "nearest",
    "closest",
    "near me",
    "in stock",
    "stock",
    "availability",
    "available at",
    "how much is",
    "price of",
    "msrp",
    "news",
    "latest",
    "current",
    "today",
    "who won",
    "release date",
    "web",
    "internet",
    "online",
}

# Strips conversational filler so DuckDuckGo receives high-signal keywords
_SEARCH_FILLER_RE = re.compile(
    r"\b("
    r"can you|could you|would you|please|hey|static|"
    r"search the web for|search for|search|look up|lookup|google|find out|"
    r"where i could possibly find|where could i possibly find|where can i find|"
    r"where could i find|where to find|where to buy|tell me|what is|whats|"
    r"the|a|an|at|for|in|my|zip code|zipcode"
    r")\b",
    flags=re.IGNORECASE,
)

# Common retail names for two-step ZIP fallback searches
_RETAILER_RE = re.compile(
    r"\b("
    r"best buy|bestbuy|micro center|microcenter|walmart|target|costco|"
    r"gamestop|newegg|staples|office depot|home depot|lowes|lowe's|"
    r"menards|fleet farm|ace hardware|cvs|walgreens|autozone|oreilly|o'reilly"
    r")\b",
    flags=re.IGNORECASE,
)


# ==============================================================================
# QUERY SANITIZATION & STORE EXTRACTION
# ==============================================================================
def sanitize_search_query(raw_text: str) -> str:
    """Strips conversational filler and caps length so DDGS returns valid matches."""
    # Normalize GPU suffixes ("5060ti" -> "5060 ti") before stripping punctuation
    cleaned = re.sub(
        r"(\d{4})(ti|super|xt|xtx)\b", r"\1 \2", raw_text, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"[^\w\s\-]", " ", cleaned)
    cleaned = _SEARCH_FILLER_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Fallback to stripped original text if regex removed too much
    if len(cleaned) < 4:
        cleaned = re.sub(r"[^\w\s\-]", " ", raw_text).strip()

    # Cap at 10 tokens so verbose prompts don't return empty [] results
    return " ".join(cleaned.split()[:10])


def extract_store_location_query(
    raw_text: str, default_location: str = "Chicago, IL"
) -> str | None:
    """Builds a '<retailer> stores <zip>' fallback query when stock checks fail."""
    zip_match = re.search(r"\b(\d{5})\b", raw_text)
    if not zip_match:
        return None

    zip_code = zip_match.group(1)
    retailer_match = _RETAILER_RE.search(raw_text)
    if retailer_match:
        retailer = retailer_match.group(1).lower()
        return f"{retailer} stores {zip_code} {default_location}"

    return None


# ==============================================================================
# EXTERNAL DATA FETCHERS (WEATHER & DDGS MULTI-BACKEND)
# ==============================================================================
def _run_ddgs_backends(search_query: str, max_results: int = 4) -> list[str]:
    """Queries DDGS across reliable backends and formats clean bullet snippets."""
    for backend in ["duckduckgo", "lite", "auto"]:
        try:
            results = list(
                DDGS().text(
                    search_query,
                    region="us-en",
                    max_results=max_results,
                    backend=backend,
                )
            )
            if not results:
                continue

            snippets = []
            for r in results:
                title = r.get("title", "").strip()
                body = r.get("body", "").strip()
                href = r.get("href", "").strip()
                if body:
                    entry = f"- {title}: {body}" if title else f"- {body}"
                    if href:
                        entry += f" ({href})"
                    snippets.append(entry)

            if snippets:
                return snippets
        except Exception as e:
            print(f"[SEARCH WARNING] Backend '{backend}' failed: {e}")
    return []


def fetch_web_search(query: str, default_location: str = "Chicago, IL") -> str:
    """Runs sanitized DDGS search with store-location fallback and anti-hallucination guard."""
    clean_query = sanitize_search_query(query)
    print(f"[SEARCH] Primary sanitized query: '{clean_query}'")

    # Pass 1: Full product + location query
    snippets = _run_ddgs_backends(clean_query, max_results=4)
    if snippets:
        return "\n".join(snippets)

    # Pass 2: Two-step retailer + ZIP fallback when live stock returns empty
    store_query = extract_store_location_query(query, default_location)
    if store_query and store_query != clean_query.lower():
        print(f"[SEARCH] Primary empty -> running store fallback: '{store_query}'")
        store_snippets = _run_ddgs_backends(store_query, max_results=4)
        if store_snippets:
            joined_stores = "\n".join(store_snippets)
            return (
                f"PARTIAL_RESULTS (Live stock/product availability for '{clean_query}' returned zero matches, "
                f"but nearby store locations were found):\n{joined_stores}\n"
                "INSTRUCTION: Share the verified nearby store locations above, and clearly state that "
                "live item stock could not be verified online."
            )

    # Explicit empty-result guardrail prevents Gemma from inventing [Insert...] placeholders
    return (
        "NO_RESULTS_FOUND: Live web search returned zero verified matches for "
        f"'{clean_query}'. Tell the user no verified locations or stock data were found "
        "and do NOT invent addresses or bracketed placeholders."
    )


def fetch_exact_weather(default_location: str = "Chicago, IL") -> str:
    """Fetches concise local weather via wttr.in, falling back to DDGS if unreachable."""
    try:
        encoded_loc = urllib.parse.quote(default_location)
        url = f"https://wttr.in/{encoded_loc}?format=3"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return fetch_web_search(
            f"{default_location} current temperature weather today",
            default_location=default_location,
        )


async def enrich_user_prompt(
    raw_text: str,
    user_title: str = "Operator",
    default_location: str = "Chicago, IL",
) -> tuple[str, str | None]:
    """
    Routes !search, weather keywords, or search triggers to external fetchers.
    Returns (enriched_prompt, validation_error_or_none).
    """
    text = raw_text.strip()
    lowered = text.lower()

    # 1. Manual override: !search <query>
    if lowered.startswith("!search"):
        forced_query = text[7:].strip()
        if not forced_query:
            return (
                "",
                f"⚠️ Include a search topic after `!search`, {user_title} (e.g., "
                "`!search RTX 5060 Ti specs`).",
            )
        search_info = await asyncio.to_thread(
            fetch_web_search, forced_query, default_location
        )
        return f"{forced_query}\n[Live web search results:\n{search_info}]", None

    # 2. Automatic weather lookup
    if any(w in lowered for w in WEATHER_KEYWORDS):
        weather_info = await asyncio.to_thread(
            fetch_exact_weather, default_location
        )
        return (
            f"{text}\n[Live weather context for {default_location}: {weather_info}]",
            None,
        )

    # 3. Automatic web search lookup (keywords or 5-digit ZIP codes)
    if any(w in lowered for w in SEARCH_KEYWORDS) or re.search(
        r"\b\d{5}\b", lowered
    ):
        search_info = await asyncio.to_thread(
            fetch_web_search, text, default_location
        )
        return f"{text}\n[Live web search results:\n{search_info}]", None

    return text, None


# ==============================================================================
# CLOUD API FALLBACK ENGINE (OPT-IN GEMINI OPENAI-COMPATIBLE ENDPOINT)
# ==============================================================================
class CloudFallbackClient:
    """Encapsulates optional Gemini API fallback when local VRAM is offloaded."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://googleapis.com",
        model: str = "gemma-4-26b-a4b-it",
        reasoning_effort: str = "minimal",
    ):
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.reasoning_effort = reasoning_effort.strip()
        self.client = (
            OpenAI(base_url=base_url.strip(), api_key=self.api_key)
            if self.api_key
            else None
        )

    @property
    def is_enabled(self) -> bool:
        return self.client is not None

    def query(
        self,
        system_prompt: str,
        chat_history: list[dict],
        user_text: str | None = None,
        vram_stats: dict | None = None,
        messages_override: list[dict] | None = None,
        max_tokens: int = 700,
        temperature: float = 0.7,
    ) -> str | None:
        """Sends a fallback request to Gemini; returns None on error or if disabled."""
        if not self.is_enabled:
            return None

        if user_text:
            vram = vram_stats or {"percent": 0.0, "temp_c": None}
            temp_c = vram.get("temp_c")
            temp_str = f" | GPU Temp: {temp_c}°C" if temp_c is not None else ""
            now_str = datetime.now().strftime("%A, %I:%M %p")
            user_text = (
                f"[Current Time: {now_str} | VRAM: {vram['percent']}%{temp_str} | "
                f"State: CLOUD_OFFLOAD] {user_text}"
            )

        if messages_override is not None:
            payload_messages = messages_override
        else:
            recent_turns = chat_history[-10:] if len(chat_history) > 1 else []
            payload_messages = [{"role": "system", "content": system_prompt}] + [
                m for m in recent_turns if m.get("role") != "system"
            ]
            if user_text:
                last_msg = payload_messages[-1] if payload_messages else {}
                if not (
                    last_msg.get("role") == "user"
                    and last_msg.get("content") == user_text
                ):
                    payload_messages.append({"role": "user", "content": user_text})

        request_kwargs = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.reasoning_effort:
            request_kwargs["reasoning_effort"] = self.reasoning_effort

        try:
            res = self.client.chat.completions.create(**request_kwargs)
            return (res.choices[0].message.content or "").strip() or None
        except TypeError:
            request_kwargs.pop("reasoning_effort", None)
            try:
                res = self.client.chat.completions.create(**request_kwargs)
                return (res.choices[0].message.content or "").strip() or None
            except Exception as retry_err:
                print(f"[GEMINI FALLBACK WARN] Retry failed: {retry_err}")
                return None
        except Exception as e:
            print(f"[GEMINI FALLBACK WARN] Cloud request failed ({e}).")
            return None

