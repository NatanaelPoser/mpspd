from __future__ import annotations

import argparse
import asyncio
import html
import json
import mimetypes
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_BASE_URL = "https://images.meupatrocinio.com"
DEFAULT_CONCURRENCY = 50
DEFAULT_INCREMENT = -1
DEFAULT_MAX_RUNTIME_SECONDS = 350 * 60
DEFAULT_CHECKPOINT_INTERVAL = 100
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_REQUEST_DELAY_SECONDS = 0.0
STATE_FILE = "state.json"
FOUND_FILE = "found_links.jsonl"
MANUAL_FILE = "manual_links.txt"
INDEX_FILE = "index.html"
STOP_FILE = "STOP"
MIN_PHOTO_ID = 0
VERSION = "2.2.0"


@dataclass(frozen=True)
class SeedUrl:
    base_url: str
    profile_id: int
    photo_id: int
    photo_number: int

    @property
    def url(self) -> str:
        return build_image_url(
            self.base_url,
            self.profile_id,
            self.photo_id,
            self.photo_number,
        )


@dataclass
class ScanState:
    base_url: str
    profile_id: int
    next_photo_id: int
    next_photo_number: int
    increment: int
    scanned: int = 0
    found: int = 0
    consecutive_errors: int = 0
    last_status: str = "initialized"
    last_probe_status: int | None = None
    last_error: str | None = None
    last_url: str | None = None
    last_found_url: str | None = None
    updated_at: str | None = None
    skipped_photo_numbers: list[int] = field(default_factory=list)
    search_low: int | None = None
    search_high: int | None = None


@dataclass(frozen=True)
class Candidate:
    base_url: str
    profile_id: int
    photo_id: int
    photo_number: int

    @property
    def url(self) -> str:
        return build_image_url(
            self.base_url,
            self.profile_id,
            self.photo_id,
            self.photo_number,
        )


@dataclass(frozen=True)
class FoundRecord:
    url: str
    profile_id: int
    photo_id: int
    photo_number: int
    content_type: str | None
    content_length: int | None
    status: int
    discovered_at: str


@dataclass(frozen=True)
class ProbeResult:
    candidate: Candidate
    status: int | None
    found: bool
    content_type: str | None = None
    content_length: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class AnchorPoint:
    photo_number: int
    photo_id: int


class AnchorIndex:
    def __init__(self, anchors: dict[int, AnchorPoint]):
        self.by_number = anchors
        self.sorted_numbers = sorted(anchors.keys())

    @classmethod
    def from_records(cls, profile_id: int, records: list[FoundRecord]) -> AnchorIndex:
        by_number: dict[int, int] = {}
        for record in records:
            if record.profile_id != profile_id:
                continue
            current = by_number.get(record.photo_number)
            if current is None or record.photo_id > current:
                by_number[record.photo_number] = record.photo_id
        return cls({number: AnchorPoint(number, photo_id) for number, photo_id in by_number.items()})

    def floor_id(self, photo_number: int) -> int | None:
        anchor = self.floor_anchor(photo_number)
        return anchor.photo_id if anchor is not None else None

    def floor_anchor(self, photo_number: int) -> AnchorPoint | None:
        result: AnchorPoint | None = None
        for number in self.sorted_numbers:
            if number >= photo_number:
                break
            result = self.by_number[number]
        return result

    def ceiling_id(self, photo_number: int) -> int | None:
        anchor = self.ceiling_anchor(photo_number)
        return anchor.photo_id if anchor is not None else None

    def ceiling_anchor(self, photo_number: int) -> AnchorPoint | None:
        for number in self.sorted_numbers:
            if number > photo_number:
                return self.by_number[number]
        return None


def load_anchor_index(output_dir: Path, profile_id: int, found_records: list[FoundRecord]) -> AnchorIndex:
    manual_records = load_manual_records(output_dir / MANUAL_FILE)
    return AnchorIndex.from_records(profile_id, manual_records + found_records)


def clamp_photo_id(photo_id: int) -> int:
    return max(MIN_PHOTO_ID, photo_id)


def state_from_seed(seed: SeedUrl, increment: int) -> ScanState:
    return ScanState(
        base_url=seed.base_url,
        profile_id=seed.profile_id,
        next_photo_id=clamp_photo_id(seed.photo_id + increment),
        next_photo_number=seed.photo_number + increment,
        increment=increment,
    )


