#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
La Voz de César Vidal — RSS selectivo v6 acumulativo robusto:
- Editorial
- Despegamos
- Así fue España

Fuente: páginas públicas de iVoox del podcast La Voz de César Vidal.
No descarga audios; construye catalog.json y feed.xml con enlaces a iVoox.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PODCAST_BASE = "https://www.ivoox.com"
PODCAST_ID = "1102806"
PAGE_TEMPLATE = PODCAST_BASE + "/podcast-voz-cesar-vidal_sq_f1102806_{page}.html"

CATALOG_FILE = Path("catalog.json")
STATE_FILE = Path("backfill_state.json")
FEED_FILE = Path("feed.xml")

RECENT_KEEP = int(os.getenv("RECENT_KEEP", "90"))
BACKFILL_BATCH = int(os.getenv("BACKFILL_BATCH", "120"))
BACKFILL_HOLD_HOURS = int(os.getenv("BACKFILL_HOLD_HOURS", "24"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.75"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "900"))
STOP_AFTER_KNOWN_PAGES = int(os.getenv("STOP_AFTER_KNOWN_PAGES", "4"))

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/151 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
})

EPISODE_RE = re.compile(r"_rf_(\d+)_1\.html", re.I)
DATE_RE = re.compile(r"(?:^|\s)[-–—]\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")

def log(msg: str) -> None:
    print(msg, flush=True)

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def classify(title: str) -> str | None:
    t = norm(title)
    if re.match(r"^(?:el\s+)?editorial\s*[:\-–—]", t):
        return "Editorial"
    if re.match(r"^despegamos\s*[:\-–—]", t):
        return "Despegamos"
    if re.match(r"^asi\s+fue\s+espana\s*[:\-–—]", t):
        return "Así fue España"
    return None

