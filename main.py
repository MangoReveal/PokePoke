"""
Who's That Pokemon? - video creation service (Google Cloud Run).

The static site sends a handful of small files plus timing settings; this
service composites them onto the bundled background video with ffmpeg and
returns a finished MP4. No screen recording involved - ffmpeg decodes the
source video once and encodes the final result once.

Designed to be cheap to run:
  * one encode per instance at a time (Cloud Run --concurrency 1)
  * hard caps on upload size, audio length, bitrate and runtime
  * overlay PNGs are decoded once, not once per frame
  * quality-based (CRF) encoding with a bitrate ceiling, so simple frames
    cost fewer bytes (less CPU time AND less download bandwidth)
  * nothing user-supplied is ever passed to ffmpeg except validated PNG/WAV
    files and numbers this file formats itself
"""

import io
import logging
import math
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # total upload cap

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video-service")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STANDARD_BG = os.path.join(BASE_DIR, "Mango", "background.mp4")
HD_BG = os.path.join(BASE_DIR, "Mango", "backgroundHD.mp4")

OUT_W, OUT_H = 1920, 1080
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "150"))
MAX_AUDIO_SECONDS = 6.0

# Comma-separated list of sites allowed to call this service, e.g.
#   ALLOWED_ORIGINS="https://yourname.github.io"
# "*" (the default) allows any site - fine for testing, tighten it later.
ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------
# CORS (hand-rolled: avoids an extra dependency)
# --------------------------------------------------------------------------

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")

    if "*" in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    return response


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

class BadRequest(Exception):
    pass


def clamp(value, low, high):
    return max(low, min(high, value))


def form_float(name, default, low, high):
    raw = request.form.get(name, None)

    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        value = float(default)

    if math.isnan(value):
        value = float(default)

    return clamp(value, low, high)


def form_bool(name, default=False):
    raw = request.form.get(name)

    if raw is None:
        return default

    return raw.strip().lower() in ("1", "true", "yes", "on")


def fmt(value):
    """Format a number for use inside an ffmpeg filter expression."""
    return f"{float(value):.3f}"


def run(cmd, timeout):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@lru_cache(maxsize=4)
def probe(path):
    """Duration, fps and whether the file has audio - cached per file."""
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type,r_frame_rate",
            "-of", "json",
            path,
        ],
        timeout=20,
    )

    if result.returncode != 0:
        raise RuntimeError("Could not read the background video.")

    import json

    data = json.loads(result.stdout)
    duration = float(data["format"]["duration"])
    has_audio = any(
        s.get("codec_type") == "audio" for s in data.get("streams", [])
    )

    fps = 30.0

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            num, _, den = str(stream.get("r_frame_rate", "30/1")).partition("/")

            try:
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                fps = 30.0

            break

    return {"duration": duration, "has_audio": has_audio, "fps": fps}


def save_upload(field, directory, filename, magic_check, required=True):
    file = request.files.get(field)

    if file is None:
        if required:
            raise BadRequest(f"Missing file: {field}")

        return None

    data = file.read()

    if not data:
        if required:
            raise BadRequest(f"Empty file: {field}")

        return None

    if not magic_check(data):
        raise BadRequest(f"{field} is not a valid file of the expected type.")

    path = os.path.join(directory, filename)

    with open(path, "wb") as handle:
        handle.write(data)

    return path


def is_png(data):
    return data[:8] == PNG_MAGIC


