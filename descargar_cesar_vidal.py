#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Descargador automático de La Voz de César Vidal — FIX v2:
solo Editorial, Despegamos y Así fue España.

Modo híbrido:
1) primero descarga cualquier episodio NUEVO de las tres secciones;
2) después continúa con el histórico pendiente por lotes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import socket
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path

import requests
import yt_dlp
import imageio_ffmpeg
from mutagen.id3 import ID3, TIT2, TALB, TPE1, TDRC, COMM, TCON
from mutagen.mp3 import MP3

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
STATE_FILE = APP_DIR / "descargados.json"
LOG_FILE = APP_DIR / "descargador.log"

DEFAULT_CONFIG = {
    "catalog_url": "https://raw.githubusercontent.com/luisdrico-prog/cesar-vidal-seleccion-rss/main/catalog.json",
    "download_dir": str(Path.home() / "Music" / "Audios" / "Podcast" / "La Voz de Cesar Vidal"),
    "historical_per_run": 10,
    "max_historical_checks_per_run": 60,
    "failed_retry_hours": 24,
    "mp3_quality": "192",
    "delay_seconds": 2,
    "section_subfolders": True
}

INVALID = r'<>:"/\|?*'

def log(msg: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def config():
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    c = DEFAULT_CONFIG.copy()
    c.update(load_json(CONFIG_FILE, {}))
    c["download_dir"] = os.path.expandvars(os.path.expanduser(str(c["download_dir"])))
    return c

def sanitize(s: str, limit=165):
    s = re.sub(r"\s+", " ", s or "").strip()
    for ch in INVALID:
        s = s.replace(ch, "-")
    s = s.rstrip(". ")
    return (s[:limit].rstrip(". ") or "episodio")

def fetch_catalog(url):
    r = requests.get(url, timeout=45, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("catalog.json no es una lista.")
    data = [x for x in data if isinstance(x, dict) and x.get("id") and x.get("url")]
    data.sort(key=lambda x: int(x["id"]), reverse=True)
    return data

def date_str(item):
    raw = item.get("published")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z","+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return ""

def target_path(root: Path, item: dict):
    section = item.get("section") or "La Voz"
    folder = root / sanitize(section, 60)
    folder.mkdir(parents=True, exist_ok=True)

    title = item.get("title") or f"Episodio {item['id']}"
    # Quitar fecha final para no duplicarla en el nombre.
    title = re.sub(r"\s*[-–—]\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$", "", title).strip()
    d = date_str(item)
    name = sanitize(title, 145)
    if d:
        name += f" - {d}"
    return folder / (name + ".mp3")

def write_tags(path: Path, item: dict):
    try:
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        for key in ("TIT2","TALB","TPE1","TDRC","COMM","TCON"):
            tags.delall(key)
        tags.add(TIT2(encoding=3, text=item.get("title") or "La Voz de César Vidal"))
        tags.add(TALB(encoding=3, text="La Voz de César Vidal"))
        tags.add(TPE1(encoding=3, text="César Vidal"))
        tags.add(TCON(encoding=3, text=item.get("section") or "Podcast"))
        d = date_str(item)
        if d:
            tags.add(TDRC(encoding=3, text=d[:4]))
        tags.add(COMM(
            encoding=3, lang="spa", desc="Sección",
            text=item.get("section") or ""
        ))
        audio.save(v2_version=3)
    except Exception as exc:
        log(f"Aviso ID3: {exc}")


def host_resolves(hostname: str) -> bool:
    if not hostname:
        return True
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False

def is_dead_legacy_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == "ficheros.cesarvidal.com" and not host_resolves(host)

def probe_direct_media(item: dict, cfg: dict) -> tuple[str | None, dict | None]:
    """
    Extrae metadatos sin descargar para detectar enlaces históricos que
    apuntan al dominio desaparecido ficheros.cesarvidal.com.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 1,
        "ffmpeg_location": ffmpeg,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(item["url"], download=False)

    # Buscar URL directa en el formato elegido o en formatos disponibles.
    direct = None
    if isinstance(info, dict):
        direct = info.get("url")
        if not direct:
            formats = info.get("formats") or []
            for f in reversed(formats):
                if isinstance(f, dict) and f.get("url"):
                    direct = f["url"]
                    break
    return direct, info

def failed_is_in_cooldown(failed_entry: dict, hours: int) -> bool:
    raw = failed_entry.get("at")
    if not raw:
        return False
    try:
        then = datetime.fromisoformat(raw)
        age = datetime.now() - then
        return age.total_seconds() < hours * 3600
    except Exception:
        return False

def download(item, cfg, root):
    target = target_path(root, item)
    if target.exists() and target.stat().st_size > 100_000:
        log(f"Ya existe: {target.name}")
        return target

    # Primero averiguamos a qué servidor apunta realmente el audio.
    direct_url, _ = probe_direct_media(item, cfg)
    if direct_url and is_dead_legacy_host(direct_url):
        raise RuntimeError(
            "LEGACY_HOST_UNAVAILABLE: el audio apunta a "
            "ficheros.cesarvidal.com, dominio que actualmente no resuelve."
        )

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(target.with_suffix(".%(ext)s")),
        "noplaylist": True,
        "continuedl": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 45,
        "ffmpeg_location": ffmpeg,
        "postprocessors": [{
            "key":"FFmpegExtractAudio",
            "preferredcodec":"mp3",
            "preferredquality":str(cfg.get("mp3_quality","192"))
        }],
    }

    log(f"Descargando [{item.get('section')}] {item.get('title')}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([item["url"]])

    if not target.exists():
        prefix = target.stem[:50]
        cand = sorted(
            target.parent.glob(prefix + "*.mp3"),
            key=lambda p:p.stat().st_mtime,
            reverse=True
        )
        if cand:
            cand[0].replace(target)

    if not target.exists():
        raise RuntimeError("No encuentro el MP3 tras la descarga.")

    write_tags(target, item)
    log(f"OK: {target}")
    return target

def main():
    cfg = config()
    root = Path(cfg["download_dir"])
    root.mkdir(parents=True, exist_ok=True)

    state = load_json(STATE_FILE, {
        "completed_ids": [],
        "hybrid_baseline_id": None,
        "failed": {},
        "unavailable_ids": []
    })
    completed = {str(x) for x in state.get("completed_ids", [])}
    unavailable = {str(x) for x in state.get("unavailable_ids", [])}
    failed = state.setdefault("failed", {})

    cat = fetch_catalog(cfg["catalog_url"])
    if not cat:
        log("El catálogo está vacío.")
        return

    max_id = max(int(x["id"]) for x in cat)
    baseline = state.get("hybrid_baseline_id")
    if baseline is None:
        baseline = max_id
        state["hybrid_baseline_id"] = baseline
        state["initialized_at"] = datetime.now().isoformat(timespec="seconds")
        save_json(STATE_FILE, state)
        log(f"Modo híbrido inicializado en ID iVoox {baseline}.")

    retry_hours = max(1, int(cfg.get("failed_retry_hours", 24)))

    # Novedades: siempre tienen prioridad. No excluimos fallos transitorios
    # salvo que estén en cooldown, para evitar martillear el servidor.
    priority = []
    for x in cat:
        eid = str(x["id"])
        if int(x["id"]) <= int(baseline) or eid in completed or eid in unavailable:
            continue
        if eid in failed and failed_is_in_cooldown(failed[eid], retry_hours):
            continue
        priority.append(x)
    priority.sort(key=lambda x:int(x["id"]))

    # Históricos: escaneamos más candidatos de los que queremos descargar,
    # de modo que los audios muertos no bloqueen el progreso.
    historical_candidates = []
    for x in sorted(cat, key=lambda x:int(x["id"])):
        eid = str(x["id"])
        if int(x["id"]) > int(baseline) or eid in completed or eid in unavailable:
            continue
        if eid in failed and failed_is_in_cooldown(failed[eid], retry_hours):
            continue
        historical_candidates.append(x)

    target_successes = max(1, int(cfg.get("historical_per_run", 10)))
    max_checks = max(target_successes, int(cfg.get("max_historical_checks_per_run", 60)))
    historical_candidates = historical_candidates[:max_checks]

    log(
        f"Plan: {len(priority)} novedades prioritarias + "
        f"hasta {target_successes} históricos descargables "
        f"(máximo {len(historical_candidates)} comprobaciones)."
    )

    if not priority and not historical_candidates:
        log("No hay episodios pendientes disponibles en este momento.")
        return

    def process_item(item):
        eid = str(item["id"])
        try:
            p = download(item, cfg, root)
            completed.add(eid)
            failed.pop(eid, None)
            state["completed_ids"] = sorted((int(x) for x in completed))
            state["last_success"] = {
                "id": int(eid),
                "section": item.get("section"),
                "file": str(p),
                "at": datetime.now().isoformat(timespec="seconds")
            }
            save_json(STATE_FILE, state)
            return "ok"
        except Exception as exc:
            msg = str(exc)
            if "LEGACY_HOST_UNAVAILABLE" in msg or (
                "ficheros.cesarvidal.com" in msg
                and ("Failed to resolve" in msg or "getaddrinfo failed" in msg)
            ):
                log(
                    f"NO DISPONIBLE ID {eid}: el audio histórico apunta a "
                    f"ficheros.cesarvidal.com. Se salta y no bloqueará el progreso."
                )
                unavailable.add(eid)
                state["unavailable_ids"] = sorted((int(x) for x in unavailable))
                failed.pop(eid, None)
                state["last_unavailable"] = {
                    "id": int(eid),
                    "section": item.get("section"),
                    "title": item.get("title"),
                    "at": datetime.now().isoformat(timespec="seconds")
                }
                save_json(STATE_FILE, state)
                return "unavailable"
            else:
                log(f"ERROR ID {eid}: {exc}")
                failed[eid] = {
                    "error": msg,
                    "at": datetime.now().isoformat(timespec="seconds")
                }
                save_json(STATE_FILE, state)
                return "failed"

    # 1) Todas las novedades pendientes primero.
    for i, item in enumerate(priority, 1):
        log(f"[NUEVO {i}/{len(priority)}] ID {item['id']}")
        process_item(item)
        time.sleep(float(cfg.get("delay_seconds",2) or 0))

    # 2) Después seguimos el histórico hasta conseguir N descargas correctas,
    # o agotar el máximo de comprobaciones.
    historical_ok = 0
    checked = 0
    for item in historical_candidates:
        if historical_ok >= target_successes:
            break
        checked += 1
        log(
            f"[HIST {checked}/{len(historical_candidates)} | "
            f"OK {historical_ok}/{target_successes}] ID {item['id']}"
        )
        result = process_item(item)
        if result == "ok":
            historical_ok += 1
        time.sleep(float(cfg.get("delay_seconds",2) or 0))

    log(
        f"Finalizado: novedades procesadas={len(priority)}, "
        f"históricos descargados={historical_ok}, "
        f"históricos comprobados={checked}, "
        f"no disponibles acumulados={len(unavailable)}."
    )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelado.")
        raise SystemExit(130)
    except Exception as exc:
        log(f"ERROR GENERAL: {exc}")
        raise
