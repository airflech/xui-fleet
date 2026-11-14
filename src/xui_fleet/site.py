"""Cover website for every server: a plain small-business landing page.

A domain that opens nothing (or a bare nginx page) stands out; one that shows an ordinary
company site does not. Each domain or server gets a fictional brand picked from its name,
so the same name always shows the same site. No external resources: one static HTML file.
"""
from __future__ import annotations

import hashlib
import html
import re
from typing import Dict, List

# Fictional businesses (none of them refers to a real company).
THEMES: List[Dict] = [
    {
        "lang": "ru", "kind": "Антикафе настольных игр", "accent": "#e8553e", "bg": "#fff8f2", "ink": "#2a1f1a",
        "hero": "Вечер, который хочется повторить",
        "lead": "Больше 300 настольных игр, уютный зал на 40 мест и ведущие, которые объяснят правила за пять минут.",
        "cta": "Забронировать стол",
        "features": [("🎲", "300+ игр", "От «Каркассона» до редких новинок — каждую неделю пополняем полку."),
                     ("☕", "Чай и кофе включены", "Платите за время, а напитки и печенье — без ограничений."),
                     ("🧑‍🏫", "Ведущие", "Подберём игру под компанию и объясним правила."),
                     ("🎉", "Праздники", "Дни рождения, корпоративы и турниры под ключ.")],
        "prices": [("Будни до 18:00", "250 ₽/час"), ("Вечер и выходные", "350 ₽/час"), ("Безлимит на день", "990 ₽")],
        "price_title": "Цены", "contact": "Бронирование", "hours": "Ежедневно с 12:00 до 02:00",
        "footer": "Мы рады гостям и компаниям любого размера.",
    },
    {
        "lang": "ru", "kind": "Студия фото и видео для бизнеса", "accent": "#3b6ef5", "bg": "#f6f7fb", "ink": "#151827",
        "hero": "Снимаем так, что покупают",
        "lead": "Предметная съёмка для маркетплейсов, видео для соцсетей и портреты команды. Готовые материалы — за 3 дня.",
        "cta": "Обсудить съёмку",
        "features": [("📸", "Предметная съёмка", "Белый фон, инфографика и обложки по требованиям маркетплейсов."),
                     ("🎬", "Видео", "Ролики для соцсетей, обзоры товаров и интервью."),
                     ("💡", "Своя студия", "Циклорама, свет и реквизит — приезжайте с товаром."),
                     ("⚡", "Быстро", "Первые фотографии — на следующий день.")],
        "prices": [("Карточка товара", "от 490 ₽"), ("Ролик до 30 секунд", "от 6 000 ₽"), ("Аренда студии", "1 800 ₽/час")],
        "price_title": "Стоимость", "contact": "Связаться", "hours": "Пн–Сб, 10:00–20:00",
        "footer": "Работаем с селлерами, брендами и агентствами.",
    },
    {
        "lang": "ru", "kind": "Обжарка кофе и доставка", "accent": "#a2672b", "bg": "#fbf6ef", "ink": "#2b2118",
        "hero": "Свежая обжарка каждый вторник",
        "lead": "Обжариваем зерно небольшими партиями и привозим по городу на следующий день. Подберём кофе под вашу турку, рожок или фильтр.",
        "cta": "Выбрать кофе",
        "features": [("🌱", "Зерно из 9 стран", "Эфиопия, Колумбия, Бразилия, Кения и редкие микролоты."),
                     ("🔥", "Обжарка под заказ", "Светлая для фильтра, средняя для эспрессо."),
                     ("🚚", "Доставка", "Бесплатно от 1 500 ₽, в пункты выдачи — по всей стране."),
                     ("🏢", "Для офисов", "Зерно, аренда кофемашин и обслуживание.")],
        "prices": [("250 г", "от 690 ₽"), ("1 кг", "от 2 290 ₽"), ("Подписка, 1 кг в месяц", "1 990 ₽")],
        "price_title": "Цены", "contact": "Заказ и вопросы", "hours": "Отвечаем ежедневно с 9:00 до 21:00",
        "footer": "Маленькая обжарочная с большой любовью к кофе.",
    },
    {
        "lang": "en", "kind": "Web design & development studio", "accent": "#0f9d76", "bg": "#f5f8f7", "ink": "#122019",
        "hero": "Websites that work as hard as you do",
        "lead": "We design and build fast, accessible websites for small businesses — from the first sketch to launch and support.",
        "cta": "Start a project",
        "features": [("✏️", "Design", "Clean interfaces built around what your customers need."),
                     ("⚙️", "Development", "Modern, fast and easy-to-edit websites."),
                     ("📈", "Growth", "Analytics, SEO basics and conversion improvements."),
                     ("🛠", "Support", "Updates, backups and small changes every month.")],
        "prices": [("Landing page", "from €900"), ("Business website", "from €2,400"), ("Monthly care plan", "€90 / month")],
        "price_title": "Pricing", "contact": "Contact", "hours": "Mon–Fri, 9:00–18:00",
        "footer": "A small team that cares about the details.",
    },
]