def is_wav(data):
    return data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def wav_seconds(path):
    result = run(
        [
            "ffprobe", "-v", "error",
            "-protocol_whitelist", "file",
            "-f", "wav",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        timeout=15,
    )

    try:
        return float(result.stdout.strip())
    except ValueError:
        raise BadRequest("The sound file could not be read.")


def remap_time(t, snip_on, snip_start, snip_end):
    """
    Timings are set against the ORIGINAL video. If a section is cut out,
    everything after the cut moves earlier by the cut's length.
    """
    if not snip_on:
        return t

    removed = snip_end - snip_start

    if t <= snip_start:
        return t

    if t >= snip_end:
        return max(snip_start, t - removed)

    return snip_start  # landed inside the cut: happens right at the cut


# --------------------------------------------------------------------------
# ffmpeg command construction
# --------------------------------------------------------------------------

def build_command(p):
    """
    p: dict of already-validated parameters (see create_video()).
    Returns the full ffmpeg argument list.
    """
    src = probe(p["bg_path"])
    duration = src["duration"]
    src_fps = src["fps"]

    snip_on = p["snip_on"]
    s0, s1 = p["snip_start"], p["snip_end"]

    parts = []
    scale = f"scale={OUT_W}:{OUT_H}:flags=bilinear"

    # If the output frame rate is LOWER than the source, drop frames first so
    # every later step (scale, sharpen, overlays, encode) does less work.
    early_fps = p["fps"] < src_fps - 0.5
    late_fps = p["fps"] > src_fps + 0.5

    prep = [f"fps={p['fps']}"] if early_fps else []

    # ---- background video (optionally with a section cut out) -----------
    head = snip_on and s0 > 0.05
    tail = snip_on and s1 < duration - 0.05

    if snip_on and not head and not tail:
        raise BadRequest("The cut removes the entire video.")

    if snip_on and head and tail:
        parts.append(
            "[0:v]split=2[vs0][vs1];"
            f"[vs0]trim=start=0:end={fmt(s0)},setpts=PTS-STARTPTS[vh];"
            f"[vs1]trim=start={fmt(s1)},setpts=PTS-STARTPTS[vt];"
            "[vh][vt]concat=n=2:v=1:a=0[vjoined]"
        )
        vlabel = "[vjoined]"
        out_duration = duration - (s1 - s0)
    elif snip_on and tail:
        parts.append(
            f"[0:v]trim=start={fmt(s1)},setpts=PTS-STARTPTS[vjoined]"
        )
        vlabel = "[vjoined]"
        out_duration = duration - s1
    elif snip_on and head:
        parts.append(
            f"[0:v]trim=start=0:end={fmt(s0)},setpts=PTS-STARTPTS[vjoined]"
        )
        vlabel = "[vjoined]"
        out_duration = s0
    else:
        vlabel = "[0:v]"
        out_duration = duration

    chain = prep + [scale]

    if p["sharpen"]:
        chain.append("unsharp=3:3:0.7:3:3:0.0")

    parts.append(f"{vlabel}{','.join(chain)}[bg]")

    # ---- overlays (each PNG is a full 1920x1080 transparent frame) --------
    t_blank0 = remap_time(p["blank_start"], snip_on, s0, s1)
    t_blank1 = remap_time(p["blank_end"], snip_on, s0, s1)
    t_reveal = remap_time(p["revealed_start"], snip_on, s0, s1)
    t_sound = remap_time(p["sound_start"], snip_on, s0, s1)

    # Input numbering: 0 = video, 1 = blank, 2 = revealed, [3 = caption], audio
    parts.append(
        f"[bg][1:v]overlay=0:0:enable="
        f"'between(t,{fmt(t_blank0)},{fmt(t_blank1)})*lt(t,{fmt(t_reveal)})'[o1]"
    )
    parts.append(
        f"[o1][2:v]overlay=0:0:enable='gte(t,{fmt(t_reveal)})'[o2]"
    )

    last = "[o2]"
    audio_index = 3

    if p["caption_path"]:
        parts.append(
            f"[o2][3:v]overlay=0:0:enable='gte(t,{fmt(t_reveal)})'[o3]"
        )
        last = "[o3]"
        audio_index = 4

    parts.append(
        f"{last}{'fps=' + str(p['fps']) if late_fps else 'null'}[vout]"
    )

    # ---- audio ----------------------------------------------------------
    delay_ms = int(round(t_sound * 1000))

    parts.append(
        f"[{audio_index}:a]volume={fmt(p['cry_volume'])},"
        f"adelay=delays={delay_ms}:all=1[cry]"
    )

    if src["has_audio"]:
        if snip_on and head and tail:
            parts.append(
                "[0:a]asplit=2[as0][as1];"
                f"[as0]atrim=start=0:end={fmt(s0)},asetpts=PTS-STARTPTS[ah];"
                f"[as1]atrim=start={fmt(s1)},asetpts=PTS-STARTPTS[at];"
                "[ah][at]concat=n=2:v=0:a=1[bga]"
            )
        elif snip_on and tail:
            parts.append(
                f"[0:a]atrim=start={fmt(s1)},asetpts=PTS-STARTPTS[bga]"
            )
        elif snip_on and head:
            parts.append(
                f"[0:a]atrim=start=0:end={fmt(s0)},asetpts=PTS-STARTPTS[bga]"
            )
        else:
            parts.append("[0:a]anull[bga]")

        parts.append(
            f"[bga]volume={fmt(p['bg_volume'])}[bgv];"
            "[bgv][cry]amix=inputs=2:duration=first:"
            "dropout_transition=0:normalize=0,"
            "alimiter=limit=0.97[aout]"
        )
    else:
        # Background has no sound of its own: just the cry, padded with
        # silence so the audio track spans the whole video.
        parts.append("[cry]apad,alimiter=limit=0.97[aout]")

    cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file", "-i", p["bg_path"],
        "-protocol_whitelist", "file", "-f", "image2", "-i", p["blank_path"],
        "-protocol_whitelist", "file", "-f", "image2", "-i", p["revealed_path"],
    ]

    if p["caption_path"]:
        cmd += [
            "-protocol_whitelist", "file", "-f", "image2",
            "-i", p["caption_path"],
        ]

    cmd += [
        "-protocol_whitelist", "file", "-f", "wav", "-i", p["audio_path"],
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        # Hard stop at the expected length: a safety net so a bad filter
        # graph can never turn into a runaway (expensive) encode.
        "-t", fmt(out_duration + 0.05),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "animation",
        "-crf", str(p["crf"]),
        "-maxrate", f"{p['max_mbps']}M",
        "-bufsize", f"{p['max_mbps'] * 2}M",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        p["output_path"],
    ]

    return cmd


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def health():
    return jsonify({"ok": True, "service": "whos-that-pokemon-video"})


