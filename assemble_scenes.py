#!/usr/bin/env python3
"""
assemble_scenes.py — повествовательный монтаж N сцен (documentary pipeline).
v7: потолок битрейта (maxrate/bufsize) + normalize ключей + stereo-аудио
    + валидация loudnorm + retry финала + probe-валидация кэша + бюджетный guard.
Вызов: python3 assemble_scenes.py manifest.json [--assets-dir assets] [--preflight]
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NL = chr(10)
VERSION_BANNER = "[ASSEMBLE] v8: asset verification + recursive Drive index + preflight report"
AUDIO_NORM = "aformat=sample_rates=48000:channel_layouts=stereo"

SECRET_KEYS = {
    "bot_token", "chat_id", "service_account", "sa_json", "pat", "github_token",
    "access_token", "refresh_token", "client_secret", "api_key", "token",
    "telegram_bot_token", "telegram_chat_id"
}
SEG_CACHE_DIR = Path("seg_cache")
SEG_CACHE_REMOTE = "gdrive:_aether_segcache"
CACHE_SCHEMA = 15

try:
    from lower_third_overlay import apply_lower_third_to_clip
    HAS_LT = True
except Exception:
    HAS_LT = False


def scrub(o):
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items() if k.lower() not in SECRET_KEYS}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    return o


def normalize(o):
    if isinstance(o, dict):
        return {(k.strip() if isinstance(k, str) else k): normalize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [normalize(v) for v in o]
    if isinstance(o, str):
        return o.strip()
    return o


def run(cmd, capture=False):
    print(f"{NL}[EXEC]: " + (" ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)), flush=True)
    if capture:
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, check=True)


def try_run(cmd):
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def probe(path):
    res = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)], capture=True)
    data = json.loads(res.stdout)
    dur = float(data.get("format", {}).get("duration", 0) or 0)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    try:
        video_duration = float(v.get("duration") or dur)
    except (TypeError, ValueError):
        video_duration = dur
    return {"duration": dur, "video_duration": video_duration, "has_audio": a is not None, "has_video": bool(v),
            "width": v.get("width"), "height": v.get("height"),
            "fps": v.get("avg_frame_rate"),
            "bit_rate": int(v.get("bit_rate") or (data.get("format") or {}).get("bit_rate") or 0)}


def segment_is_valid(path, expected=None):
    try:
        info = probe(path)
        if not (info["duration"] > 0 and info["has_audio"] and info["has_video"]):
            return False
        if expected is None:
            return True
        dur, w, h, fps = expected
        num, den = map(int, info["fps"].split("/"))
        return (abs(info["duration"] - dur) <= max(0.12, 2 / fps)
                and (info["width"], info["height"]) == (w, h)
                and den > 0 and abs(num / den - fps) < 0.05)
    except Exception:
        return False


def fmt_clock(sec):
    s = int(round(max(0.0, sec)))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m}:{ss:02d}"


def fmt_srt(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


DRIVE_INDEX = {}

def resolve_asset(name, assets_dir, folder_id):
    clean = str(name or "").strip()
    if not clean:
        return None
    relative = Path(clean)
    if relative.is_absolute() or ".." in relative.parts or "\\" in clean:
        raise ValueError("Unsafe asset path: " + clean)
    for c in (Path(assets_dir) / clean, Path(clean)):
        if c.is_file():
            return c
    ad = Path(assets_dir)
    local = [f for f in ad.rglob("*") if f.is_file() and
             (f.relative_to(ad).as_posix().casefold() == clean.casefold()
              if "/" in clean else f.name.casefold() == clean.casefold())]
    if len(local) > 1:
        raise ValueError("Ambiguous local asset: " + clean)
    if local:
        return local[0]
    if folder_id:
        root = ["--drive-root-folder-id", folder_id]
        if folder_id not in DRIVE_INDEX:
            result = subprocess.run(["rclone", "lsjson", "gdrive:", "--recursive", "--files-only"] + root,
                                    check=True, capture_output=True, text=True)
            DRIVE_INDEX[folder_id] = [x["Path"] for x in json.loads(result.stdout)]
        paths = DRIVE_INDEX[folder_id]
        matches = [p for p in paths if (p.casefold() == clean.casefold()
                   if "/" in clean else Path(p).name.casefold() == clean.casefold())]
        if len(matches) > 1:
            raise ValueError("Ambiguous Drive asset: " + clean + " -> " + ", ".join(matches))
        if matches:
            dst = ad / clean
            dst.parent.mkdir(parents=True, exist_ok=True)
            partial = dst.with_name(dst.name + ".partial")
            try:
                if try_run(["rclone", "copyto", "gdrive:" + matches[0], str(partial)] + root):
                    if partial.is_file():
                        os.replace(partial, dst)
                        print(f"[DOWNLOAD] {clean} <- Google Drive ({matches[0]})", flush=True)
                        return dst
            finally:
                partial.unlink(missing_ok=True)
    return None


def verify_assets(entries, assets_dir, folder_id, lt_cfg, sentence_pad, strict=False):
    report = {"status": "FAIL", "scenes": [], "errors": [], "warnings": [],
              "planned_duration_sec": 0.0, "estimated_effective_duration_sec": None}
    errors = report["errors"]
    cache = {}
    def check(name, kind, required=True):
        if not name:
            if required:
                errors.append(f"Missing {kind} reference")
            return None
        key = (str(name), kind)
        if key in cache:
            return cache[key]
        try:
            path = resolve_asset(name, assets_dir, folder_id)
            if path is None:
                raise ValueError("file not found")
            if kind == "emblem":
                from PIL import Image
                with Image.open(path) as im:
                    im.verify()
                info = {"file": str(name)}
            else:
                info = probe(path)
                if not info["has_video" if kind == "video" else "has_audio"]:
                    raise ValueError("required stream missing")
                if kind != "video" or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                    if not math.isfinite(info["duration"]) or info["duration"] <= 0:
                        raise ValueError("invalid media duration")
                info["file"] = str(name)
                info["path"] = str(path.resolve())
            cache[key] = info
            return info
        except Exception as exc:
            errors.append(f"{kind}: {name}: {exc}")
            cache[key] = None
            return None
    effective = 0.0
    seen_video = {}
    for i, e in enumerate(entries, 1):
        assets = e.get("assets") or {}
        video = check(e.get("file_name") or e.get("video_file") or assets.get("video_file") or assets.get("image_reference"), "video")
        voice_name = e.get("voiceover_file") or e.get("audio_file") or assets.get("voiceover_file")
        voice = check(voice_name, "voice", False)
        ambient = e.get("ambient_audio") or assets.get("ambient_audio")
        if isinstance(ambient, dict):
            ambient = ambient.get("file_name")
        ambient_info = check(ambient, "ambient", False)
        planned = float(e.get("duration_sec") or e.get("duration") or (video or {}).get("duration") or 6)
        duration = planned
        sync = {}
        if video:
            try:
                fingerprint = file_fingerprint(video["path"])["sha256"]
                if fingerprint in seen_video and e.get("allow_video_reuse") is not True:
                    errors.append(f"Scene {i}: duplicate video content from scene {seen_video[fingerprint]}")
                seen_video.setdefault(fingerprint, i)
                duration, sync = scene_timing(e, Path(video["path"]), Path(voice["path"]) if voice else None, sentence_pad)
                sync["camera_policy"] = camera_policy(e, video["path"])
            except Exception as exc:
                errors.append(f"Scene {i}: {exc}")
        report["planned_duration_sec"] += planned
        effective += duration
        report["scenes"].append({"scene_index": i, "scene_id": e.get("scene_id"), "video": video,
                                 "voice": voice, "ambient": ambient_info, "planned_duration_sec": planned,
                                 "estimated_duration_sec": duration, "sync": sync})
    if (lt_cfg.get("brand_identity") or {}).get("enabled") or (lt_cfg.get("enabled", True) and any(
            e.get("text_overlay") or e.get("lower_third") or e.get("location") or e.get("date_era") for e in entries)):
        before = len(errors)
        report["emblem"] = check(lt_cfg.get("emblem_path") or "aether_art_150x150_A.png", "emblem")
        if not strict and len(errors) > before:
            report["warnings"].extend(errors[before:])
            del errors[before:]
    report["status"] = "FAIL" if errors else "PASS"
    if not errors:
        report["estimated_effective_duration_sec"] = round(effective, 3)
    Path("preflight_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        for error in errors:
            print("[PREFLIGHT] " + error, file=sys.stderr)
        raise SystemExit(1)
    return effective


def scene_timing(entry, vid_path, voice_path=None, sentence_pad=0.35):
    still = vid_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    mode = str(entry.get("video_duration_mode") or ("freeze" if still else "native")).lower()
    if mode not in ("native", "stretch", "freeze", "loop"):
        raise ValueError("Unsupported video_duration_mode: " + mode)
    info = probe(vid_path)
    source = info["video_duration"] or info["duration"]
    if mode == "native" and not still:
        if not math.isfinite(source) or source <= 0:
            raise ValueError("Invalid native video duration")
        active_end = None
        if voice_path:
            audio = probe(voice_path)["duration"]
            result = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(voice_path),
                                     "-af", "silencedetect=noise=-40dB:d=0.15", "-f", "null", "-"],
                                    check=True, capture_output=True, text=True)
            starts = re.findall(r"silence_start: ([0-9.]+)", result.stderr)
            ends = re.findall(r"silence_end: ([0-9.]+)", result.stderr)
            active_end = audio
            if starts and ends and float(ends[-1]) >= audio - 0.08:
                active_end = float(starts[-1])
            if active_end <= 0:
                raise ValueError("Voiceover appears silent")
            if active_end > source:
                raise ValueError(f"Voice exceeds native video: {active_end:.3f}s > {source:.3f}s")
        return source, {"mode": mode, "source_duration_sec": source, "voice_active_end_sec": active_end,
                        "voice_start_sec": 0.0, "silence_threshold_db": -40}
    dur = float(entry.get("duration_sec") or entry.get("duration") or source or 6.0)
    if voice_path:
        dur = max(dur, probe(voice_path)["duration"] + float(entry.get("voice_start", 0.2)) + sentence_pad)
    return dur, {"mode": mode, "source_duration_sec": source,
                 "voice_start_sec": float(entry.get("voice_start", 0.2))}


def entry_duration(entry, vid_path, voice_path=None, sentence_pad=0.35):
    return scene_timing(entry, vid_path, voice_path, sentence_pad)[0]


def file_fingerprint(path):
    if not path:
        return None
    path = Path(path)
    if not path.is_file():
        return {"missing": str(path)}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest()}


def seg_cache_key(entry, vid, voice, ambient, w, h, fps, preset, crf, lt_on, dur, maxrate, lt_cfg, bufsize):
    payload = {
        "ver": CACHE_SCHEMA,
        "renderer": file_fingerprint(__file__),
        "vid": file_fingerprint(vid),
        "voice": file_fingerprint(voice),
        "ambient": file_fingerprint(ambient),
        "dur": dur,
        "vs": entry.get("voice_start", 0.2),
        "camera_policy": camera_policy(entry, vid),
        "camera_movement": entry.get("camera_movement", "static"),
        "camera_zoom": entry.get("camera_zoom", 1.12),
        "w": w, "h": h, "fps": fps, "preset": preset, "crf": crf,
        "maxrate": maxrate, "bufsize": bufsize,
        "lt": bool(lt_on),
        "overlay": entry.get("text_overlay"),
        "lower_third": entry.get("lower_third"),
        "lt_cfg": lt_cfg if lt_on else None,
        "emblem": file_fingerprint(lt_cfg.get("emblem_path")) if lt_on else None,
        "overlay_engine": file_fingerprint(Path(__file__).with_name("lower_third_overlay.py")) if lt_on else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def cache_get(key, expected):
    local = SEG_CACHE_DIR / f"{key}.mp4"
    SEG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if local.is_file():
        if segment_is_valid(local, expected):
            print(f"[CACHE HIT] seg {key[:10]} (local)", flush=True)
            return local
        local.unlink()
        print(f"[CACHE INVALID] {key[:10]} (local)", flush=True)
    partial = local.with_suffix(f".{os.getpid()}.download")
    try:
        if try_run(["rclone", "copyto", f"{SEG_CACHE_REMOTE}/{key}.mp4", str(partial), "--transfers", "2"]):
            if partial.is_file() and segment_is_valid(partial, expected):
                os.replace(partial, local)
                print(f"[CACHE HIT] seg {key[:10]} (Google Drive)", flush=True)
                return local
            print(f"[CACHE INVALID] {key[:10]} (Google Drive)", flush=True)
    finally:
        partial.unlink(missing_ok=True)
    return None


def cache_put(key, path):
    dst = SEG_CACHE_DIR / f"{key}.mp4"
    partial = dst.with_suffix(f".{os.getpid()}.tmp")
    try:
        SEG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, partial)
        os.replace(partial, dst)
        if not try_run(["rclone", "copyto", str(dst), f"{SEG_CACHE_REMOTE}/{key}.mp4", "--transfers", "2"]):
            print(f"[CACHE WARN] remote upload failed: {key[:10]}", flush=True)
    except Exception as exc:
        print(f"[CACHE WARN] {exc}", flush=True)
    finally:
        partial.unlink(missing_ok=True)


def camera_policy(entry, path):
    still = Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    policy = str(entry.get("camera_policy") or ("cinematic" if still else "native")).lower()
    if policy not in ("native", "cinematic"):
        raise ValueError("Unsupported camera_policy: " + policy)
    return policy


def cinematic_camera_filter(entry, w, h, fps, dur):
    """Apply explicitly requested camera motion, including image-derived MP4s."""
    mode = str(entry.get("camera_movement") or "static").lower()
    if mode in ("static", "none"):
        return None
    if mode not in ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down"):
        raise ValueError(f"Unsupported camera_movement: {mode}")
    zoom = float(entry.get("camera_zoom", 1.12))
    if not math.isfinite(zoom) or not 1.0 <= zoom <= 1.5:
        raise ValueError("camera_zoom must be between 1.0 and 1.5")
    frames = max(1, math.ceil(dur * fps) - 1)
    p = f"(0.5-0.5*cos(PI*min(on/{frames},1)))"
    z = str(zoom)
    x, y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    if mode == "zoom_in":
        z = f"1+{zoom-1}*{p}"
    elif mode == "zoom_out":
        z = f"{zoom}-{zoom-1}*{p}"
    elif mode == "pan_left":
        x = f"(iw-iw/zoom)*(1-{p})"
    elif mode == "pan_right":
        x = f"(iw-iw/zoom)*{p}"
    elif mode == "pan_up":
        y = f"(ih-ih/zoom)*(1-{p})"
    elif mode == "pan_down":
        y = f"(ih-ih/zoom)*{p}"
    # Oversample to reduce integer crop-position jitter; d=1 preserves video motion.
    sw, sh = w * 2, h * 2
    return (f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
            f"crop={sw}:{sh},setsar=1,fps={fps},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps},"
            "format=yuv420p")


def transition_filter(incoming, outgoing, dur):
    """Non-overlapping dip transitions; audio and timeline remain untouched."""
    def half(entry):
        if not entry:
            return 0.0, "black"
        mode = str(entry.get("transition") or "cut").lower()
        if mode in ("cut", "none", "hard_cut"):
            return 0.0, "black"
        if mode not in ("fade", "dip_to_black", "fade_black", "dip_to_white", "fade_white"):
            raise ValueError(f"Unsupported transition: {mode}")
        seconds = float(entry.get("transition_duration", 0.5))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("transition_duration must be finite and non-negative")
        return seconds / 2, "white" if "white" in mode else "black"
    a, ac = half(incoming)
    b, bc = half(outgoing)
    a, b = min(a, dur / 2), min(b, dur / 2)
    filters = []
    if a:
        filters.append(f"fade=t=in:st=0:d={a}:color={ac}")
    if b:
        filters.append(f"fade=t=out:st={dur-b}:d={b}:color={bc}")
    return ",".join(filters)


def build_segment(entry, idx, assets_dir, folder_id, w, h, fps, preset, crf,
                  maxrate_mbps, bufsize_mbps, lt_cfg, tmp_dir, incoming=None, outgoing=None,
                  audio_bitrate=192, sentence_pad=0.35):
    fn = (entry.get("file_name") or entry.get("video_file")
          or (entry.get("assets") or {}).get("video_file")
          or (entry.get("assets") or {}).get("image_reference"))
    vid = resolve_asset(fn, assets_dir, folder_id)
    if vid is None:
        raise SystemExit(f"[P0-GUARD] Сцена #{idx}: не удалось найти {fn}")
    voice_name = (entry.get("voiceover_file") or entry.get("audio_file")
                  or (entry.get("assets") or {}).get("voiceover_file"))
    voice = resolve_asset(voice_name, assets_dir, folder_id) if str(voice_name or "").strip() else None
    ambient_name = entry.get("ambient_audio") or (entry.get("assets") or {}).get("ambient_audio")
    ambient_cfg = ambient_name if isinstance(ambient_name, dict) else {}
    ambient_name = ambient_cfg.get("file_name") if ambient_cfg else ambient_name
    ambient = resolve_asset(ambient_name, assets_dir, folder_id) if str(ambient_name or "").strip() else None
    if voice_name and voice is None:
        raise SystemExit(f"[AUDIO] Scene {idx}: missing voiceover {voice_name}")
    if ambient_name and ambient is None:
        raise SystemExit(f"[AUDIO] Scene {idx}: missing ambient {ambient_name}")
    source = probe(vid)
    source_enabled = entry.get("source_audio_enabled", True)
    if not isinstance(source_enabled, bool):
        raise ValueError("source_audio_enabled must be boolean")
    source_gain = float(entry.get("source_audio_volume", 0.7))
    if not math.isfinite(source_gain) or not 0 <= source_gain <= 4:
        raise ValueError("source_audio_volume must be finite and between 0 and 4")
    source_on = source_enabled and source["has_audio"] and source_gain > 0
    voice_gain = float(entry.get("voiceover_volume", 1.0))
    ambient_gain = float(ambient_cfg.get("volume", entry.get("ambient_volume", 0.35)))
    if not all(math.isfinite(g) and 0 <= g <= 4 for g in (voice_gain, ambient_gain)):
        raise ValueError("Audio volume must be finite and between 0 and 4")
    dur, sync = scene_timing(entry, vid, voice, sentence_pad)
    text_overlay = str(entry.get("text_overlay") or "").strip()
    lower = entry.get("lower_third") or {}
    if isinstance(lower, dict) and not text_overlay:
        location = str(lower.get("location") or entry.get("location") or "").strip()
        era = str(lower.get("era") or entry.get("date_era") or "").strip()
        title = str(lower.get("title") or "").strip()
        text_overlay = location + "\n" + " • ".join(x for x in (era, title) if x)
        text_overlay = text_overlay.strip()
    lt_on = (bool(lt_cfg.get("enabled", True)) and bool(text_overlay)) or bool((lt_cfg.get("brand_identity") or {}).get("enabled", False))
    if lt_on and not HAS_LT:
        raise SystemExit("[BRANDING] lower_third_overlay unavailable")
    lt_cfg = dict(lt_cfg)
    if lt_on:
        emblem = resolve_asset(lt_cfg.get("emblem_path") or "aether_art_150x150_A.png", assets_dir, folder_id)
        if emblem is None:
            emblem = resolve_asset("aether_art_150x150_A.png", assets_dir, folder_id)
        if emblem is None:
            print("[BRANDING WARN] emblem missing; rendering text panel only", flush=True)
        lt_cfg["emblem_path"] = str(emblem) if emblem else ""

    key = seg_cache_key(entry, vid, voice, ambient, w, h, fps, preset, crf, lt_on, dur, maxrate_mbps, lt_cfg, bufsize_mbps)
    transition = transition_filter(incoming, outgoing, dur)
    key = hashlib.sha256(f'{key}|{transition}|{voice_gain}|{ambient_gain}|{audio_bitrate}|{sentence_pad}|{sync["mode"]}|{source_on}|{source_gain}'.encode()).hexdigest()
    expected = (dur, w, h, fps)
    hit = cache_get(key, expected)
    if hit:
        return hit, dur, sync["voice_start_sec"]

    voice_start = sync["voice_start_sec"]
    delay_ms = max(0, int(round(voice_start * 1000)))
    out = tmp_dir / f"seg_{idx:03d}.mp4"
    scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p")

    inputs = ["-i", str(vid)]
    if voice is not None:
        inputs += ["-i", str(voice)]
    if ambient is not None:
        inputs += ["-stream_loop", "-1", "-i", str(ambient)]

    policy = camera_policy(entry, vid)
    camera = cinematic_camera_filter(entry, w, h, fps, dur) if policy == "cinematic" else None
    print(f"[CAMERA] Scene {idx}: {policy}; filter={'cinematic' if camera else 'fit without crop'}", flush=True)
    # Never repeat source motion unless explicitly requested.
    mode = sync["mode"]
    source_dur = source.get("video_duration") or source["duration"]
    still = vid.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    timing = "setpts=PTS-STARTPTS,"
    if mode == "loop" and not still:
        inputs = ["-stream_loop", "-1"] + inputs
    elif still:
        timing += f"tpad=stop_mode=clone:stop_duration={dur},"
    elif not math.isfinite(source_dur) or source_dur <= 0:
        raise ValueError("Invalid source video duration: " + str(vid))
    elif source_dur < dur and mode == "stretch":
        timing += f"setpts={dur/source_dur:.9f}*PTS,"
    # Clone the final frame to cover rounding or an early stream end.
    print(f"[VIDEO FIT] Scene {idx}: {source_dur:.3f}s -> {dur:.3f}s ({mode})", flush=True)
    v_chain = (f"[0:v]{timing}{camera or scale},tpad=stop_mode=clone:stop_duration={dur},trim=duration={dur},setpts=PTS-STARTPTS"
               + (f",{transition}" if transition else "") + "[v]")
    a_parts, mix_inputs = [], []
    if voice is not None:
        a_parts.append(
            f"[1:a]{AUDIO_NORM},volume={voice_gain},adelay={delay_ms}:all=1,apad,atrim=0:{dur},"
            f"asetpts=PTS-STARTPTS,{AUDIO_NORM}[vox]"
        )
        mix_inputs.append("[vox]")
    if ambient is not None:
        aidx = 2 if voice is not None else 1
        a_parts.append(
            f"[{aidx}:a]volume={ambient_gain},aresample=48000,apad,atrim=0:{dur},"
            f"asetpts=PTS-STARTPTS,{AUDIO_NORM}[amb]"
        )
        mix_inputs.append("[amb]")
    if source_on:
        # Preserve source audio timestamps relative to the video. Explicit stretch
        # modes need matching audio tempo; native mode never changes tempo.
        tempo = ""
        if mode == "stretch" and not still and source_dur < dur:
            ratio = source_dur / dur
            factors = []
            while ratio < 0.5:
                factors.append("atempo=0.5")
                ratio *= 2
            factors.append(f"atempo={ratio:.9f}")
            tempo = "," + ",".join(factors)
        a_parts.append(
            f"[0:a:0]aresample=48000:async=1:first_pts=0,{AUDIO_NORM}{tempo},"
            f"volume={source_gain},apad,atrim=0:{dur},asetpts=PTS-STARTPTS[srcenv]"
        )
        mix_inputs.append("[srcenv]")
    print(f"[SOURCE AUDIO] Scene {idx}: present={source['has_audio']} enabled={source_on} gain={source_gain}", flush=True)
    if not mix_inputs:
        a_parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur},{AUDIO_NORM}[aout]")
        amix = "[aout]"
    elif len(mix_inputs) == 1:
        a_parts[-1] = a_parts[-1].replace("[vox]", "[aout]").replace("[amb]", "[aout]").replace("[srcenv]", "[aout]")
        amix = "[aout]"
    else:
        a_parts.append(
            f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:"
            f"dropout_transition=0:normalize=0,alimiter=limit=0.95:level=0:latency=1,{AUDIO_NORM}[aout]"
        )
        amix = "[aout]"

    fc = v_chain + ";" + ";".join(a_parts)
    t_seg = time.monotonic()
    # CRF управляет качеством, maxrate/bufsize срезают пики битрейта от зерна.
    rate_args = []
    if maxrate_mbps > 0:
        rate_args = ["-maxrate", f"{maxrate_mbps}M", "-bufsize", f"{bufsize_mbps}M"]
    run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]", "-map", amix,
         "-c:v", "libx264", "-preset", preset, "-crf", str(crf)] + rate_args +
        ["-pix_fmt", "yuv420p", "-g", str(fps * 2), "-keyint_min", str(fps * 2),
         "-c:a", "aac", "-b:a", f"{audio_bitrate}k", "-ar", "48000", "-ac", "2", "-t", f"{dur}",
         "-movflags", "+faststart", str(out)])
    wall = (time.monotonic() - t_seg) / 60.0

    final_out = out
    if lt_on:
        ov = tmp_dir / f"seg_{idx:03d}_ov.mp4"
        show = lt_cfg.get("show_sec", [0.5, 5.5])
        apply_lower_third_to_clip(
            input_video_path=str(out), output_video_path=str(ov),
            text_overlay=text_overlay, fps=fps,
            emblem_path=str(lt_cfg.get("emblem_path", "")) or None,
            show_start=float(show[0]), show_end=float(show[1]),
            anim_dur=float(lt_cfg.get("anim_sec", 0.55)),
            work_dir=str(tmp_dir / f"lt_{idx:03d}"), config=lt_cfg,
            crf=crf, preset=preset, maxrate=maxrate_mbps, bufsize=bufsize_mbps,
        )
        final_out = ov
    if not segment_is_valid(final_out, expected):
        raise SystemExit(f"[P0-GUARD] Сегмент сцены #{idx} собран невалидно (нет видео/аудио).")
    mb = final_out.stat().st_size / (1024 * 1024)
    print(f"[SEG {idx}] {dur:.1f}s контента за {wall:.1f} мин wall • {mb:.1f} MB "
          f"(speed {dur / 60.0 / max(wall, 0.01):.2f}x)", flush=True)
    cache_put(key, final_out)
    return final_out, dur, voice_start


def _is_finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def measure_loudness(concat_list):
    """Первый проход loudnorm. None, если измерения невалидны или вырождены."""
    res = subprocess.run(
        ["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-map", "0:a", "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", res.stderr)
    if not m:
        return None
    try:
        data = json.loads(m[-1])
    except Exception:
        return None
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if not all(_is_finite(data.get(k)) for k in keys):
        print("[LOUDNORM] Измерения содержат inf/NaN — переход на однопроходный режим.", flush=True)
        return None
    if float(data["input_i"]) <= -70.0 or float(data["input_lra"]) <= 0.0:
        print("[LOUDNORM] Измерения вырождены (тишина/сбой чтения) — переход на однопроходный режим.", flush=True)
        return None
    return data


def main():
    print(VERSION_BANNER, flush=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?", default="manifest.json")
    ap.add_argument("--assets-dir", default="assets")
    ap.add_argument("--preflight", action="store_true")
    args = ap.parse_args()

    path = Path(args.manifest)
    if not path.is_file():
        path = Path("current_manifest.json")
    manifest = normalize(scrub(json.loads(path.read_text(encoding="utf-8"))))

    entries = manifest.get("timeline") or manifest.get("scenes") or []
    if not entries:
        raise SystemExit("[P0-GUARD] Манифест не содержит сцен (timeline/scenes пусты).")

    export = manifest.get("export_settings") or {}
    res_mode = str(export.get("resolution", "4k")).lower()
    dims = {"720p": (1280, 720), "1080p": (1920, 1080), "2k": (2560, 1440), "4k": (3840, 2160)}
    w, h = dims.get(res_mode, (3840, 2160))
    fps = int(export.get("fps", 24))
    preset = str(export.get("preset", "ultrafast" if res_mode == "720p" else "veryfast"))
    crf = int(export.get("crf", 23 if preset == "ultrafast" else 20))
    lt_cfg = dict(export.get("lower_third_config") or {})
    brand = dict(export.get("brand_identity") or lt_cfg.get("brand_identity") or {})
    project = str(manifest.get("project_name", ""))
    if "enabled" not in brand:
        brand["enabled"] = project.lower().startswith("histoviva")
    if brand["enabled"] and not brand.get("episode_label"):
        match = re.match(r"histoviva_(EP\d+)_(.+)", project, re.I)
        brand["episode_label"] = (match.group(1) + " | " + match.group(2).replace("_", " ")) if match else project
    lt_cfg["brand_identity"] = brand
    audio_bitrate = int(export.get("audio_bitrate_kbps", 192))
    sentence_pad = float((manifest.get("anti_desync_locks") or {}).get("voiceover_sentence_pad_sec", 0.35))
    if not 64 <= audio_bitrate <= 320 or not math.isfinite(sentence_pad) or sentence_pad < 0:
        raise ValueError("Invalid audio bitrate or sentence padding")

    # Потолок битрейта: берём bitrate_mbps из манифеста с запасом x1.3 (по умолчанию 45M для 4K).
    default_cap = {3840: 45, 2560: 25, 1920: 16, 1280: 8}.get(w, 45)
    declared = float(export.get("bitrate_mbps") or 0)
    maxrate_mbps = int(round(declared * 1.3)) if declared > 0 else default_cap
    if str(export.get("bitrate_cap", "on")).lower() in ("off", "false", "0", "none"):
        maxrate_mbps = 0
    bufsize_mbps = max(2, maxrate_mbps * 2)

    folder_id = str(
        manifest.get("gdrive_folder_id")
        or (manifest.get("cloud_export") or {}).get("target_folder_id")
        or os.environ.get("GDRIVE_FOLDER_ID", "")
    ).strip()

    assets_dir = Path(args.assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    total_content = verify_assets(entries, assets_dir, folder_id, lt_cfg, sentence_pad,
                                  strict=str(manifest.get("manifest_version")) == "8.0")
    if args.preflight:
        print(f"[PREFLIGHT] OK: {len(entries)} сцен, все ассеты найдены.", flush=True)
        return

    project_name = manifest.get("project_name", "output")
    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / f"_build_{project_name}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    SEG_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    budget_min = float(os.environ.get("AETHER_BUDGET_MINUTES", "330"))
    cap_label = f"{maxrate_mbps}M" if maxrate_mbps > 0 else "без потолка"
    est_gb = (maxrate_mbps or 200) * 1e6 / 8 * total_content / 1e9
    print(f"[PLAN] Контента: {total_content / 60:.1f} мин • {w}x{h} {fps}fps "
          f"{preset} crf{crf} • maxrate {cap_label} • ожидание ~{est_gb:.2f} GB • бюджет {budget_min:.0f} мин",
          flush=True)

    t0 = time.monotonic()
    content_done = 0.0
    segments, actual_timings = [], []
    cursor = 0.0
    try:
        for i, entry in enumerate(entries, 1):
            seg_path, dur, actual_voice_start = build_segment(entry, i, assets_dir, folder_id, w, h, fps,
                                          preset, crf, maxrate_mbps, bufsize_mbps, lt_cfg, tmp_dir,
                                          incoming=entries[i-2] if i > 1 else None,
                                          outgoing=entry if i < len(entries) else None,
                                          audio_bitrate=audio_bitrate, sentence_pad=sentence_pad)
            actual_timings.append({
                "scene_index": i,
                "scene_start": round(cursor, 3),
                "voice_start": round(cursor + actual_voice_start, 3),
                "scene_end": round(cursor + dur, 3),
                "duration": round(dur, 3),
            })
            cursor += dur
            content_done += dur
            segments.append(seg_path)

            elapsed = (time.monotonic() - t0) / 60.0
            if i >= 3 and elapsed > 0.5:
                speed = (content_done / 60.0) / elapsed
                finalize_est = max(5.0, (total_content / 60.0) * 0.12)
                projected = elapsed + ((total_content - content_done) / 60.0) / max(speed, 0.01) + finalize_est
                print(f"[BUDGET] готово {content_done / 60:.1f}/{total_content / 60:.1f} мин контента, "
                      f"speed {speed:.2f}x, проекция {projected:.0f} мин", flush=True)
                if projected > budget_min:
                    print(f"""[BUDGET-ABORT] Проекция не влезает в 6-часовой кап GitHub Actions.
  Текущая скорость: {speed:.2f}x при {w}x{h} {preset} crf{crf}.
  Варианты: (а) resolution 2k/1080p; (б) preset ultrafast + crf 23;
  (в) self-hosted runner / NVENC; (г) разбить манифест на два запуска.
  Сегменты сохранены в кэше — после смены настроек прогресс не потеряется.""",
                          file=sys.stderr, flush=True)
                    raise SystemExit(2)

        bad = [p for p in segments if not p.is_file() or not segment_is_valid(p)]
        if bad:
            raise SystemExit(f"[P0-GUARD] Невалидные сегменты для concat: {[str(p) for p in bad]}")

        Path("build").mkdir(exist_ok=True)
        Path("build/actual_timings.json").write_text(
            json.dumps(actual_timings, ensure_ascii=False, indent=2), encoding="utf-8")

        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{p.resolve().as_posix()}'{NL}" for p in segments), encoding="utf-8")

        measured = measure_loudness(concat_list)
        ln = "loudnorm=I=-14:TP=-1:LRA=11"
        if measured:
            ln += (f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
                   f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
                   f":offset={measured['target_offset']}:linear=true")

        final_out = out_dir / f"{project_name}.mp4"
        final_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                     "-map", "0:v", "-map", "0:a", "-c:v", "copy", "-c:a", "aac", "-b:a", f"{audio_bitrate}k",
                     "-ar", "48000", "-ac", "2", "-af", ln, "-movflags", "+faststart", str(final_out)]
        try:
            run(final_cmd)
        except subprocess.CalledProcessError:
            print("[WARN] Двухпроходный loudnorm отклонён FFmpeg — повторяю финал в однопроходном режиме.",
                  flush=True)
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                 "-map", "0:v", "-map", "0:a", "-c:v", "copy", "-c:a", "aac", "-b:a", f"{audio_bitrate}k",
                 "-ar", "48000", "-ac", "2", "-af", "loudnorm=I=-14:TP=-1:LRA=11",
                 "-movflags", "+faststart", str(final_out)])

        total = sum(t["duration"] for t in actual_timings)
        size_mb = round(final_out.stat().st_size / (1024 * 1024), 2)
        print(f"[DONE] {final_out} | {fmt_clock(total)} | {len(segments)} сцен | {size_mb} MB | "
              f"wall {(time.monotonic() - t0) / 60:.1f} мин", flush=True)
        (out_dir / "final_output_name.txt").write_text(final_out.name, encoding="utf-8")

        chapters = []
        for t in actual_timings:
            label = (entries[t["scene_index"] - 1].get("title")
                     or entries[t["scene_index"] - 1].get("name")
                     or f"Scene {t['scene_index']}")
            chapters.append(f"{fmt_clock(t['scene_start'])} {label}")
        (out_dir / "output_chapters.txt").write_text(NL.join(chapters) + NL, encoding="utf-8")

        for lang in ("en", "ru"):
            lines, n = [], 1
            for t in actual_timings:
                subs = entries[t["scene_index"] - 1].get("subtitles") or {}
                text = str(subs.get(lang) or subs.get(lang.upper()) or "").strip()
                if text and t["scene_end"] > t["voice_start"]:
                    lines.append(f"{n}{NL}{fmt_srt(t['voice_start'])} --> {fmt_srt(t['scene_end'])}{NL}{text}{NL}")
                    n += 1
            if lines:
                (out_dir / f"{project_name}_{lang.upper()}.srt").write_text(NL.join(lines), encoding="utf-8")

        card = [f"# Release Card: {manifest.get('video_title', project_name)}", "",
                f"- **Project:** `{project_name}`",
                f"- **Duration:** `{total:.2f}s` (`{fmt_clock(total)}`)",
                f"- **File Size:** `{size_mb} MB`",
                f"- **Resolution:** `{w}x{h} @ {fps}fps`",
                f"- **Bitrate cap:** `{cap_label}`",
                f"- **Scenes:** `{len(segments)}`",
                f"- **Loudness:** `-14 LUFS / -1 dBTP`", "",
                "## Chapters", "", *chapters, ""]
        (out_dir / "RELEASE_CARD.md").write_text(NL.join(card), encoding="utf-8")

        preview = out_dir / f"{project_name}_720p_preview.mp4"
        preview_limit = export.get("preview_duration_sec")
        preview_args = []
        if preview_limit is not None:
            try:
                limit = float(preview_limit)
                if not math.isfinite(limit) or limit < 0:
                    raise ValueError("expected a finite non-negative duration")
                if limit > 0:
                    preview_args = ["-t", str(limit)]
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"[PREVIEW] Invalid preview_duration_sec: {exc}") from exc
        run(["ffmpeg", "-y", "-i", str(final_out)] + preview_args +
            ["-vf", "scale=1280:720", "-c:v", "libx264", "-crf", "24",
             "-preset", "veryfast", "-c:a", "copy", "-movflags", "+faststart", str(preview)])

        qc = {"duration_sec": round(total, 3), "duration_formatted": fmt_clock(total),
              "integrated_lufs": -14.0, "scenes": len(segments),
              "resolution": f"{w}x{h}", "fps": fps, "file_size_mb": size_mb,
              "bitrate_cap_mbps": maxrate_mbps,
              "loudnorm_mode": "two-pass" if measured else "single-pass",
              "encode_speed_x": round((content_done / 60.0) / max((time.monotonic() - t0) / 60.0, 0.01), 3)}
        Path("qc_report.json").write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)  # seg_cache/ НЕ удаляем — это резюм


if __name__ == "__main__":
    main()