_EN_NAMES = ["Kestrel Works", "Bluefen Studio", "Oakline Digital", "Harbor & Pine", "Lumenfield", "Copperleaf Studio"]


def _pick(seed: str, n: int) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % n


def brand_for(host: str) -> Dict:
    """Theme and brand name for a domain (Russian themes for .ru/.рф/.su) or a bare IP (English)."""
    host = host.lower().strip(".")
    is_ip = bool(re.fullmatch(r"[\d.]+|[0-9a-f:]+", host))
    ru = not is_ip and host.rsplit(".", 1)[-1] in ("ru", "su", "xn--p1ai")
    themes = [t for t in THEMES if (t["lang"] == "ru") == ru]
    # A name that hints at a business gets that business ("night-play" — board games), others by hash.
    hints = {"play": 0, "game": 0, "igr": 0, "foto": 1, "photo": 1, "media": 1, "kofe": 2, "coffee": 2, "cafe": 2}
    hinted = next((i for k, i in hints.items() if k in host.split(".")[0]), None)
    theme = dict(THEMES[hinted] if ru and hinted is not None else themes[_pick(host + "|v2", len(themes))])
    if is_ip:
        theme["name"] = _EN_NAMES[_pick(host, len(_EN_NAMES))]
        theme["email"] = ""
    else:
        label = host.split(".")[0]
        theme["name"] = " ".join(w.capitalize() for w in re.split(r"[-_]", label) if w) or host
        theme["email"] = f"hello@{host}"
    return theme