def skipped_photo_number_set(state: ScanState) -> set[int]:
    return set(state.skipped_photo_numbers)


def mark_photo_number_skipped(state: ScanState, photo_number: int) -> None:
    if photo_number in skipped_photo_number_set(state):
        return
    state.skipped_photo_numbers.append(photo_number)
    state.skipped_photo_numbers.sort()


def mark_exhausted_photo_numbers(
    state: ScanState,
    exhausted_number: int,
    boundary_anchor: AnchorPoint,
    increment: int,
) -> None:
    if increment < 0:
        numbers = range(exhausted_number, boundary_anchor.photo_number, increment)
    else:
        numbers = range(exhausted_number, boundary_anchor.photo_number, increment)
    for photo_number in numbers:
        mark_photo_number_skipped(state, photo_number)


def estimate_photo_id(photo_number: int, anchor_index: AnchorIndex) -> int | None:
    floor_anchor = anchor_index.floor_anchor(photo_number)
    ceiling_anchor = anchor_index.ceiling_anchor(photo_number)
    if floor_anchor is None or ceiling_anchor is None:
        return None
    number_gap = ceiling_anchor.photo_number - floor_anchor.photo_number
    if number_gap <= 0:
        return None
    id_gap = ceiling_anchor.photo_id - floor_anchor.photo_id
    offset = photo_number - floor_anchor.photo_number
    return floor_anchor.photo_id + (id_gap * offset) // number_gap


def search_bounds_for_number(state: ScanState, anchor_index: AnchorIndex) -> tuple[int, int] | None:
    photo_number = state.next_photo_number
    floor_id = anchor_index.floor_id(photo_number)
    ceiling_id = anchor_index.ceiling_id(photo_number)

    if state.increment < 0:
        low = (floor_id + 1) if floor_id is not None else MIN_PHOTO_ID
        high = (ceiling_id - 1) if ceiling_id is not None else None
        if high is None:
            return None
        if high < low:
            return None
        return low, high

    low = (floor_id + 1) if floor_id is not None else MIN_PHOTO_ID
    high = (ceiling_id - 1) if ceiling_id is not None else None
    if high is None:
        return None
    if high < low:
        return None
    return low, high


