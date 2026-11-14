"""Country flags for connection names: detected from the server's IP, or chosen by hand."""
from __future__ import annotations

import re
from typing import Optional

# Russian names for the countries VPS are usually rented in; anything else shows its ISO code.
COUNTRIES = {
    "AE": "ОАЭ", "AM": "Армения", "AT": "Австрия", "AU": "Австралия", "BE": "Бельгия", "BG": "Болгария",
    "BY": "Беларусь", "CA": "Канада", "CH": "Швейцария", "CY": "Кипр", "CZ": "Чехия", "DE": "Германия",
    "DK": "Дания", "EE": "Эстония", "ES": "Испания", "FI": "Финляндия", "FR": "Франция", "GB": "Великобритания",
    "GE": "Грузия", "GR": "Греция", "HK": "Гонконг", "HU": "Венгрия", "IE": "Ирландия", "IL": "Израиль",
    "IN": "Индия", "IS": "Исландия", "IT": "Италия", "JP": "Япония", "KR": "Южная Корея", "KZ": "Казахстан",
    "LT": "Литва", "LU": "Люксембург", "LV": "Латвия", "MD": "Молдова", "NL": "Нидерланды", "NO": "Норвегия",
    "PL": "Польша", "PT": "Португалия", "RO": "Румыния", "RS": "Сербия", "RU": "Россия", "SE": "Швеция",
    "SG": "Сингапур", "SK": "Словакия", "TR": "Турция", "UA": "Украина", "US": "США", "UZ": "Узбекистан",
}

_FLAG_RE = re.compile(r"^[\U0001F1E6-\U0001F1FF]{2}\s*")


def flag(iso: str) -> str:
    """'DE' -> '🇩🇪'."""
    iso = (iso or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", iso):
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)


def iso_of_flag(text: str) -> str:
    m = _FLAG_RE.match(text or "")
    if not m:
        return ""
    return "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in m.group(0).strip())


def strip_flag(text: str) -> str:
    return _FLAG_RE.sub("", text or "").strip()


def display_label(name: str, label: Optional[str], choice: Optional[str], detected: Optional[str]) -> str:
    """Name shown in client apps.

    choice: None/"auto" = detected country, "none" = no flag, else ISO code ("DE") or a flag emoji.
    A label without a flag gets one; a label with a flag gets it replaced only by an explicit choice.
    """
    text = strip_flag(label or "")
    own = iso_of_flag(label or "")
    if choice and choice.lower() == "none":
        iso = ""
    elif choice and choice.lower() != "auto":
        iso = iso_of_flag(choice) or choice.strip().upper()
    else:
        iso = own or (detected or "").upper()
    if not text:
        text = COUNTRIES.get(iso, name) if iso else name
    mark = flag(iso)
    return f"{mark} {text}" if mark else text


DETECT_SCRIPT = (
    # ipinfo matched the real city on every server we checked; ip-api is the fallback.
    "c=$(curl -s -m 6 https://ipinfo.io/country | tr -dc 'A-Z'); "
    "[ ${#c} -eq 2 ] || c=$(curl -s -m 6 'http://ip-api.com/line/?fields=countryCode' | tr -dc 'A-Z'); "
    "echo ${c}"
)


def display_labels(nodes, countries) -> dict:
    """Names for all servers at once: servers that would look the same in a client app
    ("🇩🇪 Germany" twice) get a short number in config order — "🇩🇪 Germany 1", "🇩🇪 Germany 2"."""
    base = {n.name: display_label(n.name, n.label, n.flag, countries.get(n.name)) for n in nodes}
    seen: dict = {}
    for n in nodes:
        if getattr(n, "enabled", True):
            seen.setdefault(base[n.name], []).append(n.name)
    out = dict(base)
    for label, names in seen.items():
        if len(names) > 1:
            for i, name in enumerate(names, 1):
                out[name] = f"{label} {i}"
    return out