def render(host: str) -> str:
    t = brand_for(host)
    e = html.escape
    feats = "".join(f'<div class="f"><div class="fi">{i}</div><h3>{e(a)}</h3><p>{e(b)}</p></div>' for i, a, b in t["features"])
    prices = "".join(f'<li><span>{e(a)}</span><b>{e(b)}</b></li>' for a, b in t["prices"])
    contact = (f'<a class="btn" href="mailto:{e(t["email"])}">{e(t["email"])}</a>' if t["email"] else "")
    initials = "".join(w[0] for w in t["name"].split()[:2]).upper()
    year = "2026"
    return f"""<!doctype html>
<html lang="{t['lang']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(t['name'])} — {e(t['kind'])}</title>
<meta name="description" content="{e(t['lead'])}">
<style>
:root{{--a:{t['accent']};--bg:{t['bg']};--ink:{t['ink']};--mut:color-mix(in srgb,var(--ink) 62%,transparent)}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}}
.w{{max-width:1080px;margin:0 auto;padding:0 22px}}
header{{display:flex;align-items:center;justify-content:space-between;padding:22px 0}}
.logo{{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px;letter-spacing:-.01em}}
.mk{{width:36px;height:36px;border-radius:11px;background:var(--a);color:#fff;display:grid;place-items:center;font-size:14px;font-weight:800}}
nav a{{color:var(--ink);text-decoration:none;margin-left:22px;font-size:15px;opacity:.75}}nav a:hover{{opacity:1}}
.hero{{display:grid;grid-template-columns:1.15fr .85fr;gap:40px;align-items:center;padding:56px 0 72px}}
.tag{{display:inline-block;padding:5px 12px;border-radius:999px;background:color-mix(in srgb,var(--a) 14%,transparent);color:var(--a);font-weight:600;font-size:13px}}
h1{{font-size:clamp(34px,5vw,56px);line-height:1.06;letter-spacing:-.03em;margin:18px 0}}
.lead{{font-size:19px;color:var(--mut);max-width:34em}}
.btn{{display:inline-block;margin-top:26px;background:var(--a);color:#fff;text-decoration:none;font-weight:650;padding:14px 24px;border-radius:12px;box-shadow:0 10px 24px color-mix(in srgb,var(--a) 30%,transparent)}}
.art{{aspect-ratio:1;border-radius:32px;background:radial-gradient(circle at 30% 30%,color-mix(in srgb,var(--a) 55%,#fff),var(--a) 60%,color-mix(in srgb,var(--a) 70%,#000));
 position:relative;overflow:hidden;box-shadow:0 30px 60px color-mix(in srgb,var(--a) 25%,transparent)}}
.art:before,.art:after{{content:"";position:absolute;border-radius:50%;background:rgba(255,255,255,.18)}}
.art:before{{width:60%;height:60%;right:-12%;top:-10%}}.art:after{{width:42%;height:42%;left:-8%;bottom:-6%;background:rgba(255,255,255,.12)}}
section{{padding:56px 0}}h2{{font-size:32px;letter-spacing:-.02em;margin:0 0 26px}}
.fs{{display:grid;grid-template-columns:repeat(4,1fr);gap:18px}}
.f{{background:#fff;border-radius:18px;padding:22px;box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.05)}}
.fi{{font-size:26px}}.f h3{{margin:10px 0 6px;font-size:17px}}.f p{{margin:0;color:var(--mut);font-size:15px}}
.pr{{display:grid;grid-template-columns:1fr 1fr;gap:28px;align-items:start}}
ul.p{{list-style:none;margin:0;padding:0;background:#fff;border-radius:18px;box-shadow:0 8px 24px rgba(0,0,0,.05)}}
ul.p li{{display:flex;justify-content:space-between;gap:16px;padding:18px 22px;border-top:1px solid rgba(0,0,0,.06)}}ul.p li:first-child{{border-top:0}}
.box{{background:var(--ink);color:var(--bg);border-radius:22px;padding:30px}}.box p{{opacity:.75;margin:6px 0 0}}.box .btn{{margin-top:20px}}
footer{{padding:34px 0 46px;color:var(--mut);font-size:14px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}}
@media(max-width:820px){{.hero,.pr{{grid-template-columns:1fr}}.art{{max-width:340px}}.fs{{grid-template-columns:1fr 1fr}}nav{{display:none}}}}
@media(max-width:520px){{.fs{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="w">
<header><div class="logo"><span class="mk">{e(initials)}</span>{e(t['name'])}</div>
<nav><a href="#about">{'О нас' if t['lang'] == 'ru' else 'About'}</a><a href="#prices">{e(t['price_title'])}</a><a href="#contact">{e(t['contact'])}</a></nav></header>
<div class="hero"><div><span class="tag">{e(t['kind'])}</span><h1>{e(t['hero'])}</h1><p class="lead">{e(t['lead'])}</p>
<a class="btn" href="#contact">{e(t['cta'])}</a></div><div class="art" aria-hidden="true"></div></div>
<section id="about"><div class="fs">{feats}</div></section>
<section id="prices" class="pr"><div><h2>{e(t['price_title'])}</h2><ul class="p">{prices}</ul></div>
<div id="contact" class="box"><h2 style="margin:0">{e(t['contact'])}</h2><p>{e(t['hours'])}</p>{contact}</div></section>
<footer><span>© {year} {e(t['name'])}</span><span>{e(t['footer'])}</span></footer>
</div>
</body>
</html>
"""