def reset_search_window(state: ScanState, anchor_index: AnchorIndex) -> None:
    bounds = search_bounds_for_number(state, anchor_index)
    if bounds is None:
        state.search_low = None
        state.search_high = None
        state.next_photo_id = initial_photo_id_for_number(state, anchor_index)
        return

    low, high = bounds
    state.search_low = low
    state.search_high = high
    if low <= state.next_photo_id <= high:
        return
    estimate = estimate_photo_id(state.next_photo_number, anchor_index)
    if estimate is not None:
        state.next_photo_id = clamp_photo_id(min(high, max(low, estimate)))
        return
    state.next_photo_id = clamp_photo_id((low + high) // 2)


def clear_search_window(state: ScanState) -> None:
    state.search_low = None
    state.search_high = None


def advance_after_probe_miss(state: ScanState, probe_id: int, anchor_index: AnchorIndex) -> None:
    if state.search_low is None or state.search_high is None:
        return

    estimate = estimate_photo_id(state.next_photo_number, anchor_index)
    if state.increment < 0:
        if estimate is not None and probe_id > estimate:
            state.search_high = min(state.search_high, probe_id - 1)
        elif estimate is not None and probe_id < estimate:
            state.search_low = max(state.search_low, probe_id + 1)
        else:
            state.search_high = min(state.search_high, probe_id - 1)
    elif estimate is not None and probe_id < estimate:
        state.search_low = max(state.search_low, probe_id + 1)
    elif estimate is not None and probe_id > estimate:
        state.search_high = min(state.search_high, probe_id - 1)
    else:
        state.search_low = max(state.search_low, probe_id + 1)

    if state.search_low > state.search_high:
        return

    state.next_photo_id = clamp_photo_id((state.search_low + state.search_high) // 2)


def is_search_window_exhausted(state: ScanState) -> bool:
    return state.search_low is not None and state.search_high is not None and state.search_low > state.search_high


def initial_photo_id_for_number(state: ScanState, anchor_index: AnchorIndex) -> int:
    photo_number = state.next_photo_number
    if state.increment < 0:
        ceiling = anchor_index.ceiling_id(photo_number)
        if ceiling is not None:
            return clamp_photo_id(ceiling)
        floor = anchor_index.floor_id(photo_number)
        if floor is not None:
            return clamp_photo_id(floor + 1)
        return clamp_photo_id(state.next_photo_id)

    floor = anchor_index.floor_id(photo_number)
    if floor is not None:
        return clamp_photo_id(floor + 1)
    return clamp_photo_id(state.next_photo_id)


def is_photo_number_search_exhausted(state: ScanState, anchor_index: AnchorIndex) -> bool:
    floor = anchor_index.floor_id(state.next_photo_number)
    ceiling = anchor_index.ceiling_id(state.next_photo_number)
    photo_id = clamp_photo_id(state.next_photo_id)

    if state.increment < 0:
        return floor is not None and photo_id <= floor

    return ceiling is not None and photo_id >= ceiling


def advance_after_boundary_exhausted(state: ScanState, anchor_index: AnchorIndex) -> None:
    exhausted_number = state.next_photo_number
    if state.increment < 0:
        floor_anchor = anchor_index.floor_anchor(state.next_photo_number)
        if floor_anchor is not None:
            mark_exhausted_photo_numbers(state, exhausted_number, floor_anchor, state.increment)
            state.next_photo_number = floor_anchor.photo_number + state.increment
            state.next_photo_id = clamp_photo_id(floor_anchor.photo_id + state.increment)
            clear_search_window(state)
            return
    else:
        ceiling_anchor = anchor_index.ceiling_anchor(state.next_photo_number)
        if ceiling_anchor is not None:
            mark_exhausted_photo_numbers(state, exhausted_number, ceiling_anchor, state.increment)
            state.next_photo_number = ceiling_anchor.photo_number + state.increment
            state.next_photo_id = clamp_photo_id(ceiling_anchor.photo_id + state.increment)
            clear_search_window(state)
            return

    mark_photo_number_skipped(state, exhausted_number)
    state.next_photo_number += state.increment
    clear_search_window(state)
    state.next_photo_id = initial_photo_id_for_number(state, anchor_index)


def ensure_scan_position(state: ScanState, anchor_index: AnchorIndex) -> bool:
    while True:
        if state.next_photo_number < 0:
            return False

        while state.next_photo_number in skipped_photo_number_set(state):
            state.next_photo_number += state.increment
            clear_search_window(state)
            if state.next_photo_number < 0:
                return False

        state.next_photo_id = clamp_photo_id(state.next_photo_id)

        if is_photo_number_search_exhausted(state, anchor_index):
            advance_after_boundary_exhausted(state, anchor_index)
            continue

        if state.search_low is None and state.search_high is None:
            reset_search_window(state, anchor_index)

        if state.increment < 0:
            ceiling = anchor_index.ceiling_id(state.next_photo_number)
            if ceiling is not None and state.next_photo_id > ceiling:
                state.next_photo_id = ceiling
                if state.search_high is not None:
                    state.search_high = min(state.search_high, ceiling)
        else:
            floor = anchor_index.floor_id(state.next_photo_number)
            if floor is not None and state.next_photo_id <= floor:
                state.next_photo_id = floor + 1
                if state.search_low is not None:
                    state.search_low = max(state.search_low, floor + 1)

        if is_search_window_exhausted(state):
            advance_after_boundary_exhausted(state, anchor_index)
            continue

        if is_photo_number_search_exhausted(state, anchor_index):
            advance_after_boundary_exhausted(state, anchor_index)
            continue

        floor = anchor_index.floor_id(state.next_photo_number)
        if state.increment < 0 and floor is None and state.next_photo_id <= MIN_PHOTO_ID:
            return False

        return True


def scan_position_status(state: ScanState, anchor_index: AnchorIndex) -> str:
    if state.next_photo_number < 0:
        return "photo_number_exhausted"
    if (
        state.increment < 0
        and state.next_photo_id <= MIN_PHOTO_ID
        and anchor_index.floor_id(state.next_photo_number) is None
    ):
        return "photo_id_floor_reached"
    return "photo_number_exhausted"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_image_url(
    base_url: str,
    profile_id: int,
    photo_id: int,
    photo_number: int,
) -> str:
    return f"{base_url.rstrip('/')}/{profile_id}/{photo_id}/{photo_number}/"


def parse_seed_url(url: str) -> SeedUrl:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parsed.scheme or not parsed.netloc or len(parts) < 3:
        raise ValueError(
            "Expected an image URL like "
            "https://images.meupatrocinio.com/<profile_id>/<photo_id>/<photo_number>/"
        )

    try:
        profile_id = int(parts[0])
        photo_id = int(parts[1])
        photo_number = int(parts[2])
    except ValueError as exc:
        raise ValueError("Seed URL profile_id, photo_id, and photo_number must be integers") from exc

    return SeedUrl(
        base_url=f"{parsed.scheme}://{parsed.netloc}",
        profile_id=profile_id,
        photo_id=photo_id,
        photo_number=photo_number,
    )


def candidate_from_state(state: ScanState) -> Candidate:
    return Candidate(
        base_url=state.base_url,
        profile_id=state.profile_id,
        photo_id=state.next_photo_id,
        photo_number=state.next_photo_number,
    )


def advance_state(state: ScanState, count: int = 1) -> None:
    state.next_photo_id += state.increment * count
    state.next_photo_id = clamp_photo_id(state.next_photo_id)


def advance_after_found(state: ScanState, candidate: Candidate, anchor_index: AnchorIndex) -> None:
    clear_search_window(state)
    state.next_photo_number = candidate.photo_number + state.increment
    adjacent_id = clamp_photo_id(candidate.photo_id + state.increment)
    if candidate.photo_number + state.increment == state.next_photo_number:
        state.next_photo_id = adjacent_id
        return
    reset_search_window(state, anchor_index)


def load_state(path: Path) -> ScanState | None:
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("skipped_photo_numbers", [])
    data.setdefault("search_low", None)
    data.setdefault("search_high", None)
    return ScanState(**data)


def save_state(path: Path, state: ScanState) -> None:
    state.updated_at = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_found_records(path: Path) -> list[FoundRecord]:
    if not path.exists():
        return []

    records: list[FoundRecord] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            records.append(FoundRecord(**json.loads(line)))
    return records


def load_manual_records(path: Path) -> list[FoundRecord]:
    if not path.exists():
        return []

    records: list[FoundRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue

        seed = parse_seed_url(url)
        records.append(
            FoundRecord(
                url=seed.url,
                profile_id=seed.profile_id,
                photo_id=seed.photo_id,
                photo_number=seed.photo_number,
                content_type=None,
                content_length=None,
                status=0,
                discovered_at="manual",
            )
        )
    return records


def append_found_record(path: Path, record: FoundRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(record), sort_keys=True) + "\n")


def found_key(record: FoundRecord) -> tuple[int, int, int]:
    return (record.profile_id, record.photo_id, record.photo_number)


def import_local_images(output_dir: Path, base_url: str | None = None) -> list[FoundRecord]:
    records: list[FoundRecord] = []
    image_pattern = re.compile(r"^(\d+)_(\d+)_(\d+)\.(jpe?g|png|gif|webp)$", re.IGNORECASE)

    for image_path in sorted(Path.cwd().iterdir()):
        if not image_path.is_file():
            continue
        match = image_pattern.match(image_path.name)
        if not match:
            continue

        photo_number = int(match.group(1))
        photo_id = int(match.group(2))
        profile_id = int(match.group(3))
        resolved_base_url = base_url or DEFAULT_BASE_URL
        content_type = mimetypes.guess_type(image_path.name)[0]
        records.append(
            FoundRecord(
                url=build_image_url(resolved_base_url, profile_id, photo_id, photo_number),
                profile_id=profile_id,
                photo_id=photo_id,
                photo_number=photo_number,
                content_type=content_type,
                content_length=image_path.stat().st_size,
                status=200,
                discovered_at=utc_now(),
            )
        )

    if not records:
        return []

    found_path = output_dir / FOUND_FILE
    existing = {found_key(record) for record in load_found_records(found_path)}
    for record in records:
        if found_key(record) not in existing:
            append_found_record(found_path, record)
            existing.add(found_key(record))

    return records


def seed_state_from_local_images(output_dir: Path, increment: int, base_url: str | None = None) -> ScanState | None:
    records = import_local_images(output_dir, base_url=base_url)
    if not records:
        return None

    if increment >= 0:
        latest = max(records, key=lambda item: (item.photo_number, item.photo_id))
    else:
        latest = min(records, key=lambda item: (item.photo_number, item.photo_id))

    return ScanState(
        base_url=base_url or DEFAULT_BASE_URL,
        profile_id=latest.profile_id,
        next_photo_id=latest.photo_id + increment,
        next_photo_number=latest.photo_number + increment,
        increment=increment,
        scanned=0,
        found=len(records),
        last_status="seeded_from_local_images",
        last_url=latest.url,
        last_found_url=latest.url,
    )


def is_image_response(status: int, content_type: str | None) -> bool:
    if not (200 <= status < 300):
        return False
    if not content_type:
        return True
    return content_type.lower().split(";")[0].strip().startswith("image/")


def content_length_from_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def render_index(
    path: Path,
    state: ScanState,
    records: list[FoundRecord],
    stop_enabled: bool = False,
) -> None:
    manual_records = load_manual_records(path.parent / MANUAL_FILE)
    display_records_by_key = {found_key(record): record for record in manual_records}
    display_records_by_key.update({found_key(record): record for record in records})
    display_records = list(display_records_by_key.values())

    rows = []
    for record in sorted(display_records, key=lambda item: (item.photo_number, item.photo_id)):
        rows.append(
            "<tr>"
            f"<td>{record.photo_number}</td>"
            f"<td>{record.photo_id}</td>"
            f"<td>{record.profile_id}</td>"
            f"<td><a href=\"{html.escape(record.url, quote=True)}\">{html.escape(record.url)}</a></td>"
            f"<td>{html.escape(record.content_type or '')}</td>"
            f"<td>{record.content_length or ''}</td>"
            f"<td>{html.escape(record.discovered_at)}</td>"
            "</tr>"
        )

    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MPSPD found links</title>
</head>
<body>
  <h1>MPSPD found links</h1>
  <p>Updated: {html.escape(state.updated_at or utc_now())}</p>
  <p>Status: {html.escape(state.last_status)}; scanned: {state.scanned}; found: {len(records)}; manual: {len(manual_records)}; displayed: {len(display_records)}; skipped numbers: {len(state.skipped_photo_numbers)}; next photo id: {state.next_photo_id}; next photo number: {state.next_photo_number}; stop flag: {"on" if stop_enabled else "off"}</p>
  <p>Raw files: <a href="./{FOUND_FILE}">{FOUND_FILE}</a> <a href="./{MANUAL_FILE}">{MANUAL_FILE}</a> <a href="./{STATE_FILE}">{STATE_FILE}</a></p>
  <table border="1" cellpadding="4" cellspacing="0">
    <thead>
      <tr>
        <th>Photo #</th>
        <th>Photo ID</th>
        <th>Profile ID</th>
        <th>URL</th>
        <th>Type</th>
        <th>Length</th>
        <th>Discovered</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def probe_url(
    session: Any,
    candidate: Candidate,
    timeout_seconds: float,
    retries: int,
) -> ProbeResult:
    for attempt in range(retries + 1):
        try:
            result = await probe_once(session, candidate, timeout_seconds)
            if result.status in {429, 500, 502, 503, 504} and attempt < retries:
                await asyncio.sleep(min(10.0, 0.5 * (2**attempt)))
                continue
            return result
        except Exception as exc:  # noqa: BLE001 - record transient network failures.
            if attempt >= retries:
                return ProbeResult(candidate=candidate, status=None, found=False, error=repr(exc))
            await asyncio.sleep(min(10.0, 0.5 * (2**attempt)))

    return ProbeResult(candidate=candidate, status=None, found=False, error="unknown probe failure")


async def probe_once(session: Any, candidate: Candidate, timeout_seconds: float) -> ProbeResult:
    import aiohttp

    headers = {"User-Agent": f"mpspd/{VERSION}"}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.head(candidate.url, allow_redirects=True, headers=headers, timeout=timeout) as response:
        content_type = response.headers.get("content-type")
        content_length = content_length_from_header(response.headers.get("content-length"))
        if response.status not in {405, 403}:
            return ProbeResult(
                candidate=candidate,
                status=response.status,
                found=is_image_response(response.status, content_type),
                content_type=content_type,
                content_length=content_length,
            )

    get_headers = {**headers, "Range": "bytes=0-0"}
    async with session.get(candidate.url, allow_redirects=True, headers=get_headers, timeout=timeout) as response:
        content_type = response.headers.get("content-type")
        content_length = content_length_from_header(response.headers.get("content-length"))
        response.release()
        return ProbeResult(
            candidate=candidate,
            status=response.status,
            found=is_image_response(response.status, content_type),
            content_type=content_type,
            content_length=content_length,
        )


async def run_scan(args: argparse.Namespace) -> int:
    try:
        import aiohttp
    except ImportError as exc:
        raise SystemExit("Missing dependency: install with `python -m pip install -r requirements.txt`.") from exc

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    found_path = output_dir / FOUND_FILE
    state_path = output_dir / STATE_FILE
    index_path = output_dir / INDEX_FILE
    stop_path = output_dir / STOP_FILE

    state = load_state(state_path)
    if state is None and args.seed_url:
        seed = parse_seed_url(args.seed_url)
        state = state_from_seed(seed, args.increment)
    elif state is None:
        state = seed_state_from_local_images(output_dir, increment=args.increment)

    if state is None:
        raise SystemExit("No state exists and no seed URL/local image fixtures were found.")

    if args.seed_url and args.reset:
        seed = parse_seed_url(args.seed_url)
        state = state_from_seed(seed, args.increment)
        state.skipped_photo_numbers = []
        state.search_low = None
        state.search_high = None

    if args.import_local:
        import_local_images(output_dir, base_url=state.base_url)

    records = load_found_records(found_path)
    state.found = len(records)
    seen = {found_key(record) for record in records}
    anchor_index = load_anchor_index(output_dir, state.profile_id, records)
    ensure_scan_position(state, anchor_index)
    start_time = time.monotonic()
    total_candidates = 0
    checkpoint_counter = 0
    delay_seconds = max(0.0, args.request_delay)
    stop_after_found = args.stop_on_found

    connector = aiohttp.TCPConnector(limit=args.concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= args.max_runtime_seconds:
                state.last_status = "runtime_limit_reached"
                break
            if args.max_candidates and total_candidates >= args.max_candidates:
                state.last_status = "candidate_limit_reached"
                break
            if stop_path.exists():
                state.last_status = "stop_flag_present"
                break

            if args.max_candidates:
                batch_size = min(args.concurrency, args.max_candidates - total_candidates)
            else:
                batch_size = args.concurrency
            if batch_size <= 0:
                state.last_status = "candidate_limit_reached"
                break

            candidates = []
            while len(candidates) < batch_size:
                if not ensure_scan_position(state, anchor_index):
                    state.last_status = scan_position_status(state, anchor_index)
                    break
                binary_active = state.search_low is not None and state.search_high is not None
                if binary_active and candidates:
                    break
                candidates.append(candidate_from_state(state))
                if not binary_active:
                    advance_state(state)
                total_candidates += 1
                if binary_active:
                    break
                if args.max_candidates and total_candidates >= args.max_candidates:
                    break

            if not candidates:
                break

            tasks = [
                asyncio.create_task(
                    probe_url(
                        session,
                        candidate,
                        timeout_seconds=args.timeout,
                        retries=args.retries,
                    )
                )
                for candidate in candidates
            ]
            results = []
            found_in_batch = False
            for task in asyncio.as_completed(tasks):
                result = await task
                results.append(result)
                if result.found:
                    found_in_batch = True
                    for pending_task in tasks:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break

            for result in results:
                state.scanned += 1
                state.last_url = result.candidate.url
                state.last_probe_status = result.status
                state.last_error = result.error

                if result.found:
                    key = (
                        result.candidate.profile_id,
                        result.candidate.photo_id,
                        result.candidate.photo_number,
                    )
                    if key not in seen:
                        record = FoundRecord(
                            url=result.candidate.url,
                            profile_id=result.candidate.profile_id,
                            photo_id=result.candidate.photo_id,
                            photo_number=result.candidate.photo_number,
                            content_type=result.content_type,
                            content_length=result.content_length,
                            status=result.status or 200,
                            discovered_at=utc_now(),
                        )
                        append_found_record(found_path, record)
                        records.append(record)
                        seen.add(key)
                        state.found = len(records)
                        anchor_index = load_anchor_index(output_dir, state.profile_id, records)

                    advance_after_found(state, result.candidate, anchor_index)
                    ensure_scan_position(state, anchor_index)
                    state.last_found_url = result.candidate.url
                    state.consecutive_errors = 0
                    state.last_error = None
                    state.last_status = "found"
                elif result.status in {429, 500, 502, 503, 504} or result.error:
                    state.consecutive_errors += 1
                    state.last_status = f"transient_error:{result.status or result.error}"
                else:
                    state.last_status = f"miss:{result.status}"
                    advance_after_probe_miss(state, result.candidate.photo_id, anchor_index)
                    if is_search_window_exhausted(state):
                        advance_after_boundary_exhausted(state, anchor_index)
                        ensure_scan_position(state, anchor_index)

            if found_in_batch:
                state.last_status = "found"

            if state.consecutive_errors >= args.backoff_after:
                delay_seconds = min(args.max_backoff, max(1.0, delay_seconds * 2 or 1.0))
                state.consecutive_errors = 0
            elif delay_seconds > args.request_delay:
                delay_seconds = max(args.request_delay, delay_seconds / 2)

            checkpoint_counter += len(results)
            if checkpoint_counter >= args.checkpoint_interval:
                save_state(state_path, state)
                render_index(index_path, state, records, stop_enabled=stop_path.exists())
                checkpoint_counter = 0

            if found_in_batch and stop_after_found:
                save_state(state_path, state)
                render_index(index_path, state, records, stop_enabled=stop_path.exists())
                break

            if delay_seconds:
                await asyncio.sleep(delay_seconds)

    state.found = len(records)
    save_state(state_path, state)
    render_index(index_path, state, records, stop_enabled=stop_path.exists())
    print(
        f"Done: {state.last_status}; scanned={state.scanned}; "
        f"found={len(records)}; next={state.next_photo_id}/{state.next_photo_number}"
    )
    return 0


def run_init(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / STATE_FILE
    index_path = output_dir / INDEX_FILE

    if args.seed_url:
        seed = parse_seed_url(args.seed_url)
        state = state_from_seed(seed, args.increment)
    else:
        state = seed_state_from_local_images(output_dir, increment=args.increment)
        if state is None:
            raise SystemExit("No seed URL provided and no local image fixtures were found.")

    save_state(state_path, state)
    records = load_found_records(output_dir / FOUND_FILE)
    render_index(index_path, state, records, stop_enabled=(output_dir / STOP_FILE).exists())
    print(f"Initialized {state_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable MPSPD image-link scanner.")
    parser.add_argument("--version", action="version", version=f"mpspd {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create initial state/output files.")
    init_parser.add_argument("--seed-url")
    init_parser.add_argument("--increment", type=int, default=DEFAULT_INCREMENT)
    init_parser.add_argument("--output-dir", default="public")
    init_parser.set_defaults(func=run_init)

    scan_parser = subparsers.add_parser("scan", help="Run a bounded resumable scan.")
    scan_parser.add_argument("--seed-url")
    scan_parser.add_argument("--increment", type=int, default=DEFAULT_INCREMENT)
    scan_parser.add_argument("--output-dir", default="public")
    scan_parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    scan_parser.add_argument("--max-runtime-seconds", type=int, default=DEFAULT_MAX_RUNTIME_SECONDS)
    scan_parser.add_argument("--max-candidates", type=int, default=0)
    scan_parser.add_argument("--checkpoint-interval", type=int, default=DEFAULT_CHECKPOINT_INTERVAL)
    scan_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    scan_parser.add_argument("--retries", type=int, default=2)
    scan_parser.add_argument("--backoff-after", type=int, default=20)
    scan_parser.add_argument("--max-backoff", type=float, default=30.0)
    scan_parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    scan_parser.add_argument("--reset", action="store_true")
    scan_parser.add_argument("--import-local", action="store_true")
    scan_parser.add_argument("--stop-on-found", action=argparse.BooleanOptionalAction, default=True)
    scan_parser.set_defaults(func=lambda args: asyncio.run(run_scan(args)))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
