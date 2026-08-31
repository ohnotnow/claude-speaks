#!/usr/bin/env python3
"""Quickly audition Kokoro TTS voices using a shared sample phrase."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TEXT = "The ships hung in the sky in much the same way that bricks don't."
VOICE_RE = re.compile(r"^\s*\d+\.\s+(\S+)\s*$")


def load_settings() -> dict[str, Any]:
    config_path = PROJECT_DIR / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        settings = config.get("provider_settings", {}).get("kokoro", {})
        return settings if isinstance(settings, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def project_path(value: str) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else PROJECT_DIR / path)


def discover_voices(cli: str) -> list[str]:
    try:
        proc = subprocess.run(
            [cli, "--help-voices"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit(f"Could not find {cli!r}. Is kokoro-tts installed and on PATH?") from None

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit status {proc.returncode}"
        raise SystemExit(f"Could not list Kokoro voices: {detail}")

    voices = [match.group(1) for line in proc.stdout.splitlines() if (match := VOICE_RE.match(line))]
    if not voices:
        raise SystemExit("kokoro-tts returned no recognisable voices from --help-voices")
    return voices


def sample_text(args: argparse.Namespace, *, prompt: bool = False) -> str:
    if args.file:
        try:
            text = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"Could not read {args.file}: {exc}") from None
    elif args.text:
        text = " ".join(args.text)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    elif prompt:
        try:
            text = input(f"Sample text [{DEFAULT_TEXT}]: ").strip() or DEFAULT_TEXT
        except EOFError:
            text = DEFAULT_TEXT
    else:
        text = DEFAULT_TEXT

    text = text.strip()
    if not text:
        raise SystemExit("The sample text is empty")
    return text


def select_voices(requested: list[str], available: list[str]) -> list[str]:
    if not requested:
        return available

    unknown = [voice for voice in requested if voice not in available and "," not in voice and ":" not in voice]
    if unknown:
        raise SystemExit(
            f"Unknown voice(s): {', '.join(unknown)}\n"
            "Run with --list to see the installed voices. Voice blends are also accepted."
        )
    return requested


def print_voice_menu(voices: list[str]) -> None:
    print("\nAvailable voices:")
    for number, voice in enumerate(voices, start=1):
        print(f"{number:>3}. {voice}")


def interactive_loop(
    voices: list[str],
    cli: str,
    text_path: Path,
    language: str,
    speed: float,
    model: str,
    voices_file: str,
) -> int:
    print_voice_menu(voices)
    failures = 0
    while True:
        try:
            answer = input("\nVoice number (Enter or q to quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not answer or answer in {"q", "quit", "exit"}:
            break
        try:
            number = int(answer)
        except ValueError:
            print(f"Please enter a number from 1 to {len(voices)}, or q to quit.")
            continue
        if not 1 <= number <= len(voices):
            print(f"Please enter a number from 1 to {len(voices)}.")
            continue
        if not audition(cli, text_path, voices[number - 1], language, speed, model, voices_file):
            failures += 1

    return 1 if failures else 0


def audition(
    cli: str,
    text_path: Path,
    voice: str,
    language: str,
    speed: float,
    model: str,
    voices_file: str,
) -> bool:
    print(f"\n▶ {voice}", flush=True)
    proc = subprocess.run(
        [
            cli,
            str(text_path),
            "--stream",
            "--voice",
            voice,
            "--lang",
            language,
            "--speed",
            str(speed),
            "--model",
            model,
            "--voices",
            voices_file,
        ],
        check=False,
    )
    if proc.returncode != 0:
        print(f"Voice {voice!r} failed (exit status {proc.returncode})", file=sys.stderr)
        return False
    return True


def parser(settings: dict[str, Any]) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Audition kokoro-tts voices with the same short phrase.",
        epilog=(
            "With no arguments, prompts for sample text and then lets you choose voices by number. "
            "Text may also be piped on stdin. Values default to provider_settings.kokoro in config.json."
        ),
    )
    result.add_argument("text", nargs="*", help="sample text (quote it, or pipe text on stdin)")
    result.add_argument("-f", "--file", type=Path, help="read sample text from a UTF-8 file")
    result.add_argument("-v", "--voice", action="append", default=[], help="voice or blend to audition; repeat for several")
    result.add_argument("-a", "--all", action="store_true", help="play every selected voice without prompting")
    result.add_argument("-l", "--list", action="store_true", help="list available voices and exit")
    result.add_argument("--lang", default=settings.get("language", "en-gb"), help="language code")
    result.add_argument("--speed", type=float, default=settings.get("speed", 1.0), help="speech speed")
    result.add_argument("--cli", default=settings.get("cli_path", "kokoro-tts"), help="kokoro-tts executable")
    result.add_argument("--model", default=settings.get("model_path", "kokoro-v1.0.onnx"), help="model path")
    result.add_argument("--voices-file", default=settings.get("voices_path", "voices-v1.0.bin"), help="voices data path")
    return result


def main() -> int:
    settings = load_settings()
    args = parser(settings).parse_args()
    if args.file and args.text:
        raise SystemExit("Use either positional text or --file, not both")

    available = discover_voices(args.cli)
    if args.list:
        print("\n".join(available))
        return 0

    interactive = not args.voice and not args.all and sys.stdin.isatty()
    selected = select_voices(args.voice, available)
    text = sample_text(args, prompt=interactive)
    model = project_path(args.model)
    voices_file = project_path(args.voices_file)

    failures = 0
    with tempfile.TemporaryDirectory(prefix="kokoro-voice-test-") as tmp:
        text_path = Path(tmp) / "sample.txt"
        text_path.write_text(text + "\n", encoding="utf-8")

        print(f"Sample: {text!r}")
        if interactive:
            return interactive_loop(
                available,
                args.cli,
                text_path,
                args.lang,
                args.speed,
                model,
                voices_file,
            )

        for voice in selected:
            if not args.all and len(selected) > 1:
                try:
                    answer = input(f"\nPress Enter to hear {voice}, s to skip, or q to quit: ").strip().lower()
                except EOFError:
                    print("\nNo interactive input available; use --all to play every voice.", file=sys.stderr)
                    return 2
                if answer == "q":
                    break
                if answer == "s":
                    continue

            if not audition(args.cli, text_path, voice, args.lang, args.speed, model, voices_file):
                failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