def fetch(url: str, tries: int = 5, allow_404: bool = False) -> str | None:
    last = None
    for attempt in range(tries):
        try:
            r = SESSION.get(url, timeout=40)
            if allow_404 and r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last = exc
            wait = min(30, 2 ** attempt)
            log(f"Aviso: {exc}; reintento en {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"No se pudo cargar {url}: {last}")

def fix_date_year_typos(title: str) -> str:
    """
    Corrige erratas evidentes de año publicadas en iVoox, por ejemplo:
    07/12/3023 -> 07/12/2023
    06/12/3022 -> 06/12/2022

    Solo actúa sobre fechas dd/mm/3xxx al final del título.
    """
    def repl(m):
        d, mo, y = m.group(1), m.group(2), int(m.group(3))
        if 3000 <= y <= 3099:
            y -= 1000
        return f"{d}/{mo}/{y:04d}"

    return re.sub(
        r"(\d{1,2})/(\d{1,2})/(3\d{3})(?=\s*$)",
        repl,
        title or ""
    )

def parse_date_from_title(title: str) -> str | None:
    title = fix_date_year_typos(title)
    m = DATE_RE.search(title or "")
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    if y < 100:
        y += 2000 if y <= 60 else 1900
    try:
        dt = datetime(y, mo, d, 12, 0, tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None

def clean_title(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_page(page_no: int) -> tuple[list[dict], set[str]] | None:
    url = PAGE_TEMPLATE.format(page=page_no)
    text = fetch(url, allow_404=True)

    # En iVoox, la primera página inexistente (404) marca el final real del archivo.
    if text is None:
        return None

    soup = BeautifulSoup(text, "html.parser")
    all_ids = set(EPISODE_RE.findall(text))

    by_id: dict[str, dict] = {}

    for a in soup.find_all("a", href=True):
        href = urljoin(PODCAST_BASE, a.get("href"))
        m = EPISODE_RE.search(href)
        if not m:
            continue
        eid = m.group(1)

        candidates = [
            a.get("title", ""),
            a.get_text(" ", strip=True),
        ]
        parent = a.find_parent(["h1", "h2", "h3", "h4", "article"])
        if parent:
            candidates.append(parent.get_text(" ", strip=True))

        best = ""
        best_section = None
        for cand in candidates:
            cand = fix_date_year_typos(clean_title(cand))
            sec = classify(cand)
            if sec and len(cand) > len(best):
                best, best_section = cand, sec

        if not best_section:
            continue

        href = href.split("#", 1)[0]
        old = by_id.get(eid)
        if old is None or len(best) > len(old["title"]):
            by_id[eid] = {
                "id": int(eid),
                "section": best_section,
                "title": best,
                "url": href,
                "published": parse_date_from_title(best),
            }

    return list(by_id.values()), all_ids

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def crawl_catalog() -> list[dict]:
    old = load_json(CATALOG_FILE, [])

    # Limpieza retroactiva del catálogo ya existente:
    # corrige títulos/fechas imposibles como 3023 -> 2023 aunque el
    # rastreo incremental no vuelva a visitar esas páginas antiguas.
    cleaned_old = []
    for x in old:
        if not isinstance(x, dict):
            continue
        item = dict(x)
        fixed_title = fix_date_year_typos(str(item.get("title") or ""))
        item["title"] = fixed_title
        fixed_published = parse_date_from_title(fixed_title)
        if fixed_published:
            item["published"] = fixed_published
        cleaned_old.append(item)

    old = cleaned_old
    by_id = {str(x.get("id")): x for x in old if x.get("id") is not None}
    old_ids = set(by_id)
    first_run = not bool(old)

    total_new = 0
    consecutive_known_pages = 0

    for page in range(1, MAX_PAGES + 1):
        try:
            result = extract_page(page)
        except RuntimeError as exc:
            # iVoox a veces devuelve 5xx de forma transitoria en una página
            # histórica concreta. No dejamos que un fallo aislado tumbe todo
            # el RSS ni bloquee el backfill: se registra y se continúa.
            log(
                f"Aviso: se omite temporalmente la página {page} por error de iVoox: {exc}"
            )
            time.sleep(REQUEST_DELAY)
            continue

        # La primera página 404 es el final normal del histórico.
        if result is None:
            log(f"Fin del archivo de iVoox en página {page} (404 esperado).")
            break

        items, all_ids = result

        if not all_ids:
            log(f"Fin aparente del archivo en página {page}.")
            break

        new_here = 0
        for item in items:
            k = str(item["id"])
            if k not in by_id:
                by_id[k] = item
                new_here += 1
                total_new += 1
            else:
                # Refrescar título/fecha si mejora.
                merged = dict(by_id[k])
                merged.update({kk: vv for kk, vv in item.items() if vv not in (None, "")})
                by_id[k] = merged

        if not first_run:
            # El catálogo solo guarda Editorial/Despegamos/Así fue España.
            # Por tanto debemos comparar únicamente los IDs SELECCIONADOS de
            # esta página, no todos los episodios de iVoox. Así la actualización
            # diaria se detiene pronto al entrar en una zona ya catalogada.
            selected_ids = {str(item["id"]) for item in items}

            if selected_ids and all(i in old_ids for i in selected_ids):
                consecutive_known_pages += 1
            else:
                consecutive_known_pages = 0

            if consecutive_known_pages >= STOP_AFTER_KNOWN_PAGES:
                log(
                    f"Zona ya conocida alcanzada tras "
                    f"{consecutive_known_pages} páginas; "
                    "finaliza actualización incremental."
                )
                break

        if page % 20 == 0 or new_here:
            log(f"Página {page}: +{new_here} seleccionados; total catálogo {len(by_id)}")

        time.sleep(REQUEST_DELAY)

    catalog = list(by_id.values())
    catalog.sort(
        key=lambda x: (
            x.get("published") or "",
            int(x.get("id") or 0)
        ),
        reverse=True
    )

    log(f"Catálogo final: {len(catalog)} episodios seleccionados; nuevos: {total_new}")
    counts = {}
    for x in catalog:
        counts[x["section"]] = counts.get(x["section"], 0) + 1
    log("Por sección: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return catalog

def parse_dt(v: str | None) -> datetime:
    if not v:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

def xml_escape(v) -> str:
    return html.escape(str(v or ""), quote=True)

def cdata(v: str) -> str:
    return "<![CDATA[" + (v or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"

def choose_feed_items(catalog: list[dict]) -> list[dict]:
    """
    Backfill ACUMULATIVO para Feedly.

    - Los RECENT_KEEP más recientes permanecen siempre.
    - Cada BACKFILL_HOLD_HOURS se añaden BACKFILL_BATCH históricos más.
    - Los históricos ya expuestos NO se retiran.
    - Cuando se alcanza el final, feed.xml contiene todo el catálogo.

    Esto es más robusto que rotar lotes: si Feedly se salta una actualización,
    puede recuperar esos episodios en cualquier lectura posterior.
    """
    recent = catalog[:RECENT_KEEP]
    historical = catalog[RECENT_KEEP:]

    state = load_json(STATE_FILE, {})
    now = datetime.now(timezone.utc)

    # Migración automática desde el antiguo estado rotatorio.
    exposed = state.get("exposed_historical")
    if exposed is None:
        exposed = min(BACKFILL_BATCH, len(historical))
        last_growth = now
    else:
        exposed = max(0, min(int(exposed), len(historical)))
        last_growth = parse_dt(state.get("last_growth"))

    if exposed < len(historical):
        if not state or now - last_growth >= timedelta(hours=BACKFILL_HOLD_HOURS):
            exposed = min(len(historical), exposed + BACKFILL_BATCH)
            last_growth = now

    state = {
        "mode": "cumulative",
        "exposed_historical": exposed,
        "last_growth": last_growth.isoformat(),
        "recent_keep": RECENT_KEEP,
        "backfill_batch": BACKFILL_BATCH,
        "backfill_hold_hours": BACKFILL_HOLD_HOURS,
        "historical_total": len(historical),
        "total_exposed": len(recent) + exposed,
        "complete": exposed >= len(historical),
    }
    save_json(STATE_FILE, state)

    selected = recent + historical[:exposed]

    seen = set()
    result = []
    for x in selected:
        k = str(x["id"])
        if k not in seen:
            seen.add(k)
            result.append(x)

    log(
        f"Backfill acumulativo: {len(recent)} recientes + "
        f"{exposed}/{len(historical)} históricos = {len(result)} entradas."
    )
    return result

def build_feed(catalog: list[dict]) -> None:
    items = choose_feed_items(catalog)
    now = datetime.now(timezone.utc)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        '<channel>',
        '<title>La Voz de César Vidal — Selección</title>',
        '<link>https://www.ivoox.com/podcast-voz-cesar-vidal_sq_f1102806_1.html</link>',
        '<description>Editorial + Despegamos + Así fue España. Archivo filtrado desde iVoox.</description>',
        '<language>es-es</language>',
        f'<lastBuildDate>{format_datetime(now)}</lastBuildDate>',
        '<atom:link href="https://raw.githubusercontent.com/luisdrico-prog/cesar-vidal-seleccion-rss/main/feed.xml" rel="self" type="application/rss+xml" />',
    ]

    for it in items:
        pub = parse_dt(it.get("published"))
        section = it.get("section", "")
        title = it.get("title", "")
        url = it.get("url", "")
        parts.extend([
            '<item>',
            f'<title>{cdata(title)}</title>',
            f'<link>{xml_escape(url)}</link>',
            f'<guid isPermaLink="false">ivoox:{it["id"]}</guid>',
            f'<pubDate>{format_datetime(pub)}</pubDate>',
            f'<category>{cdata(section)}</category>',
            f'<description>{cdata(section + " — Abrir episodio en iVoox")}</description>',
            '</item>',
        ])

    parts.extend(['</channel>', '</rss>'])
    FEED_FILE.write_text("\n".join(parts) + "\n", encoding="utf-8")
    log(f"feed.xml generado con {len(items)} entradas expuestas.")

def main():
    catalog = crawl_catalog()
    save_json(CATALOG_FILE, catalog)
    build_feed(catalog)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