@app.route("/create-video", methods=["POST", "OPTIONS"])
def create_video():
    if request.method == "OPTIONS":
        return Response(status=204)

    work = tempfile.mkdtemp(prefix="reveal_")

    try:
        blank_path = save_upload("blank_overlay", work, "blank.png", is_png)
        revealed_path = save_upload(
            "revealed_overlay", work, "revealed.png", is_png
        )
        caption_path = save_upload(
            "caption_overlay", work, "caption.png", is_png, required=False
        )
        audio_path = save_upload("cry_audio", work, "cry.wav", is_wav)

        if wav_seconds(audio_path) > MAX_AUDIO_SECONDS:
            raise BadRequest(
                f"Sound is too long (max {MAX_AUDIO_SECONDS:g} seconds)."
            )

        use_hd = form_bool("use_hd_background") and os.path.exists(HD_BG)
        bg_path = HD_BG if use_hd else STANDARD_BG
        duration = probe(bg_path)["duration"]

        snip_on = form_bool("snip_enabled")
        snip_start = form_float("snip_start", 0, 0, duration)
        snip_end = form_float("snip_end", 0, 0, duration)

        if snip_end <= snip_start:
            snip_on = False

        max_mbps = int(clamp(round(form_float("max_mbps", 4, 1, 8)), 1, 8))

        params = {
            "bg_path": bg_path,
            "blank_path": blank_path,
            "revealed_path": revealed_path,
            "caption_path": caption_path,
            "audio_path": audio_path,
            "output_path": os.path.join(work, "output.mp4"),
            "blank_start": form_float("blank_start", 0.88, 0, duration),
            "blank_end": form_float("blank_end", 9.58, 0, duration),
            "revealed_start": form_float("revealed_start", 9.59, 0, duration),
            "sound_start": form_float("sound_start", 9.89, 0, duration),
            "snip_on": snip_on,
            "snip_start": snip_start,
            "snip_end": snip_end,
            "fps": int(clamp(round(form_float("frame_rate", 30, 10, 60)), 10, 60)),
            "max_mbps": max_mbps,
            "crf": 24,
            "sharpen": form_bool("sharpen_enabled"),
            "bg_volume": form_float("bg_volume", 1, 0, 1.5),
            "cry_volume": form_float("cry_volume", 0.5, 0, 1.5),
        }

        cmd = build_command(params)
        result = run(cmd, timeout=FFMPEG_TIMEOUT)

        if result.returncode != 0:
            log.error("ffmpeg failed: %s", result.stderr[-2000:])
            return jsonify({"error": "Video creation failed."}), 500

        with open(params["output_path"], "rb") as handle:
            payload = handle.read()

        return send_file(
            io.BytesIO(payload),
            mimetype="video/mp4",
            as_attachment=True,
            download_name="reveal.mp4",
        )

    except BadRequest as error:
        return jsonify({"error": str(error)}), 400
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out")
        return jsonify({"error": "Video creation took too long."}), 504
    except Exception:  # noqa: BLE001 - never leak internals to the client
        log.exception("Unexpected error")
        return jsonify({"error": "Video creation failed."}), 500
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "Upload is too large."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
