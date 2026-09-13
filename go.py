"""
go.py - A* pathfinding bot for Minescript v5.0

Usage (from in-game chat):
    # goto X Y Z
    # goto BLOCK_ID
    # mine BLOCK_ID [MAX_COUNT]
    # follow ENTITY_TYPE
    # wander [RADIUS] [LEGS]
    # waypoints add/new NAME [X] [Y] [Z]   (alias: # wp)
    # waypoints to/goto NAME
    # waypoints remove/rm/delete NAME
    # waypoints list
    # waypoints clear
    # settings list
    # settings get SETTING
    # settings set SETTING VALUE
    # settings add SETTING VALUE
    # settings remove SETTING VALUE
    # settings unset SETTING
    # settings clear SETTING
    # settings toggle SETTING

Running "#" with no arguments starts a background daemon instead: it
listens for chat messages sent with a leading "#" (intercepted before
they're actually sent to chat, so nothing shows up publicly) and runs
them as the commands above on a background thread, e.g. "#goto 100 64
100" or "#mine iron_ore 32". "#eta" reports a rough time-of-arrival
estimate for whatever's currently running. Only one task runs at a
time; by default, typing another "#..." command while one is in flight
is refused, but setting "allow_queueing_tasks" to true makes it queue
up (FIFO) and run once the current task (and anything queued ahead of
it) finishes instead. "#stop" cancels whatever's currently running
*and* clears anything still queued, unwinding cleanly instead of
leaving movement/attack keys held down.

Walks the local player to a set of coordinates, to the nearest block
matching BLOCK_ID (e.g. "diamond_ore" or "minecraft:diamond_ore"), or
repeatedly mines through a structure made of BLOCK_ID (up to MAX_COUNT
instances, default 64). Targets are found by gradually scanning outward
from the player using the same lazy terrain cache the pathfinder itself
uses. Uses A* search over walkable blocks, then simulates forward/jump/
sprint key presses and full 3D look-at rotation to follow the path.

All tunables (penalties, weight, reach, tile size, avoid lists, sprint/
mining toggles, landing precision, thread count, cache size/expiration,
etc.) live in a per-install preferences file at minescript/pf/pf_pref.json,
editable in-game via "# settings ...". See DEFAULT_SETTINGS below for the
full list and defaults.

Terrain is additionally cached to disk (minescript/pf/cache/<world
address + spawn point>.tilecache) so repeat runs against the same world
don't have to re-fetch tiles that were already scanned in a previous
session. This is pure client-side bookkeeping - no Java library / mod
is involved, only the standard Minescript Python API
(get_block_region / getblock).

Notes:
- Terrain is read lazily in small batched tiles via get_block_region()
  as the search frontier actually reaches them, instead of prefetching
  the entire start/goal bounding box up front.
- "Walkable" = an air/passable block with a solid block underneath,
  and enough headroom (2 blocks) for the player.
- Diagonal moves are allowed but corner-cutting through solid blocks
  is blocked.
- Supports single-block-up "step" moves (auto-jump) but not multi-block
  parkour.
- Search uses a weighted heuristic (settings: "weight") to cut down
  node expansions; paths are very-near-optimal rather than strictly
  shortest.
- Breakable obstacles can be mined through as a fallback when no walking
  route exists in a given direction, cost-penalized by the "mine_penalty"
  setting. "# mine BLOCK_ID" specifically rewards mining that one
  block type so it'll actively tunnel through a structure made of it.
- Orientation uses player_look_at, giving the bot a full, continuous 360
  degree yaw *and* pitch aim.
"""

import sys
import os
import time
import heapq
import itertools
import math
import random
import re
import json
import zlib
import pickle
import array
import concurrent.futures
import threading
import shlex
from collections import deque
from copy import deepcopy

import system.lib.minescript as minescript
from system.lib.minescript import (
    get_block_region,
    player_position,
    player_look_at,
    player_press_forward,
    player_press_sprint,
    player_press_jump,
    player_press_attack,
    player_inventory_select_slot,
    player_press_use,
    echo,
)


_TAG = "\u00a78[\u00a77go\u00a78]\u00a7r "


def go_echo(body: str, error: bool = False):
    color = "\u00a7c" if error else "\u00a7f"
    echo(f"{_TAG}{color}{body}")


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs:
        return f"{hrs}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"

# ---------------------------------------------------------------------------
# Chat message formatting
# ---------------------------------------------------------------------------
#
# Every message the bot prints goes through go_echo() so they all share one
# consistent look:
#
#     §8[§7go§8] §f<message, with §7 highlights on the interesting bits>
#
# e.g. "§8[§7go§8] §fSearched §712345 §fnodes. §8(§78200 §8nodes/s§8)"
#      "§8[§7go§8] §fFound path to §712, 64, -8"
#
# `body` is plain text/f-string content - go_echo() adds the "[go]" tag and
# the base §f (white) message color for you; drop in "\u00a77...\u00a7f"
# around any values (coordinates, counts, block/entity names, etc.) you
# want to stand out in gray against the rest of the white text. Pass
# error=True for failures, which tags the message §c (red) instead of §f.

# ---------------------------------------------------------------------------
# Task progress tracking (for "#eta")
# ---------------------------------------------------------------------------
#
# A tiny bit of shared state that the currently-running background task
# (see _run_task()) keeps updated as it makes progress, so "#eta" - itself
# handled inline on the daemon's event loop thread, same as "#stop" - can
# report a rough time-of-arrival estimate without having to talk to the
# task thread directly. "fraction" is a 0.0-1.0 rough completion estimate;
# it's deliberately approximate (see each call site), so the ETA derived
# from it is a rough guess, not a promise.

_progress_lock = threading.Lock()
_progress = {"task": None, "start_time": None, "fraction": 0.0, "detail": ""}


def _progress_begin(task_name: str):
    with _progress_lock:
        _progress.update(task=task_name, start_time=time.time(), fraction=0.0, detail="")


def _progress_set(fraction: float, detail: str = ""):
    with _progress_lock:
        if _progress["task"] is not None:
            _progress["fraction"] = max(0.0, min(1.0, fraction))
            _progress["detail"] = detail


def _progress_end():
    with _progress_lock:
        _progress.update(task=None, start_time=None, fraction=0.0, detail="")


def handle_eta():
    with _progress_lock:
        snapshot = dict(_progress)

    if snapshot["task"] is None:
        go_echo("No task is currently running.")
        return

    elapsed = time.time() - snapshot["start_time"]
    fraction = snapshot["fraction"]
    detail = snapshot["detail"]
    pct = f"{fraction * 100:.0f}%"

    body = f"Task \u00a77{snapshot['task']}\u00a7f - \u00a77{pct}\u00a7f done"
    if detail:
        body += f" \u00a78(\u00a77{detail}\u00a78)"
    body += f", elapsed \u00a77{_format_duration(elapsed)}"

    # A fraction near zero (just started, or a task like "follow"
    # that never reports real progress) would make remaining = elapsed *
    # (1/fraction - 1) blow up into a meaningless number, so only quote an
    # ETA once there's enough progress for the extrapolation to mean
    # anything.
    if fraction >= 0.02:
        remaining = elapsed * (1.0 - fraction) / fraction
        body += f"\u00a7f - ETA ~\u00a77{_format_duration(remaining)}"
    else:
        body += "\u00a7f - not enough progress yet for an ETA"

    go_echo(body)


# ---------------------------------------------------------------------------
# Settings / preferences
# ---------------------------------------------------------------------------
#
# Everything below is configurable in-game via "\go settings ..." and
# persisted to minescript/pf/pf_pref.json. DEFAULT_SETTINGS below is only
# the fallback used the first time the script runs (or for any key
# missing from an existing prefs file) - the live values always come from
# the SETTINGS dict, populated by load_settings().

PF_DIR = os.path.join("minescript", "pf")
PF_PREF_PATH = os.path.join(PF_DIR, "pf_pref.json")
PF_CACHE_DIR = os.path.join(PF_DIR, "cache")
PF_WAYPOINTS_PATH = os.path.join(PF_DIR, "waypoints.json")
PF_BLOCKITEMS_PATH = os.path.join(PF_DIR, "blockitems.json")
BLOCKITEMS_URL = "https://raw.githubusercontent.com/SkylerHg3GH/go-pathfinder/refs/heads/main/resources/blockitems.json"

TILE_SIZE = 12

# Floor for "threads_for_searching" - never go below this many worker
# threads, even if the setting is misconfigured or os.cpu_count() can't
# tell how many cores are available.
MIN_SEARCH_THREADS = 2
# Upper bound so a typo like 100000 doesn't try to spawn 100k threads.
MAX_SEARCH_THREADS = 32

# Weight > 1.0 makes the search greedier (fewer nodes expanded, faster),
# at the cost of paths that can be very slightly longer than optimal.
# 1.0 = classic admissible A*. Default for the "weight" setting below.
HEURISTIC_WEIGHT = 1.1

# Default for the "mine_penalty" setting below. Walking a
# single block costs ~1.0, so this makes mining a strongly last-resort
# option — the search will happily take a detour worth several extra steps
# rather than break a block, but will still mine through if that's truly
# the only (or a drastically shorter) way.
MINE_PENALTY_PER_BLOCK = 6.0

# Default for the "reach" setting below (player reach in blocks used by
# "# mine" in-reach checks). Kept as a named constant so the default is
# documented in one place.
REACH_BLOCKS = 4

# ---------------------------------------------------------------------------
# Background task control
# ---------------------------------------------------------------------------
#
# Commands typed as "#..." in chat (see run_chat_daemon() at the bottom of
# this file) run on a background thread instead of blocking the chat box.
# Only one such task is allowed to run at a time; "#stop" sets _stop_event,
# which every long-running loop below (pathing, mining, following,
# exploring) polls so it can unwind cleanly instead of just being killed
# mid-swing.

_stop_event = threading.Event()
_task_thread = None

# FIFO queue of pending "#..." commands (each a list of args), used when
# "allow_queueing_tasks" is enabled - see run_chat_daemon() / _run_task_chain()
# below. Only touched under _task_queue_lock since the daemon's event loop
# and the background task thread both read/write it.
_task_queue = deque()
_task_queue_lock = threading.Lock()


class TaskStopped(Exception):
    """Raised internally to unwind a running task after '#stop'."""


def _check_stop():
    if _stop_event.is_set():
        raise TaskStopped()


# ---------------------------------------------------------------------------
# Terrain helpers
# ---------------------------------------------------------------------------
#
# NOTE: the actual PASSABLE / _PASSABLE_SUBSTRINGS / _is_passable_block /
# is_breakable / NON_STANDABLE_FLOOR definitions live further down, right
# before TilePersistentCache. This used to be a byte-for-byte duplicate of
# all of them sitting here too - dead code, silently shadowed by the real
# ones below (Python just re-binds the names), that only wasted space and
# invited someone to edit the wrong copy. Removed instead of kept in sync
# by hand.

DEFAULT_SETTINGS = {
    "avoid_stepping_on": [],
    "avoid_mining": [],
    "jump_penalty": 0.0,
    "walk_penalty": 0.0,
    "mine_penalty": MINE_PENALTY_PER_BLOCK,
    "weight": HEURISTIC_WEIGHT,
    "turn_penalty": 0.4,
    "tile_scan_size": TILE_SIZE,
    "reach": REACH_BLOCKS,
    "allow_breaking_blocks": True,
    "allow_sprinting": True,
    "allow_queueing_tasks": False,
    "threads_for_searching": max(MIN_SEARCH_THREADS, os.cpu_count() or 8),
    "passables": [],
    "passable_block_substrings": [],
    "max_cache_size": 1000000,
    "cache_expiration_duration": 7,
    "precise_landing": False,
    "waypoint_tolerance": 0.6,
    "per_block_timeout": 10.0,
    "allow_placing_blocks": True,
}

# Setting "types", used to validate/parse "\go settings set/add/..." input.
LIST_SETTINGS = {
    "avoid_stepping_on", "avoid_mining", "passables", "passable_block_substrings",
}
BOOL_SETTINGS = {"allow_breaking_blocks", "allow_sprinting", "allow_queueing_tasks", "precise_landing", "allow_placing_blocks"}
# Settings whose list *values* are block IDs and should be normalized
# (lowercased, given a "minecraft:" prefix if missing).
BLOCK_ID_LIST_SETTINGS = {"avoid_stepping_on", "avoid_mining", "passables"}

def _copy_setting_value(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _fresh_settings(base):
    return {k: _copy_setting_value(v) for k, v in base.items()}


SETTINGS = _fresh_settings(DEFAULT_SETTINGS)

# Cost charged instead of "mine_penalty" when the block being mined is the
# one "# mine BLOCK_ID" was asked to target. Kept as a small fixed
# fraction of a normal walking step (cheaper than walking, so the search
# actively seeks the target) rather than a user setting, since it only
# matters relative to mine_penalty (which *is* configurable).
MINE_REWARD_COST = 0.5


def _as_bool(value) -> bool:
    """Lenient bool coercion for prefs-file values. Plain bool() would treat
    the string "false" as True (non-empty), so handle common spellings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _settings_changed():
    """Call after any in-game settings mutation so the per-search caches
    (passability memo + _SEARCH_SETTINGS_CACHE) can't serve stale values."""
    try:
        _refresh_search_settings_cache()
    except NameError:
        pass  # defined further down; find_path() refreshes anyway


def ensure_dirs():
    os.makedirs(PF_DIR, exist_ok=True)
    os.makedirs(PF_CACHE_DIR, exist_ok=True)


def _get_world_spawn():
    """Returns the world's spawn point as an (x, y, z) int tuple, or None
    if it can't be determined. Shared by the terrain tile cache and
    waypoints storage so both key their per-world data the same way."""
    try:
        info = minescript.world_info()
        spawn = getattr(info, "spawn", None)
        if spawn is None:
            return None
        try:
            sx, sy, sz = spawn.x, spawn.y, spawn.z
        except AttributeError:
            sx, sy, sz = spawn[0], spawn[1], spawn[2]
        return (int(sx), int(sy), int(sz))
    except Exception:
        return None


def _world_key(spawn=None) -> str:
    """Filesystem/JSON-key-safe identifier for "this world" - server
    address plus spawn point - the same identity TilePersistentCache uses
    to key the terrain tile cache, reused here so waypoints.json keeps a
    separate set of waypoints per world too. Doesn't include the terrain
    fingerprint sampling TilePersistentCache additionally does on top of
    this, since a stale/misattributed waypoint list is low-stakes
    compared to pathfinding against the wrong cached terrain."""
    address = "unknown"
    try:
        info = minescript.world_info()
        address = getattr(info, "address", None) or "unknown"
    except Exception:
        pass
    if spawn is None:
        spawn = _get_world_spawn()
    spawn_part = f"_spawn{spawn[0]}_{spawn[1]}_{spawn[2]}" if spawn else ""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{address}{spawn_part}").strip("_") or "unknown"


def load_settings():
    """Loads minescript/pf/pf_pref.json, creating the folder structure and
    a defaults file if this is the first run (or the file is missing/
    corrupt). Any keys missing from an on-disk file are backfilled from
    DEFAULT_SETTINGS so upgrading the script never crashes on a stale
    prefs file."""
    global SETTINGS
    ensure_dirs()
    loaded = {}
    if os.path.exists(PF_PREF_PATH):
        try:
            with open(PF_PREF_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                loaded = {}
        except Exception:
            loaded = {}
    merged = _fresh_settings(DEFAULT_SETTINGS)
    for key, value in loaded.items():
        if key not in DEFAULT_SETTINGS:
            continue  # drop unknown/stale keys instead of persisting them forever
        default = DEFAULT_SETTINGS[key]
        if isinstance(default, list):
            merged[key] = list(value) if isinstance(value, list) else _copy_setting_value(default)
        elif isinstance(default, bool):
            merged[key] = _as_bool(value) if not isinstance(value, bool) else value
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                merged[key] = int(value)
            except (TypeError, ValueError):
                merged[key] = default
        elif isinstance(default, float):
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                merged[key] = default
        else:
            merged[key] = value
    SETTINGS = merged
    save_settings()
    return SETTINGS


def save_settings():
    ensure_dirs()
    try:
        with open(PF_PREF_PATH, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2, sort_keys=True)
    except Exception as e:
        go_echo(f"failed to save settings: {e}", error=True)


# ---------------------------------------------------------------------------
# Terrain helpers
# ---------------------------------------------------------------------------

# Blocks the player can stand inside of / walk through.
# Per-block-string memo for _is_passable_block(). get_block() already
# caches the block *string* per position, but that still left the actual
# passability computation - PASSABLE membership plus, for anything not in
# that set, a scan through ~24 substrings - re-running on every single
# call even though only a small vocabulary of distinct block strings
# (stone, dirt, air, water, ...) actually shows up across a search. Keyed
# on the block string itself, so it pays off the moment the same block id
# is seen twice anywhere, not just at the same position. Depends on the
# "passables"/"passable_block_substrings" settings, so it's cleared
# alongside the settings snapshot in _refresh_search_settings_cache().
_PASSABLE_BLOCK_CACHE = {}


PASSABLE = {
    "minecraft:air",
    "minecraft:cave_air",
    "minecraft:void_air",
    "minecraft:short_grass",
    "minecraft:grass",
    "minecraft:tall_grass",
    "minecraft:fern",
    "minecraft:large_fern",
    "minecraft:snow",
    "minecraft:water",  # comment out if you don't want the bot swimming
    "minecraft:bubble_column",
    "minecraft:lily_pad",
    "minecraft:seagrass",
    "minecraft:tall_seagrass",
    "minecraft:kelp",
    "minecraft:kelp_plant",
    "minecraft:cobweb",
    "minecraft:structure_void",
    "minecraft:light",
    "minecraft:hanging_roots",
    "minecraft:glow_lichen",
    "minecraft:small_dripleaf",
    # common flowers/decorative plants (not caught by the substring rules below)
    "minecraft:dandelion", "minecraft:poppy", "minecraft:blue_orchid",
    "minecraft:allium", "minecraft:azure_bluet", "minecraft:red_tulip",
    "minecraft:orange_tulip", "minecraft:white_tulip", "minecraft:pink_tulip",
    "minecraft:oxeye_daisy", "minecraft:cornflower", "minecraft:lily_of_the_valley",
    "minecraft:torchflower", "minecraft:wither_rose", "minecraft:sunflower",
    "minecraft:lilac", "minecraft:rose_bush", "minecraft:peony",
    "minecraft:pitcher_plant", "minecraft:bamboo_sapling",
}

# Substrings that mark a block as non-solid / walk-through even though it's
# not (and can't practically be) enumerated exhaustively above.
_PASSABLE_SUBSTRINGS = (
    "vine", "sapling", "propagule", "torch", "carpet", "pressure_plate",
    "button", "lever", "rail", "redstone_wire", "sign", "banner",
    "wheat", "carrots", "potatoes", "beetroots", "sugar_cane", "nether_wart",
    "sweet_berry_bush", "moss_carpet", "coral_fan", "chorus_flower",
    "flower_pot", "candle_cake",
    # Slabs and turtle eggs are short enough (0.5 and ~0.44 blocks tall,
    # respectively) that the bot can walk through/over the airspace they
    # occupy without needing to mine. Orientation (top vs. bottom slab)
    # isn't known from the block ID alone, so this is a deliberate
    # approximation - same tradeoff already made for "carpet".
    #
    # Stairs are deliberately NOT in this list (unlike before). A stair's
    # collision is effectively a full block (two stacked half-height
    # steps), not one uniform half-height slab, so treating "stairs" as
    # passable here let the pathfinder think there was full headroom
    # above/through a stair block when there wasn't - it would try to
    # step into or jump through a spot a real stair actually obstructs.
    # Stairs are now handled as full/solid blocks for passability; they
    # are still explicitly recognized as valid standable floor in
    # is_solid_floor() below, so the bot can still walk on top of one -
    # it just no longer treats the space a stair occupies as open air.
    "slab", "turtle_egg",
)


def _is_passable_block(block: str) -> bool:
    cached = _PASSABLE_BLOCK_CACHE.get(block)
    if cached is not None:
        return cached
    result = _compute_passable_block(block)
    _PASSABLE_BLOCK_CACHE[block] = result
    return result


def _compute_passable_block(block: str) -> bool:
    if block in PASSABLE:
        return True
    if block in _cached_setting("passables", ()):
        return True
    extra_subs = _cached_setting("passable_block_substrings") or ()
    if any(s and s in block for s in extra_subs):
        return True

    # "sign" is passable in general (signs and ceiling-hung hanging signs
    # have no real collision box - you walk straight through the board,
    # and the chains are a negligible 3px). *Wall* hanging signs are the
    # one exception: per the wiki their mounting bracket is a genuinely
    # solid, collidable part of the model (unlike the sign board itself),
    # and it can sit at head height. Treating "wall_hanging_sign" as
    # freely passable was the actual bug behind "can't handle hanging
    # signs" - the bot would walk straight at the bracket, bounce off it
    # every tick, and never make progress. Fall through to the mining
    # path instead, same as any other small solid obstruction.
    if "wall_hanging_sign" in block:
        return False

    return any(s in block for s in _PASSABLE_SUBSTRINGS)


# Blocks whose ID contains "carpet". Carpet is 1px thin and does NOT carry
# its own floor height/collision - per the wiki, it just inherits whatever
# is underneath it. That means "is there carpet at y-1" tells you nothing
# about whether y-1 is actually stand-on-able; you have to look through it
# to what it's resting on. This also covers the "double carpet" case
# (carpet placed directly on carpet), which the game explicitly does NOT
# let mobs/players path across normally - it has to be treated as if it
# were the (non-solid) block underneath, not as solid ground.
def _is_carpet_block(block: str) -> bool:
    return "carpet" in block


# Blocks that can't reasonably be "mined through" by a bot: truly
# indestructible blocks, world-machinery blocks that shouldn't be touched,
# and portals (breaking a portal block doesn't clear an opening anyway).
UNBREAKABLE = {
    "minecraft:bedrock", "minecraft:barrier", "minecraft:command_block",
    "minecraft:chain_command_block", "minecraft:repeating_command_block",
    "minecraft:structure_block", "minecraft:structure_void", "minecraft:jigsaw",
    "minecraft:end_portal_frame", "minecraft:end_portal", "minecraft:end_gateway",
    "minecraft:nether_portal", "minecraft:light", "minecraft:moving_piston",
    "minecraft:reinforced_deepslate", "minecraft:spawner",
}

# Liquids aren't "mined" (no drop, nothing to break) - the bot should just
# swim through them (already PASSABLE) or path around, never attack them.
LIQUIDS = {"minecraft:water", "minecraft:lava"}


def is_breakable(block: str, mine_reward_block: str = None) -> bool:
    """Whether `block` is a solid obstacle the bot is allowed to mine
    through, as opposed to something already walkable, a liquid, a truly
    unbreakable/should-not-touch block, or something the user has put in
    "avoid_mining" (unless it's specifically the block "# mine" was
    asked to target, which always overrides avoid_mining)."""
    if _is_passable_block(block):
        return False
    if block in UNBREAKABLE or block in LIQUIDS:
        return False
    if block == "minecraft:air":
        return False
    if block != mine_reward_block and not _cached_setting("allow_breaking_blocks", True):
        return False
    avoid_mining = _cached_setting("avoid_mining") or ()
    if block in avoid_mining and block != mine_reward_block:
        return False
    return True


# ---------------------------------------------------------------------------
# Placeable-block data (minescript/pf/blockitems.json) + hotbar scanning +
# obstruction-climbing block placement
# ---------------------------------------------------------------------------
#
# blockitems.json maps Minecraft version string -> list of "minecraft:x"
# item/block IDs that exist as placeable items in that version. Fetched
# once from BLOCKITEMS_URL and cached to disk; later runs just read the
# cached copy. Re-delete the file (or bump BLOCKITEMS_URL) to force a
# fresh pull.

_BLOCKITEMS_CACHE = None          # whole parsed JSON (all versions)
_PLACEABLE_FOR_VERSION = {}       # mc_version -> set() of placeable ids


def ensure_blockitems() -> dict:
    """Loads minescript/pf/blockitems.json, fetching it from
    BLOCKITEMS_URL first if it isn't already on disk. Cached in-process
    in _BLOCKITEMS_CACHE so repeat calls in the same run don't re-read
    the file. Returns {} (and echoes an error) if neither the local file
    nor the fetch works - callers should treat that as "nothing is
    placeable" rather than crashing the task."""
    global _BLOCKITEMS_CACHE
    if _BLOCKITEMS_CACHE is not None:
        return _BLOCKITEMS_CACHE

    ensure_dirs()

    if not os.path.exists(PF_BLOCKITEMS_PATH):
        try:
            import urllib.request
            with urllib.request.urlopen(BLOCKITEMS_URL, timeout=15) as resp:
                data = resp.read()
            with open(PF_BLOCKITEMS_PATH, "wb") as f:
                f.write(data)
        except Exception as e:
            go_echo(f"Failed to fetch blockitems.json: {e}", error=True)
            _BLOCKITEMS_CACHE = {}
            return _BLOCKITEMS_CACHE

    try:
        with open(PF_BLOCKITEMS_PATH, "r", encoding="utf-8") as f:
            _BLOCKITEMS_CACHE = json.load(f)
        if not isinstance(_BLOCKITEMS_CACHE, dict):
            _BLOCKITEMS_CACHE = {}
    except Exception as e:
        go_echo(f"Failed to parse blockitems.json: {e}", error=True)
        _BLOCKITEMS_CACHE = {}

    return _BLOCKITEMS_CACHE


def get_placeable_blocks_for_version() -> set:
    """The set of placeable block IDs for the game's current Minecraft
    version (minescript.version_info().minecraft used as the lookup key
    into blockitems.json). Cached in _PLACEABLE_FOR_VERSION after first
    call. Empty set if the version isn't a key in blockitems.json."""
    global _PLACEABLE_FOR_VERSION
    try:
        mc_version = minescript.version_info().minecraft
    except Exception:
        mc_version = "unknown"
    if mc_version in _PLACEABLE_FOR_VERSION:
        return _PLACEABLE_FOR_VERSION[mc_version]

    blockitems = ensure_blockitems()
    items = blockitems.get(mc_version, [])
    placeable = set(items)
    _PLACEABLE_FOR_VERSION[mc_version] = placeable

    if not placeable:
        go_echo(f"No blockitems entry for Minecraft \u00a77{mc_version}\u00a7c - "
                "block placement disabled", error=True)

    return placeable


def _entry_count(entry) -> int:
    """How many items are in this player_inventory() entry. Entries
    expose `.count`; missing/odd values mean 0 so empty/ghost slots
    are never treated as placeable."""
    try:
        count = getattr(entry, "count", 0)
        count = int(count)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def get_hotbar_slots() -> list:
    """Scans minescript.player_inventory() for entries whose 'slot'
    attribute is 0-8 (the hotbar) and returns them as a list of
    (slot, item_id, count) tuples, sorted by slot. Each inventory
    entry's '.item' attribute is the block/item ID string (e.g.
    "minecraft:cobblestone") and '.count' is how many of it are in
    that slot. Slots with no item or a count of 0 are skipped."""
    hotbar = []
    for entry in minescript.player_inventory():
        try:
            slot = int(getattr(entry, "slot", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= slot <= 8):
            continue
        item = getattr(entry, "item", None)
        if not item:
            continue
        count = _entry_count(entry)
        if count <= 0:
            continue
        hotbar.append((slot, item, count))
    hotbar.sort(key=lambda t: t[0])
    return hotbar


def count_placeable_blocks(placeable: set) -> int:
    """Total placeable block count across the whole hotbar (sum of
    `.count` for every hotbar slot whose item is in `placeable`)."""
    total = 0
    for _slot, item, count in get_hotbar_slots():
        if item in placeable:
            total += count
    return total


def find_placeable_hotbar_slot(placeable: set):
    """Returns (slot, item_id, count) for the hotbar slot (0-8) with
    the largest remaining `.count` whose .item is a member of
    `placeable` (normally get_placeable_blocks_for_version()'s
    result), or None if nothing in the hotbar can be placed as a
    block. Biggest stack first so a tower doesn't burn through many
    small stacks and re-select every block."""
    best = None
    for slot, item, count in get_hotbar_slots():
        if item in placeable:
            if best is None or count > best[2]:
                best = (slot, item, count)
    return best


def place_block_tower(height: int, terrain: "TerrainCache" = None) -> bool:
    """Places up to `height` blocks straight up beneath the player's feet
    (a "pillar jump" tower) to climb an obstruction the pathfinder can't
    otherwise get up. Does nothing and returns False if
    "allow_placing_blocks" is turned off (see the setting below), or if
    no hotbar slot (0-8) holds a placeable block.

    For each block: selects the hotbar slot holding a placeable item
    with player_inventory_select_slot(), looks straight down at the
    player's own feet (a plain pillar-up must aim at the block space
    directly below - aiming forward misplaces the block), jumps, and
    presses use while airborne to place the block underneath - the
    standard "pillar/tower up" trick. Re-checks the hotbar (including
    each stack's `.count`) every iteration so an emptied stack is
    never selected again mid-tower.

    The previously-selected hotbar slot is restored afterwards, and any
    `terrain` cache passed in is invalidated for the blocks just placed
    so later queries see them instead of stale cached air.

    Refuses up front when the hotbar doesn't hold enough blocks for
    the full `height` instead of building a short stump anyway.

    Toggle: "# settings toggle allow_placing_blocks" (or
    "set allow_placing_blocks true/false") turns this off entirely,
    independent of "allow_breaking_blocks"."""
    if not _as_bool(SETTINGS.get("allow_placing_blocks", True)):
        return False
    if height <= 0:
        return True

    placeable = get_placeable_blocks_for_version()
    if not placeable:
        return False

    # NOTE: the selected hotbar slot is left on the placed block type -
    # minescript exposes no getter for the previously selected slot, so
    # there is nothing to restore.
    available = count_placeable_blocks(placeable)
    if available < height:
        go_echo(f"Not enough blocks to place "
                f"\u00a78(\u00a77have {available}\u00a78, need \u00a77{height}\u00a78)\u00a7f - skipping placement",
                error=True)
        return False

    placed = 0
    for _ in range(height):
        _check_stop()

        found = find_placeable_hotbar_slot(placeable)
        if found is None:
            go_echo("No placeable block left in hotbar - stopping tower", error=True)
            break
        slot, item, _count = found

        minescript.player_inventory_select_slot(slot)

        px, py, pz = player_position()
        # Always look straight down at our own feet: looking at an
        # arbitrary forward target aims at the wall in front instead
        # of the block space directly below, so the pillar lands in
        # the wrong spot (or not at all). Two blocks down gives a
        # clean straight-down pitch even when x/z match exactly.
        player_look_at(px, py - 2.0, pz)
        time.sleep(0.05)

        player_press_jump(True)
        time.sleep(0.15)
        player_press_use(True)
        time.sleep(0.1)
        player_press_use(False)
        player_press_jump(False)
        time.sleep(0.25)

        placed += 1
        if terrain is not None:
            try:
                bx, by, bz = int(math.floor(px)), int(math.floor(py)) - 1, int(math.floor(pz))
                terrain.invalidate_block(bx, by, bz)
            except Exception:
                pass
        go_echo(f"Placed \u00a77{item}\u00a7f (\u00a77{placed}/{height}\u00a7f)")

    return placed >= height


# Blocks that never count as a solid "floor" to stand on (e.g. would let
# you fall through, or are dangerous).
NON_STANDABLE_FLOOR = {
    "minecraft:air",
    "minecraft:cave_air",
    "minecraft:void_air",
    "minecraft:lava",
    "minecraft:fire",
    "minecraft:magma_block",
}


# ---------------------------------------------------------------------------
# Persistent (disk) tile cache
# ---------------------------------------------------------------------------

class TilePersistentCache:
    """Caches fetched terrain tiles to disk at
    minescript/pf/cache/<world address + spawn point>.tilecache so a
    later run against the same world doesn't have to re-fetch tiles it
    already scanned. Keyed on the world's spawn point in addition to the
    server address, since two different worlds on the same server
    (e.g. different singleplayer saves proxied through the same address,
    or a server that resets/regenerates its map) usually have different
    spawn points - relying on address alone would otherwise mix their
    caches together.

    Since some servers reuse the exact same spawn point for multiple
    distinct worlds (map rotations, minigame lobbies, a regenerated
    save), the cache file is additionally validated on load by sampling
    a handful of blocks around spawn and comparing them against the
    signature stored in the file; a mismatch means this is a different
    world than the one the cache was built for, and the cache is
    discarded instead of served.

    Governed by two settings:
      - cache_expiration_duration: age (in days) after which a cached
        tile is considered stale and re-fetched from the live world.
        0 disables caching entirely; -1 means tiles never expire.
      - max_cache_size: soft cap (in bytes) on the on-disk cache file;
        oldest tiles are evicted first when the cap would be exceeded.

    Storage format: each unique block-id string is interned into a
    shared string table, and each tile is stored as a flat array of
    2-byte string-table indices (covers up to 65536 distinct block
    strings, far more than any real world uses) plus a timestamp. The
    whole thing is pickled and zlib-compressed on disk to keep the file
    as small as practical without pulling in extra dependencies.
    """

    # Small ring of positions around spawn sampled to fingerprint a world.
    # Kept deliberately close to spawn (within a few blocks) so the sample
    # is cheap and still valid even right after logging in, before much
    # of anything else has loaded.
    _FINGERPRINT_OFFSETS = (
        (0, -1, 0), (1, -1, 0), (-1, -1, 0), (0, -1, 1), (0, -1, -1),
        (3, -1, 3), (-3, -1, -3), (0, 2, 0),
    )

    def __init__(self):
        duration = SETTINGS.get("cache_expiration_duration", 7)
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 7
        self.enabled = duration != 0
        self.permanent = duration < 0
        self.expiration_seconds = None if self.permanent else duration * 86400.0
        try:
            self.max_size = max(0, int(SETTINGS.get("max_cache_size", 1000000)))
        except (TypeError, ValueError):
            self.max_size = 1000000

        self.spawn = _get_world_spawn()
        self.fingerprint = self._compute_fingerprint(self.spawn)
        self.path = self._resolve_path()
        self.strings = []
        self._string_index = {}
        # key -> (timestamp, min_pos, (sx, sy, sz), raw_bytes).
        # Older cache files store (timestamp, size, raw_bytes) with 'H'
        # arrays and no min_pos; _decode()/get() still read those.
        self.tiles = {}
        self._dirty = False
        if self.enabled:
            self._load()

    def _compute_fingerprint(self, spawn):
        """Samples a handful of blocks around the world's spawn point and
        returns them as a signature tuple. Some servers (minigame lobbies,
        map rotations, a save that got reset/regenerated) reuse the exact
        same address *and* spawn coordinates for what is, terrain-wise, a
        completely different world. Address + spawn alone can't tell those
        apart, but the actual blocks sitting around spawn almost always
        will, so this doubles as a cheap way to catch "this cache file's
        name matches, but it's not actually the world it was built for."
        """
        if spawn is None:
            return None
        sx, sy, sz = spawn
        sample = []
        for dx, dy, dz in self._FINGERPRINT_OFFSETS:
            try:
                b = minescript.getblock(sx + dx, sy + dy, sz + dz)
            except Exception:
                b = None
            sample.append(b)
        return tuple(sample)

    def _resolve_path(self) -> str:
        return os.path.join(PF_CACHE_DIR, f"{_world_key(self.spawn)}.tilecache")

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
            payload = pickle.loads(zlib.decompress(raw))

            stored_fingerprint = payload.get("fingerprint")
            if (stored_fingerprint is not None and self.fingerprint is not None
                    and stored_fingerprint != self.fingerprint):
                # Same cache filename (address + spawn), but the blocks
                # actually sitting around spawn right now don't match what
                # was sampled when this file was written - this is a
                # different world reusing the same spawn point, not the
                # world this cache belongs to. Discard it rather than risk
                # silently pathfinding against another world's terrain.
                go_echo("cached terrain for this spawn looks like it "
                        "belongs to a different world (server reuses this "
                        "spawn point) - starting a fresh terrain cache")
                self.strings = []
                self._string_index = {}
                self.tiles = {}
                self._dirty = True  # rewrite with the correct fingerprint
                return

            self.strings = list(payload.get("strings", []))
            self._string_index = {s: i for i, s in enumerate(self.strings)}
            self.tiles = dict(payload.get("tiles", {}))
        except Exception:
            self.strings = []
            self._string_index = {}
            self.tiles = {}

    def _intern(self, s: str) -> int:
        idx = self._string_index.get(s)
        if idx is None:
            idx = len(self.strings)
            self.strings.append(s)
            self._string_index[s] = idx
        return idx

    def _serialize(self) -> bytes:
        payload = {
            "strings": self.strings,
            "tiles": self.tiles,
            "fingerprint": self.fingerprint,
        }
        return zlib.compress(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL), 9)

    def flush(self):
        if not self.enabled or not self._dirty:
            return
        try:
            blob = self._serialize()
            # Evict oldest tiles first if we're over the soft size cap.
            # Drop ~1/8 of the entries per pass so a big overshoot doesn't
            # re-pickle+compress once per single evicted tile (O(n^2)).
            while self.max_size > 0 and len(blob) > self.max_size and self.tiles:
                ordered = sorted(self.tiles, key=lambda k: self.tiles[k][0])
                evict = max(1, len(ordered) // 8)
                for k in ordered[:evict]:
                    del self.tiles[k]
                blob = self._serialize()
            ensure_dirs()
            with open(self.path, "wb") as f:
                f.write(blob)
            self._dirty = False
        except Exception:
            pass  # cache is a best-effort speedup, never fatal

    def is_stale(self, timestamp: float) -> bool:
        if self.permanent:
            return False
        return (time.time() - timestamp) > self.expiration_seconds

    def get(self, key):
        """Returns (blocks_dict, stale) for a cached tile, or None."""
        if not self.enabled:
            return None
        entry = self.tiles.get(key)
        if entry is None:
            return None
        if len(entry) == 4:
            ts, min_pos, size, raw = entry
            blocks = self._decode(key, tuple(size), raw, tuple(min_pos))
        else:  # legacy 3-tuple entry: (timestamp, size, raw)
            ts, size, raw = entry
            blocks = self._decode(key, tuple(size), raw)
        return blocks, self.is_stale(ts)

    def put(self, key, min_pos, size, blocks: dict):
        if not self.enabled:
            return
        sx, sy, sz = size
        arr = array.array("I")
        for x in range(min_pos[0], min_pos[0] + sx):
            for y in range(min_pos[1], min_pos[1] + sy):
                for z in range(min_pos[2], min_pos[2] + sz):
                    block = blocks.get((x, y, z), "minecraft:air")
                    arr.append(self._intern(block))
        self.tiles[key] = (time.time(), tuple(min_pos), tuple(size), arr.tobytes())
        self._dirty = True

    def _decode(self, key, size, raw, min_pos=None) -> dict:
        if min_pos is None:
            min_pos = self._min_pos_for_key(key, size)
        sx, sy, sz = size
        expected = sx * sy * sz
        arr = array.array("I")
        # Backward compat: tiles written before min_pos/'I' used 2-byte 'H'
        # arrays with no stored min_pos.
        if len(raw) == expected * 2:
            arr = array.array("H")
        arr.frombytes(raw)
        blocks = {}
        idx = 0
        n_strings = len(self.strings)
        n_cells = len(arr)
        for x in range(min_pos[0], min_pos[0] + sx):
            for y in range(min_pos[1], min_pos[1] + sy):
                for z in range(min_pos[2], min_pos[2] + sz):
                    if idx >= n_cells:
                        blocks[(x, y, z)] = "minecraft:air"
                    else:
                        sid = arr[idx]
                        blocks[(x, y, z)] = self.strings[sid] if sid < n_strings else "minecraft:air"
                    idx += 1
        return blocks

    @staticmethod
    def _min_pos_for_key(key, size):
        # Legacy fallback for cache entries written before min_pos was
        # stored. Only correct for full-size tiles; y-clamped edge tiles
        # from those old files may decode at a shifted y and simply miss
        # (treated as a cache miss on rewrite). New entries always carry
        # their real min_pos so this is never used for them.
        tx, ty, tz = key
        sx, sy, sz = size
        return (tx * sx, ty * sy, tz * sz)


class TerrainCache:
    """Lazy, tile-batched terrain reader.

    Fetches small fixed-size cubes ("tiles") on demand, one
    get_block_region() round trip per tile (unless a still-fresh copy is
    sitting in the on-disk TilePersistentCache), the first time a query
    lands in that tile. As A* expands its frontier outward the cache
    gradually pulls in only the tiles the search actually touches.

    Falls back to per-block getblock() only if a tile fetch ever fails
    (e.g. an unloaded chunk at the world edge), so it never hard-fails.
    """

    def __init__(self, tile_size: int = None, y_min: int = -64, y_max: int = 319):
        try:
            tile_size = int(tile_size or SETTINGS.get("tile_scan_size", TILE_SIZE))
        except (TypeError, ValueError):
            tile_size = TILE_SIZE
        self.tile_size = max(1, tile_size)
        self.y_min = y_min
        self.y_max = y_max
        self._tiles = {}          # (tx, ty, tz) -> {(x,y,z): block_str}
        self._walkable_cache = {}
        self._floor_cache = {}
        self._block_cache = {}
        self.tiles_fetched = 0
        self.tiles_from_disk_cache = 0
        self.persist = TilePersistentCache()
        self._executor = None
        # Guards _tiles/_block_cache/persist string-table updates: _warm_tiles
        # calls _get_tile from a ThreadPoolExecutor, so without this two
        # threads can interleave fetches of the same tile (duplicate work at
        # best, a corrupted intern table at worst).
        self._lock = threading.RLock()

    def _executor_pool(self):
        if self._executor is None:
            workers = max(MIN_SEARCH_THREADS, int(SETTINGS.get("threads_for_searching", 8) or MIN_SEARCH_THREADS))
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        return self._executor

    def close(self):
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        self.persist.flush()

    def _tile_coord(self, v: int) -> int:
        return math.floor(v / self.tile_size)

    def _tile_bounds(self, tx: int, ty: int, tz: int):
        s = self.tile_size
        min_pos = (tx * s, max(ty * s, self.y_min), tz * s)
        max_pos = (
            tx * s + s - 1,
            min(ty * s + s - 1, self.y_max),
            tz * s + s - 1,
        )
        return min_pos, max_pos

    def _tile_out_of_range(self, min_pos, max_pos) -> bool:
        # A tile sitting entirely outside the world height range (e.g. a
        # goal/estimate above y_max) has an inverted y range. There is no
        # live tile to fetch there; callers fall back to per-block reads.
        return min_pos[1] > max_pos[1]

    def _fetch_live_tile(self, key, min_pos, max_pos):
        """Fetches one tile from the live world and returns a plain
        {(x,y,z): block_str} dict, or None on failure."""
        try:
            region = get_block_region(min_pos, max_pos, safety_limit=False)
        except Exception:
            return None
        self.tiles_fetched += 1
        blocks = {}
        for x in range(min_pos[0], max_pos[0] + 1):
            for y in range(min_pos[1], max_pos[1] + 1):
                for z in range(min_pos[2], max_pos[2] + 1):
                    b = region.get_block(x, y, z)
                    blocks[(x, y, z)] = (b or "minecraft:air").split("[", 1)[0]
        return blocks

    def _get_tile(self, tx: int, ty: int, tz: int):
        key = (tx, ty, tz)
        # Lock-free fast path: dict.get is GIL-atomic, and the hot search
        # loop hits already-fetched tiles almost every time.
        tile = self._tiles.get(key, "missing")
        if tile != "missing":
            return tile
        with self._lock:
            tile = self._tiles.get(key, "missing")
            if tile != "missing":
                return tile
            min_pos, max_pos = self._tile_bounds(tx, ty, tz)
            if self._tile_out_of_range(min_pos, max_pos):
                return None  # don't cache: nothing meaningful to remember
            size = (max_pos[0] - min_pos[0] + 1, max_pos[1] - min_pos[1] + 1, max_pos[2] - min_pos[2] + 1)
            cached = self.persist.get(key)
            if cached is not None:
                blocks, stale = cached
                if not stale:
                    self.tiles_from_disk_cache += 1
                    self._tiles[key] = blocks
                    return blocks
                stale_blocks = blocks
            else:
                stale_blocks = None
        # Live fetch (world round-trip) runs WITHOUT the lock so parallel
        # _warm_tiles workers don't serialize on it. Re-acquire below to
        # publish, re-checking first in case another thread filled the tile.
        fresh = self._fetch_live_tile(key, min_pos, max_pos)
        with self._lock:
            tile = self._tiles.get(key, "missing")
            if tile != "missing":
                return tile
            if fresh is not None:
                self.persist.put(key, min_pos, size, fresh)
                self._tiles[key] = fresh
                return fresh
            if stale_blocks is not None:
                self._tiles[key] = stale_blocks
                return stale_blocks
            return None  # don't cache failures; the chunk may load later

    def prefetch_positions(self, positions):
        """Best-effort parallel warm-up of the tiles covering `positions`,
        using up to "threads_for_searching" worker threads. Pure
        optimization - correctness never depends on this having run."""
        needed = set()
        for (x, y, z) in positions:
            key = (self._tile_coord(x), self._tile_coord(y), self._tile_coord(z))
            if self._tiles.get(key, "missing") == "missing":
                needed.add(key)
        if not needed:
            return
        self._warm_tiles(needed)

    def prefetch_bbox(self, min_pos, max_pos):
        """Best-effort parallel warm-up of every tile that overlaps the
        axis-aligned box [min_pos, max_pos] (inclusive). Same effect as
        prefetch_positions() over every point in that box, but computed
        directly from the tile grid instead of enumerating each candidate
        (x, y, z) individually and re-deriving its tile coordinate one at
        a time - cheap even when the box covers many blocks, since the
        number of tiles touched is normally 1-8 regardless of box size."""
        x0, y0, z0 = min_pos
        x1, y1, z1 = max_pos
        tx0, tx1 = sorted((self._tile_coord(x0), self._tile_coord(x1)))
        ty0, ty1 = sorted((self._tile_coord(y0), self._tile_coord(y1)))
        tz0, tz1 = sorted((self._tile_coord(z0), self._tile_coord(z1)))

        needed = set()
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                for tz in range(tz0, tz1 + 1):
                    key = (tx, ty, tz)
                    if self._tiles.get(key, "missing") == "missing":
                        needed.add(key)
        if not needed:
            return
        self._warm_tiles(needed)

    def _warm_tiles(self, needed):
        max_workers = max(MIN_SEARCH_THREADS, int(SETTINGS.get("threads_for_searching", 8) or MIN_SEARCH_THREADS))
        if max_workers <= 1 or len(needed) <= 1:
            for k in needed:
                self._get_tile(*k)
            return
        pool = self._executor_pool()
        futures = [pool.submit(self._get_tile, *k) for k in needed]
        concurrent.futures.wait(futures)

    def get_block(self, x: int, y: int, z: int) -> str:
        key = (x, y, z)
        cached = self._block_cache.get(key)
        if cached is not None:
            return cached
        tile = self._get_tile(self._tile_coord(x), self._tile_coord(y), self._tile_coord(z))
        block = None
        if tile is not None:
            block = tile.get((x, y, z))
        if block is None:
            try:
                block = minescript.getblock(x, y, z)
            except Exception:
                block = "minecraft:air"
        block = (block or "minecraft:air").split("[", 1)[0]
        with self._lock:
            self._block_cache[key] = block
        return block

    def is_passable(self, x: int, y: int, z: int) -> bool:
        return _is_passable_block(self.get_block(x, y, z))

    def is_solid_floor(self, x: int, y: int, z: int, _depth: int = 0) -> bool:
        """Can the player actually stand ON TOP of the block at (x, y, z)?

        This used to just blacklist a handful of obviously-bad floors
        (air, lava, fire, magma) and call literally everything else
        "solid" - which meant carpet, signs, vines, torches, buttons,
        pressure plates, rails, wheat, saplings, banners, redstone wire
        etc. all silently counted as valid ground to stand on, because
        none of them happened to be in that tiny blacklist. Most of
        those have no real floor collision at all (a torch or a hanging
        sign board can't support you), so the bot would compute a path
        across them, then physically fall through / get stuck on arrival
        - which is exactly the "can't handle carpets or hanging signs"
        symptom. Flipped to a solidity check instead: a block only
        counts as floor if it's NOT one of the known-passable/thin
        blocks (i.e. it's an actual full/solid obstruction).

        Memoized per-position: is_walkable() already asks this for the
        block beneath every walkable check, and the mining fallback in
        _try_mine_move() independently asks it again for that same
        position right after - unlike get_block()'s string-level cache,
        nothing previously cached the actual floor verdict, so both call
        sites were re-running this (recursive, for carpet stacks) check
        from scratch. Cached by (x, y, z) only, ignoring `_depth` - the
        answer for a given position doesn't depend on which recursion
        level asked for it, only on the terrain itself.
        """
        if _depth == 0:
            cached = self._floor_cache.get((x, y, z))
            if cached is not None:
                return cached
            result = self._compute_solid_floor(x, y, z, _depth)
            self._floor_cache[(x, y, z)] = result
            return result
        return self._compute_solid_floor(x, y, z, _depth)

    def _compute_solid_floor(self, x: int, y: int, z: int, _depth: int = 0) -> bool:
        block = self.get_block(x, y, z)
        if block in NON_STANDABLE_FLOOR:
            return False
        avoid = _cached_setting("avoid_stepping_on") or ()
        if block in avoid:
            return False

        # Slabs/turtle eggs are the one category of "passable" block
        # that's actually genuinely solid and self-supporting at its own
        # height (a bottom slab has real collision and needs nothing
        # underneath it) - that's the existing, intentional
        # walk-on-top-of-it approximation already documented above
        # _PASSABLE_SUBSTRINGS. Keep treating those as floor directly,
        # don't look further down.
        #
        # Stairs are handled separately here (rather than falling into
        # the "not passable -> return True" catch-all below) purely for
        # clarity/documentation - they're full/solid now (see
        # _PASSABLE_SUBSTRINGS), so they'd end up standable either way,
        # but calling it out keeps this function self-explanatory.
        if any(s in block for s in ("slab", "turtle_egg")):
            return True
        if "stairs" in block:
            return True  # full-block collision - obviously standable

        # Carpet doesn't have its own floor height - it just inherits
        # whatever's underneath it (wiki: "does not change the hitbox of
        # the block it is placed on"). So a carpet block is only real
        # floor if what's underneath it is real floor. This also catches
        # double-layered carpet (carpet-on-carpet), which the game
        # explicitly does not let you stand on normally.
        if _is_carpet_block(block):
            if _depth >= 4:  # sanity cap against absurd carpet stacks
                return False
            return self.is_solid_floor(x, y - 1, z, _depth=_depth + 1)

        # Everything else the pathfinder treats as walk-through (signs,
        # vines, torches, buttons, levers, pressure plates, rails,
        # redstone wire, crops, banners, etc.) is thin/no-collision by
        # design and can't be relied on as a floor either - the real
        # support is whatever's below it.
        if _is_passable_block(block):
            return self.is_solid_floor(x, y - 1, z, _depth=_depth + 1) if _depth < 4 else False

        return True

    def is_walkable(self, x: int, y: int, z: int) -> bool:
        """Can the player stand at (x, y, z) - i.e. feet at y, head at y+1?"""
        key = (x, y, z)
        cached = self._walkable_cache.get(key)
        if cached is not None:
            return cached
        result = (
            self.is_passable(x, y, z)
            and self.is_passable(x, y + 1, z)
            and self.is_solid_floor(x, y - 1, z)
        )
        self._walkable_cache[key] = result
        return result

    def invalidate_block(self, x: int, y: int, z: int):
        """Call after a block at (x, y, z) has actually been mined, so
        later queries see it as open air instead of a stale cached value."""
        with self._lock:
            self._block_cache[(x, y, z)] = "minecraft:air"
            self._walkable_cache.clear()
            self._floor_cache.clear()
            tile_key = (self._tile_coord(x), self._tile_coord(y), self._tile_coord(z))
            tile = self._tiles.get(tile_key)
            if isinstance(tile, dict):
                tile[(x, y, z)] = "minecraft:air"


# ---------------------------------------------------------------------------
# A* search
# ---------------------------------------------------------------------------

# Per-search cache for SETTINGS.get() lookups made from the neighbor-
# generation hot path. That path calls _is_passable_block/is_solid_floor/
# is_breakable/_step_cost/heuristic on every single candidate move (up to
# 8 directions x 3 dy levels x several checks per expanded node), so a
# raw SETTINGS.get() there gets re-read tens of thousands of times over a
# single search even though the value can't change mid-search. Cleared at
# the start of every find_path() call via _refresh_search_settings_cache(),
# so config changes between runs still take effect - only the redundant
# re-reads within one run are eliminated.
_SEARCH_SETTINGS_CACHE = {}

# Hot-path numeric settings, snapshotted as floats by
# _refresh_search_settings_cache() so _step_cost()/heuristic() avoid a dict
# lookup + float() conversion per candidate move.
_C_WALK = 0.0
_C_JUMP = 0.0
_C_TURN = 0.4
_C_WEIGHT = 1.1
_C_MINE = 6.0


def _cached_setting(key, default=None):
    if key not in _SEARCH_SETTINGS_CACHE:
        _SEARCH_SETTINGS_CACHE[key] = SETTINGS.get(key, default)
    return _SEARCH_SETTINGS_CACHE[key]


def _refresh_search_settings_cache():
    _SEARCH_SETTINGS_CACHE.clear()
    _PASSABLE_BLOCK_CACHE.clear()
    global _C_WALK, _C_JUMP, _C_TURN, _C_WEIGHT, _C_MINE
    _C_WALK, _C_JUMP, _C_TURN, _C_WEIGHT, _C_MINE = _snapshot_floats(
        ("walk_penalty", 0.0), ("jump_penalty", 0.0), ("turn_penalty", 0.4),
        ("weight", 1.1), ("mine_penalty", 6.0))


def _snapshot_floats(*pairs):
    out = []
    for key, default in pairs:
        try:
            out.append(float(SETTINGS.get(key, default)))
        except (TypeError, ValueError):
            out.append(float(default))
    return out


# (dx, dz) horizontal neighbors. Vertical stepping (dy in (0, 1, -1)) is
# tried per direction inside neighbors().
NEIGHBOR_OFFSETS = [
    (1, 0), (-1, 0), (0, 1), (0, -1),
    (1, 1), (1, -1), (-1, 1), (-1, -1),
]

def heuristic(a, b) -> float:
    return math.dist(a, b) * _C_WEIGHT


def _step_cost(dx, dy, dz, is_turn: bool) -> float:
    """Base movement cost for a step, plus the configurable walk/jump
    penalties (walk_penalty applies to every step, jump_penalty is added
    on top for a step-up move), plus "turn_penalty" when this step's
    horizontal direction differs from the one the bot arrived on.

    Pure Euclidean distance treats a run of diagonal steps as cheaper
    than the equivalent cardinal detour, which is fair when that run is
    actually one long straight diagonal line - the bot just points
    itself once and holds forward, same as a straight cardinal line
    would. It stops being fair the moment the "optimal" route zigzags
    between directions to hug that diagonal around minor terrain
    variation: each direction change means stopping to re-aim
    (walk_path's per-waypoint slow-down/speed-up), which costs real
    wall-clock time that plain distance never accounts for. Charging
    "turn_penalty" per direction change makes a fragmented, direction-
    flipping path cost more than a straight run - cardinal or diagonal -
    covering the same ground, so the search stops treating "technically
    shorter but constantly turning" as free."""
    cost = math.sqrt(dx * dx + dy * dy + dz * dz)
    cost += _C_WALK
    if dy == 1:
        cost += _C_JUMP
    if is_turn:
        cost += _C_TURN
    return cost


def _try_mine_move(terrain: TerrainCache, x, y, z, dx, dz, dy, mine_reward_block, is_turn: bool):
    """For an orthogonal (non-diagonal) direction, checks whether stepping
    to (x+dx, y+dy, z+dz) is achievable by mining through whatever's
    blocking it. Returns (cost, mine_blocks) or None if this move isn't
    possible even with mining."""
    nx, nz = x + dx, z + dz
    ny = y + dy

    if not terrain.is_solid_floor(nx, ny - 1, nz):
        return None  # clearing this would just drop the bot into a hole

    cells_to_check = [(nx, ny, nz), (nx, ny + 1, nz)]
    if dy == 1:
        cells_to_check.append((x, y + 2, z))  # overhead clearance to jump up

    obstacles = []
    for ox, oy, oz in cells_to_check:
        block = terrain.get_block(ox, oy, oz)
        if _is_passable_block(block):
            continue
        if not is_breakable(block, mine_reward_block):
            return None
        obstacles.append((ox, oy, oz, block))

    if not obstacles:
        return None  # nothing needed mining - should already be walkable

    mine_penalty = _C_MINE
    cost = _step_cost(dx, dy, dz, is_turn)
    for _ox, _oy, _oz, block in obstacles:
        cost += MINE_REWARD_COST if block == mine_reward_block else mine_penalty
    mine_blocks = tuple((ox, oy, oz) for ox, oy, oz, _block in obstacles)
    return cost, mine_blocks


def neighbors(pos, terrain: TerrainCache, allow_mining: bool = True,
              mine_reward_block: str = None, incoming_dir=None):
    """Yields (neighbor_pos, cost, mine_blocks, outgoing_dir) tuples.
    `incoming_dir` is the (dx, dz) horizontal direction the bot arrived
    at `pos` on (None at the very start, where no direction is charged
    against yet) - used only to decide whether a given move is a "turn"
    for _step_cost's turn_penalty. Vertical-only stepping (dy) doesn't
    affect or reset the tracked horizontal direction."""
    x, y, z = pos

    # Per-expansion memo for is_passable: the headroom cell (x, y+2, z) is
    # re-queried by every direction, and diagonal corner cells overlap
    # across dy retries. Terrain can't change mid-expansion, so memoizing
    # here only dedupes redundant cache lookups within this one call.
    _pass_memo = {}

    def _pass(px, py, pz):
        k = (px, py, pz)
        r = _pass_memo.get(k)
        if r is None:
            r = terrain.is_passable(px, py, pz)
            _pass_memo[k] = r
        return r

    results = []
    for dx, dz in NEIGHBOR_OFFSETS:
        nx, nz = x + dx, z + dz
        is_diagonal = dx != 0 and dz != 0
        out_dir = (dx, dz)
        is_turn = incoming_dir is not None and incoming_dir != out_dir
        chosen = None

        for dy in (0, 1, -1):  # same level, step up, step down
            ny = y + dy

            walkable = terrain.is_walkable(nx, ny, nz)

            if walkable:
                headroom_ok = dy != 1 or _pass(x, y + 2, z)
                if headroom_ok:
                    if is_diagonal:
                        side_a_clear = (_pass(x + dx, y, z) and _pass(x + dx, y + 1, z)
                                        and _pass(x + dx, ny, nz - dz) and _pass(x + dx, ny + 1, nz - dz))
                        side_b_clear = (_pass(x, y, z + dz) and _pass(x, y + 1, z + dz)
                                        and _pass(nx - dx, ny, z + dz) and _pass(nx - dx, ny + 1, z + dz))
                        corner_ok = side_a_clear or side_b_clear
                    else:
                        corner_ok = True
                    if corner_ok:
                        cost = _step_cost(dx, dy, dz, is_turn)
                        chosen = ((nx, ny, nz), cost, (), out_dir)
                        break

            if allow_mining and not is_diagonal:
                mine_result = _try_mine_move(terrain, x, y, z, dx, dz, dy, mine_reward_block, is_turn)
                if mine_result is not None:
                    cost, mine_blocks = mine_result
                    chosen = ((nx, ny, nz), cost, mine_blocks, out_dir)
                    break

        if chosen is not None:
            results.append(chosen)

    return results


def find_path(start, goal, terrain: TerrainCache, max_nodes=100000,
              allow_mining: bool = True, mine_reward_block: str = None):
    """A* over (position, incoming-direction) states rather than plain
    positions - reaching the same block by continuing straight vs. by
    turning into it are genuinely different costs once turn_penalty is
    nonzero (see _step_cost), so they have to be tracked as distinct
    states rather than collapsed into one "cheapest cost to this block"
    entry the way a plain-position search would."""
    start = tuple(start)
    goal = tuple(goal)
    start_state = (start, None)

    _refresh_search_settings_cache()
    # Warm only the start neighborhood once (parallel tile fetch). The rest
    # is pulled lazily by get_block; per-expansion prefetch cost more than
    # it saved.
    try:
        terrain.prefetch_bbox((start[0] - 1, start[1] - 2, start[2] - 1),
                              (start[0] + 1, start[1] + 2, start[2] + 1))
    except Exception:
        pass
    _tiebreak = itertools.count()
    open_heap = [(heuristic(start, goal), 0.0, next(_tiebreak), start_state)]
    came_from = {}
    came_from_mine = {}
    g_score = {start_state: 0.0}
    visited = set()
    nodes_expanded = 0

    while open_heap:
        _check_stop()
        _, g, _, current_state = heapq.heappop(open_heap)
        if current_state in visited:
            continue
        if g > g_score.get(current_state, math.inf) + 1e-9:
            continue  # stale heap entry: a cheaper path was found after this push
        visited.add(current_state)
        nodes_expanded += 1
        current, current_dir = current_state

        if current == goal:
            return reconstruct_path(came_from, came_from_mine, current_state), nodes_expanded

        if nodes_expanded > max_nodes:
            break

        for nbr, cost, mine_blocks, out_dir in neighbors(current, terrain, allow_mining,
                                                          mine_reward_block, current_dir):
            nbr_state = (nbr, out_dir)
            tentative_g = g_score[current_state] + cost
            if tentative_g < g_score.get(nbr_state, math.inf):
                came_from[nbr_state] = current_state
                came_from_mine[nbr_state] = mine_blocks
                g_score[nbr_state] = tentative_g
                f = tentative_g + heuristic(nbr, goal)
                heapq.heappush(open_heap, (f, tentative_g, next(_tiebreak), nbr_state))

    return None, nodes_expanded


def reconstruct_path(came_from, came_from_mine, current_state):
    path = [(current_state[0], came_from_mine.get(current_state, ()))]
    while current_state in came_from:
        current_state = came_from[current_state]
        path.append((current_state[0], came_from_mine.get(current_state, ())))
    path.reverse()
    return path


def simplify_path(path, allow_placing: bool = True):
    """Collapse consecutive nodes that lie on the same straight-line
    direction into a single waypoint.

    When `allow_placing` is False, vertical movement is never merged:
    each height change stays its own waypoint so walk_path() can climb
    +1 auto-jump steps one at a time instead of facing a single
    pillar-height leg that would need blocks placed to climb."""
    if len(path) <= 2:
        return path

    simplified = [path[0]]
    first_pos, _ = path[0]
    second_pos, _ = path[1]
    prev_dir = (
        _sign(second_pos[0] - first_pos[0]),
        _sign(second_pos[1] - first_pos[1]),
        _sign(second_pos[2] - first_pos[2]),
    )

    for i in range(2, len(path)):
        cur_pos, cur_mine = path[i]
        prev_pos, prev_mine = path[i - 1]
        direction = (
            _sign(cur_pos[0] - prev_pos[0]),
            _sign(cur_pos[1] - prev_pos[1]),
            _sign(cur_pos[2] - prev_pos[2]),
        )
        if (direction != prev_dir or prev_mine or cur_mine
                or (not allow_placing and direction[1] != 0)):
            simplified.append(path[i - 1])
            prev_dir = direction

    simplified.append(path[-1])
    return simplified


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


# ---------------------------------------------------------------------------
# Mining
# ---------------------------------------------------------------------------

def mine_block(pos, terrain: TerrainCache = None, timeout: float = 8.0,
               poll_interval: float = 0.05, mine_reward_block: str = None) -> bool:
    """Looks at and breaks the block at `pos`, polling the live world state
    until it's gone or `timeout` elapses. Returns True if the block was
    cleared, False on timeout."""
    x, y, z = pos
    try:
        px0, py0, pz0 = player_position()
        try:
            reach_lim = float(SETTINGS.get("reach", REACH_BLOCKS))
        except (TypeError, ValueError):
            reach_lim = float(REACH_BLOCKS)
        if math.dist((px0, py0 + 1.62, pz0), (x + 0.5, y + 0.5, z + 0.5)) > reach_lim + 1.0:
            return False
    except Exception:
        pass
    try:
        block = (minescript.getblock(x, y, z) or "minecraft:air").split("[", 1)[0]
    except Exception:
        return False
    if not is_breakable(block, mine_reward_block):
        if terrain is not None:
            terrain.invalidate_block(x, y, z)
        return True  # already clear (or something we can't/shouldn't mine)

    player_look_at(x + 0.5, y + 0.5, z + 0.5)
    player_press_attack(True)
    try:
        start_time = time.time()
        while time.time() - start_time < timeout:
            _check_stop()
            current = (minescript.getblock(x, y, z) or "minecraft:air").split("[", 1)[0]
            if not is_breakable(current, mine_reward_block):
                if terrain is not None:
                    terrain.invalidate_block(x, y, z)
                return True
            time.sleep(poll_interval)
        return False
    finally:
        player_press_attack(False)


# ---------------------------------------------------------------------------
# Movement
# ---------------------------------------------------------------------------

def walk_path(path, terrain: TerrainCache = None, node_timeout=5.0, arrive_dist=0.2,
              slow_dist=1.1, stuck_check_interval=0.4, stuck_dist_threshold=0.08,
              stuck_jump_bursts=3, mine_timeout=8.0, mine_reward_block: str = None,
              progress_cb=None, precise_landing: bool = None, waypoint_tolerance: float = None,
              per_block_timeout: float = None):
    """Follows a path (list of (pos, mine_blocks) pairs from find_path).
    Sprinting is gated by the "allow_sprinting" setting, and is dropped
    while the bot stops to mine - it doesn't just hold sprint the whole
    way regardless of what it's doing.

    Approaching each waypoint at full sprint and only checking position
    every 0.1s let the bot cover more than half a block per tick, so it
    could blow straight through a single-block waypoint (and the "avoid
    this block" check that guards it) before ever registering as having
    arrived. To make sure it actually settles into the middle of a block
    before counting it as stepped on: sprinting is dropped within
    `slow_dist` blocks of the target (walking speed only, for a short,
    controllable final approach), `arrive_dist` requires being close to
    the true center rather than loosely "close enough", and the position
    poll runs on a tighter interval so overshoot per check stays small.

    Not every waypoint actually needs that pinpoint precision, though -
    it only matters where missing the exact block would matter: right
    before mining, right before/after a step-up (miss the ledge and the
    jump timing is wrong), and the final node of the path (the actual
    destination). Plain flat-walking waypoints in between are just the
    path's shape, not places the bot needs to stand on the nose - being
    within `waypoint_tolerance` (setting, default 0.6) is enough to
    round the corner and move on, which also makes travel look far less
    twitchy. Setting "precise_landing" to true forces every waypoint,
    not just the critical ones, to be hit with the tight `arrive_dist` -
    this is the old, always-exact behavior, kept as an opt-in.

    Whichever tolerance is used for a given node, the check-and-adjust
    loop's poll interval is kept short (see the `time.sleep(...)` call
    below) specifically so that at typical sprint speed the player moves
    only a small fraction of a block between checks - keeping any
    overshoot well inside whatever tolerance is active, so a waypoint is
    never simply blown through unnoticed even when the tolerance itself
    is tight.

    `node_timeout` (5s) is a flat floor, but `simplify_path()` can collapse
    a long straight run into a single waypoint many blocks away, so a flat
    timeout was routinely tripping on long, perfectly-fine legs. Each
    node's actual timeout is now whichever is bigger: `node_timeout`, or
    the straight-line distance from the previous waypoint (or the bot's
    position when this call started, for the first node) times
    `per_block_timeout` (setting, default 10.0 - seconds allowed per block
    of that leg's length). So a 20-block-long simplified straight line
    gets ~200s to finish rather than 5."""
    sprint_enabled = _as_bool(SETTINGS.get("allow_sprinting", True))
    if precise_landing is None:
        precise_landing = bool(SETTINGS.get("precise_landing", False))
    if waypoint_tolerance is None:
        waypoint_tolerance = float(SETTINGS.get("waypoint_tolerance", 0.6))
    if per_block_timeout is None:
        per_block_timeout = float(SETTINGS.get("per_block_timeout", 10.0))
    # A loose waypoint still has to be tighter than the "am I sprinting"
    # cutoff, or the bot would never come off sprint before "arriving".
    waypoint_tolerance = max(arrive_dist, min(waypoint_tolerance, slow_dist * 0.9))
    sprinting = False

    def set_sprint(on: bool):
        nonlocal sprinting
        on = on and sprint_enabled
        if on != sprinting:
            player_press_sprint(on)
            sprinting = on

    # Straight up ignore pillar legs when placing is disabled: any
    # waypoint more than one block above the previous kept waypoint
    # would need blocks placed to climb, so drop it instead of walking
    # toward it and attempting a tower. (The A* search itself only ever
    # emits dy in (-1, 0, +1), so these legs only arise from
    # simplify_path() collapsing stairs or from live position drift -
    # both handled here rather than in neighbors().)
    if not _as_bool(SETTINGS.get("allow_placing_blocks", True)):
        filtered = [path[0]] if path else []
        for node, mine_blocks in path[1:]:
            if node[1] - filtered[-1][0][1] > 1:
                go_echo(f"Skipping \u00a77{node}\u00a7f - would need blocks placed to climb "
                        f"\u00a78(\u00a77allow_placing_blocks=false\u00a78)",
                        error=True)
                continue
            filtered.append((node, mine_blocks))
        path = filtered

    total_nodes = len(path)
    node_positions = [n for n, _mine in path]

    # For a smooth, sub-waypoint "#eta" fraction rather than one jump per
    # waypoint reached: remaining_after[i] is the remaining straight-line
    # path distance from node i through to the final node. While actually
    # walking toward node i, the total distance left is just that plus
    # however far the player currently still is from node i - so progress
    # can be reported continuously as the player closes in on each
    # waypoint, not only when it's fully reached.
    remaining_after = [0.0] * total_nodes
    for j in range(total_nodes - 2, -1, -1):
        remaining_after[j] = remaining_after[j + 1] + math.dist(node_positions[j], node_positions[j + 1])
    total_dist = remaining_after[0] if total_nodes > 1 else 0.0

    def _report(i, pos):
        if progress_cb is None or total_dist <= 1e-6:
            return
        remaining = math.dist(pos, node_positions[i]) + remaining_after[i]
        fraction = max(0.0, min(1.0, 1.0 - remaining / total_dist))
        progress_cb(fraction, i, total_nodes)

    prev_node_pos = tuple(player_position())

    try:
        for i, (node, mine_blocks) in enumerate(path):
            _check_stop()

            # See the "node_timeout"/"per_block_timeout" note in the
            # docstring above - a long simplified straight leg needs much
            # more than the flat floor to finish without false-timing-out.
            leg_dist = math.dist(prev_node_pos, node)
            dynamic_node_timeout = max(node_timeout, leg_dist * per_block_timeout)
            prev_node_pos = node

            # Decide how precisely this particular node needs to be hit.
            # Critical nodes (must be tight): the very last node (the
            # actual destination), any node with mining to do (the bot
            # needs to actually be standing there), and either side of an
            # elevation change (stepping up/down needs the jump timed off
            # the real ledge, not somewhere near it).
            is_last = i == total_nodes - 1
            prev_y = node_positions[i - 1][1] if i > 0 else node[1]
            next_y = node_positions[i + 1][1] if i + 1 < total_nodes else node[1]
            is_step_node = node[1] != prev_y or node[1] != next_y
            critical = precise_landing or is_last or bool(mine_blocks) or is_step_node
            node_arrive_dist = arrive_dist if critical else waypoint_tolerance

            if mine_blocks:
                set_sprint(False)
                player_press_forward(False)
                go_echo(f"Mining \u00a77{len(mine_blocks)}\u00a7f block(s) to continue...")
                for block_pos in mine_blocks:
                    if not mine_block(block_pos, terrain=terrain, timeout=mine_timeout,
                                       mine_reward_block=mine_reward_block):
                        go_echo(f"Couldn't clear block at \u00a77{block_pos}\u00a7c, trying to continue anyway",
                                error=True)

            target_x = node[0] + 0.5
            target_z = node[2] + 0.5
            target_y = node[1]

            start_time = time.time()
            last_stuck_check = start_time
            last_check_pos = None
            jump_burst_remaining = 0

            while True:
                _check_stop()
                px, py, pz = player_position()
                dist_xz = math.dist((px, pz), (target_x, target_z))
                _report(i, (px, py, pz))

                if dist_xz <= node_arrive_dist:
                    break
                if time.time() - start_time > dynamic_node_timeout:
                    go_echo(f"Timed out moving to \u00a77{node}\u00a7f, continuing")
                    go_echo("Tip: use \u00a77#settings set per_block_timeout <seconds>\u00a7f "
                            "to change the maximum timeout per block travelled!")
                    break

                player_look_at(target_x, target_y + 1.0, target_z)
                player_press_forward(True)
                # Only sprint while there's still real distance to cover -
                # come off sprint for the last stretch into the block so
                # momentum doesn't carry the bot past its center.
                set_sprint(dist_xz > slow_dist)

                now = time.time()
                if now - last_stuck_check >= stuck_check_interval:
                    if last_check_pos is not None:
                        moved = math.dist((px, pz), last_check_pos)
                        if moved < stuck_dist_threshold:
                            jump_burst_remaining = stuck_jump_bursts
                    last_check_pos = (px, pz)
                    last_stuck_check = now

                needs_step_up = target_y > py + 0.05
                step_height = target_y - py
                if needs_step_up and step_height > 1.05:
                    # More than a single auto-jump step - no floor to walk
                    # onto exists yet, so pillar up under our own feet
                    # instead (obstruction the A* search couldn't route
                    # around/through as a walk or a mine). Never attempts
                    # this when "allow_placing_blocks" is off - just give
                    # up on the node instead of spamming placement.
                    if not _as_bool(SETTINGS.get("allow_placing_blocks", True)):
                        go_echo(f"Blocked by \u00a77{int(math.ceil(step_height))}\u00a7f-high wall "
                                f"at \u00a77{node}\u00a7f, but block placing is disabled "
                                f"\u00a78(\u00a77allow_placing_blocks=false\u00a78)\u00a7f - skipping",
                                error=True)
                        break
                    set_sprint(False)
                    player_press_forward(False)
                    go_echo(f"Obstruction ahead needs \u00a77{int(math.ceil(step_height))}\u00a7f block(s) "
                            "placed to climb...")
                    if not place_block_tower(int(math.ceil(step_height)), terrain=terrain):
                        go_echo(f"Couldn't place blocks to climb to \u00a77{node}\u00a7f - skipping",
                                error=True)
                        break
                if needs_step_up or jump_burst_remaining > 0:
                    player_press_jump(True)
                    if jump_burst_remaining > 0:
                        jump_burst_remaining -= 1
                else:
                    player_press_jump(False)

                # Poll (and re-issue look/movement keys) frequently enough
                # that overshoot between checks stays small relative to
                # whichever arrive-distance is active for this node -
                # tightest right when it matters most (critical nodes,
                # and the final short approach into any node), a little
                # more relaxed only while still sprinting well outside
                # slow_dist on a non-critical node.
                if dist_xz <= slow_dist or critical:
                    time.sleep(0.02)
                else:
                    time.sleep(0.05)

            player_press_forward(False)
            player_press_jump(False)
            _report(i, node)
    finally:
        player_press_forward(False)
        player_press_jump(False)
        set_sprint(False)


# ---------------------------------------------------------------------------
# Block search (for "\go goto BLOCK_ID")
# ---------------------------------------------------------------------------

def normalize_block_id(block_id: str) -> str:
    block_id = block_id.strip().lower()
    if ":" not in block_id:
        block_id = f"minecraft:{block_id}"
    return block_id


def _shell_offsets(radius: int):
    if radius == 0:
        yield (0, 0, 0)
        return
    r = radius
    for x in range(-r, r + 1):
        for y in range(-r, r + 1):
            for z in range(-r, r + 1):
                if max(abs(x), abs(y), abs(z)) == r:
                    yield (x, y, z)


def _line_of_sight_clear(terrain: TerrainCache, from_pos, to_pos, ignore=frozenset()):
    dist = math.dist(from_pos, to_pos)
    if dist < 1e-6:
        return True
    steps = max(1, int(dist / 0.25))
    for i in range(1, steps):
        t = i / steps
        x = from_pos[0] + (to_pos[0] - from_pos[0]) * t
        y = from_pos[1] + (to_pos[1] - from_pos[1]) * t
        z = from_pos[2] + (to_pos[2] - from_pos[2]) * t
        bx, by, bz = int(math.floor(x)), int(math.floor(y)), int(math.floor(z))
        if (bx, by, bz) in ignore:
            continue
        block = terrain.get_block(bx, by, bz)
        if block != "minecraft:air" and not _is_passable_block(block):
            return False
    return True


def _find_stand_position_near(terrain: TerrainCache, bx: int, by: int, bz: int):
    if terrain.is_walkable(bx, by + 1, bz):
        return (bx, by + 1, bz)
    if terrain.is_walkable(bx, by, bz):
        return (bx, by, bz)
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        for dy in (0, 1, -1):
            nx, ny, nz = bx + dx, by + dy, bz + dz
            if terrain.is_walkable(nx, ny, nz):
                return (nx, ny, nz)
    return None


def find_nearby_block(terrain: TerrainCache, center, block_id: str,
                       max_radius: int = 48, y_radius: int = 24, exclude=None):
    cx, cy, cz = center
    block_id = normalize_block_id(block_id)

    for radius in range(0, max_radius + 1):
        candidates = []
        for dx, dy, dz in _shell_offsets(radius):
            if abs(dy) > y_radius:
                continue
            x, y, z = cx + dx, cy + dy, cz + dz
            if exclude and (x, y, z) in exclude:
                continue
            if terrain.get_block(x, y, z) == block_id:
                candidates.append((x, y, z))

        candidates.sort(key=lambda p: math.dist(center, p))

        for x, y, z in candidates:
            stand_pos = _find_stand_position_near(terrain, x, y, z)
            if stand_pos is None:
                continue
            eye = (stand_pos[0] + 0.5, stand_pos[1] + 1.62, stand_pos[2] + 0.5)
            target_center = (x + 0.5, y + 0.5, z + 0.5)
            if not _line_of_sight_clear(terrain, eye, target_center, ignore={(x, y, z)}):
                continue
            return stand_pos, (x, y, z)

    return None, None


# ---------------------------------------------------------------------------
# Settings command ("\go settings ...")
# ---------------------------------------------------------------------------

SETTINGS_USAGE = (
    "\u00a7fUsage: \u00a77# settings list\u00a7f | \u00a77get KEY\u00a7f | "
    "\u00a77set KEY VALUE\u00a7f | \u00a77add KEY VALUE\u00a7f | "
    "\u00a77remove KEY VALUE\u00a7f | \u00a77unset KEY\u00a7f | "
    "\u00a77clear KEY\u00a7f | \u00a77toggle KEY"
)


def _format_setting_value(value) -> str:
    if isinstance(value, list):
        return "[]" if not value else "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def _coerce_scalar_value(key: str, raw_value: str):
    if key in BOOL_SETTINGS:
        return raw_value.strip().lower() in ("true", "1", "yes", "on")
    raw_value = raw_value.strip()
    try:
        if re.fullmatch(r"-?\d+", raw_value):
            return int(raw_value)
        return float(raw_value)
    except ValueError:
        raise ValueError(f"'{raw_value}' is not a valid number")


def handle_settings(args):
    if not args:
        go_echo(SETTINGS_USAGE)
        return

    action = args[0].lower()
    rest = args[1:]

    if action == "list":
        go_echo("--- pathfinder settings (minescript/pf/pf_pref.json) ---")
        for key in sorted(SETTINGS.keys()):
            go_echo(f"\u00a77{key}\u00a7f: \u00a77{_format_setting_value(SETTINGS[key])}")
        return

    if not rest:
        go_echo(f"Missing setting name.\n{SETTINGS_USAGE}", error=True)
        return

    key = rest[0].strip().lower()
    if key not in DEFAULT_SETTINGS:
        go_echo(f"Unknown setting \u00a77{key}\u00a7c. Valid settings: \u00a77{', '.join(sorted(DEFAULT_SETTINGS))}",
                error=True)
        return

    if action == "get":
        go_echo(f"\u00a77{key}\u00a7f = \u00a77{_format_setting_value(SETTINGS.get(key))}")
        return

    if action == "unset":
        SETTINGS[key] = _copy_setting_value(DEFAULT_SETTINGS[key])
        save_settings()
        _settings_changed()
        go_echo(f"\u00a77{key}\u00a7f reset to default \u00a78(\u00a77{_format_setting_value(SETTINGS[key])}\u00a78)")
        return

    if action == "clear":
        if key not in LIST_SETTINGS:
            go_echo(f"\u00a77{key}\u00a7c is not a list setting, so it can't be cleared - try 'unset' instead.",
                    error=True)
            return
        SETTINGS[key] = []
        save_settings()
        _settings_changed()
        go_echo(f"\u00a77{key}\u00a7f cleared")
        return

    if action == "toggle":
        if key not in BOOL_SETTINGS:
            go_echo(f"\u00a77{key}\u00a7c is not a toggleable (true/false) setting.", error=True)
            return
        SETTINGS[key] = not _as_bool(SETTINGS.get(key))
        save_settings()
        _settings_changed()
        go_echo(f"\u00a77{key}\u00a7f is now \u00a77{SETTINGS[key]}")
        return

    if action in ("add", "remove"):
        if key not in LIST_SETTINGS:
            go_echo(f"\u00a77{key}\u00a7c is not a list setting.", error=True)
            return
        if len(rest) < 2:
            go_echo(f"Usage: # settings {action} {key} VALUE", error=True)
            return
        value = " ".join(rest[1:]).strip()
        if not value:
            go_echo("VALUE can't be blank", error=True)
            return
        if "," in value:
            go_echo(f"Usage: # settings {action} {key} SINGLE_VALUE (no commas - 'set' takes a comma list, 'add/remove' take one value)",
                    error=True)
            return
        if key in BLOCK_ID_LIST_SETTINGS:
            value = normalize_block_id(value)
        lst = SETTINGS.setdefault(key, [])
        if action == "add":
            if value in lst:
                go_echo(f"\u00a77{value}\u00a7f is already in \u00a77{key}")
            else:
                lst.append(value)
                go_echo(f"Added \u00a77{value}\u00a7f to \u00a77{key}")
        else:
            if value in lst:
                lst.remove(value)
                go_echo(f"Removed \u00a77{value}\u00a7f from \u00a77{key}")
            else:
                go_echo(f"\u00a77{value}\u00a7f isn't in \u00a77{key}")
        save_settings()
        _settings_changed()
        return

    if action == "set":
        if len(rest) < 2:
            go_echo(f"Usage: # settings set {key} VALUE", error=True)
            return
        raw_value = " ".join(rest[1:]).strip()
        if key in LIST_SETTINGS:
            items = [v.strip() for v in raw_value.split(",") if v.strip()]
            if key in BLOCK_ID_LIST_SETTINGS:
                items = [normalize_block_id(v) for v in items]
            SETTINGS[key] = items
        else:
            try:
                SETTINGS[key] = _coerce_scalar_value(key, raw_value)
            except ValueError as e:
                go_echo(str(e), error=True)
                return
            if key == "threads_for_searching":
                if SETTINGS[key] < MIN_SEARCH_THREADS:
                    go_echo(
                        f"\u00a77{key}\u00a7f can't go below \u00a77{MIN_SEARCH_THREADS}\u00a7f; "
                        f"clamping up to \u00a77{MIN_SEARCH_THREADS}\u00a7f."
                    )
                    SETTINGS[key] = MIN_SEARCH_THREADS
                elif SETTINGS[key] > MAX_SEARCH_THREADS:
                    go_echo(
                        f"\u00a77{key}\u00a7f can't go above \u00a77{MAX_SEARCH_THREADS}\u00a7f; "
                        f"clamping down to \u00a77{MAX_SEARCH_THREADS}\u00a7f."
                    )
                    SETTINGS[key] = MAX_SEARCH_THREADS
        save_settings()
        _settings_changed()
        go_echo(f"\u00a77{key}\u00a7f set to \u00a77{_format_setting_value(SETTINGS[key])}")
        return

    go_echo(f"Unknown settings action \u00a77{action}\u00a7c.\n{SETTINGS_USAGE}", error=True)


# ---------------------------------------------------------------------------
# Waypoints
# ---------------------------------------------------------------------------
#
# Saved player-named locations, persisted to minescript/pf/waypoints.json.
# Kept separate per world (server address + spawn point - the same
# identity TilePersistentCache uses for the terrain tile cache, see
# _world_key() above), so waypoints from one server/save don't show up
# or get walked to on another. The file holds every world's waypoints at
# once, keyed by that per-world identifier:
#
#   { "<world_key>": { "<name lowercased>": {"name": "<name>",
#                                             "pos": [x, y, z]}, ... }, ... }

WAYPOINTS_USAGE = (
    "\u00a7fUsage: \u00a77# wp add NAME [X] [Y] [Z]\u00a7f | \u00a77to NAME\u00a7f | "
    "\u00a77remove NAME\u00a7f | \u00a77list\u00a7f | \u00a77clear\n"
    "\u00a78(aliases: 'new' for 'add', 'goto' for 'to', 'rm'/'delete' for "
    "'remove'; with no X/Y/Z, 'add' uses your current position)"
)


def _load_waypoints_file() -> dict:
    if not os.path.exists(PF_WAYPOINTS_PATH):
        return {}
    try:
        with open(PF_WAYPOINTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_waypoints_file(data: dict):
    ensure_dirs()
    try:
        with open(PF_WAYPOINTS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception as e:
        go_echo(f"Failed to save waypoints: {e}", error=True)


def _get_world_waypoints():
    """Returns (whole_file_data, this_world's {name_key: waypoint} dict).
    Mutate the second in place, then pass the first to
    _save_waypoints_file() to persist it."""
    all_data = _load_waypoints_file()
    world_wp = all_data.get(_world_key())
    if not isinstance(world_wp, dict):
        world_wp = {}
        all_data[_world_key()] = world_wp
    return all_data, world_wp


def handle_waypoints(terrain: "TerrainCache", start, args):
    if not args:
        go_echo(WAYPOINTS_USAGE)
        return

    action = args[0].lower()
    rest = args[1:]

    if action in ("add", "new"):
        if not rest:
            go_echo("Usage: # wp add NAME [X] [Y] [Z]", error=True)
            return
        name = rest[0].strip()
        if not name:
            go_echo("Waypoint name can't be blank", error=True)
            return
        if len(rest) == 1:
            px, py, pz = player_position()
            pos = (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz)))
        elif len(rest) == 4:
            try:
                pos = (int(math.floor(float(rest[1]))), int(math.floor(float(rest[2]))), int(math.floor(float(rest[3]))))
            except ValueError:
                go_echo("X/Y/Z must be numbers", error=True)
                return
        elif len(rest) > 4:
            go_echo("Usage: # wp add NAME [X] [Y] [Z] (too many arguments)", error=True)
            return
        else:
            go_echo("Usage: # wp add NAME [X] [Y] [Z] (X/Y/Z must come as a full triplet)", error=True)
            return

        all_data, world_wp = _get_world_waypoints()
        world_wp[name.lower()] = {"name": name, "pos": list(pos)}
        _save_waypoints_file(all_data)
        go_echo(f"Waypoint \u00a77{name}\u00a7f saved at \u00a77{pos}")
        return

    if action in ("to", "goto"):
        if not rest:
            go_echo("Usage: # wp to NAME", error=True)
            return
        name = rest[0].strip()
        _, world_wp = _get_world_waypoints()
        wp = world_wp.get(name.lower())
        if wp is None:
            go_echo(f"No waypoint named \u00a77{name}\u00a7c - '#wp list' to see what's saved",
                    error=True)
            return
        run_to(terrain, start, tuple(wp["pos"]))
        return

    if action in ("remove", "rm", "delete"):
        if not rest:
            go_echo("Usage: # wp remove NAME", error=True)
            return
        name = rest[0].strip()
        all_data, world_wp = _get_world_waypoints()
        if name.lower() not in world_wp:
            go_echo(f"No waypoint named \u00a77{name}\u00a7c", error=True)
            return
        del world_wp[name.lower()]
        _save_waypoints_file(all_data)
        go_echo(f"Waypoint \u00a77{name}\u00a7f removed")
        return

    if action == "list":
        _, world_wp = _get_world_waypoints()
        if not world_wp:
            go_echo("No waypoints saved for this world yet")
            return
        go_echo(f"--- waypoints \u00a78(\u00a77{len(world_wp)}\u00a78)\u00a7f ---")
        for wp in sorted(world_wp.values(), key=lambda w: w["name"].lower()):
            x, y, z = wp["pos"]
            go_echo(f"\u00a77{wp['name']}\u00a7f: \u00a77{x}, {y}, {z}")
        return

    if action == "clear":
        all_data, world_wp = _get_world_waypoints()
        count = len(world_wp)
        world_wp.clear()
        _save_waypoints_file(all_data)
        go_echo(f"Cleared \u00a77{count}\u00a7f waypoint(s)")
        return

    go_echo(f"Unknown waypoints action \u00a77{action}\u00a7c.\n{WAYPOINTS_USAGE}", error=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

USAGE = (
    "\u00a7fUsage: \u00a77# goto X Y Z\u00a7f | \u00a77# goto BLOCK_ID\u00a7f | "
    "\u00a77# mine BLOCK_ID [MAX_COUNT]\u00a7f | "
    "\u00a77# follow ENTITY_TYPE\u00a7f | "
    "\u00a77# wander [RADIUS] [LEGS]\u00a7f | "
    "\u00a77# wp ...\u00a7f | \u00a77# settings ...\n"
    "\u00a78(run bare # with no args to start the daemon, then use "
    "'#<command>' in chat, e.g. '#goto 100 64 100'; '#eta' reports progress, "
    "'#stop' cancels the running task and clears the queue)"
)

DEFAULT_MINE_TARGETS = 64


def run_to(terrain: TerrainCache, start, goal, allow_mining=None, mine_reward_block=None,
           progress_base: float = 0.0, progress_span: float = 1.0, progress_label: str = ""):
    """Finds and walks a path from start to goal. Returns True on success.

    `progress_base`/`progress_span`/`progress_label` let a caller that's
    running several run_to() calls back-to-back (handle_mine, _travel_far)
    fold this leg's own progress into one overall fraction for "#eta" - this
    call reports itself as filling [progress_base, progress_base + progress_span]
    of the whole task instead of always reporting 0-1 for just this leg."""
    if allow_mining is None:
        allow_mining = _as_bool(SETTINGS.get("allow_breaking_blocks", True))

    go_echo(f"Searching a path to \u00a77{goal}\u00a7f...")
    search_start = time.time()
    path, expanded = find_path(start, goal, terrain, allow_mining=allow_mining,
                                mine_reward_block=mine_reward_block)
    search_elapsed = max(time.time() - search_start, 1e-6)
    rate = int(expanded / search_elapsed)
    go_echo(f"Searched \u00a77{expanded}\u00a7f nodes. \u00a78(\u00a77{rate}\u00a78 nodes/s\u00a78)")

    if path is None:
        go_echo(f"No path found \u00a78(\u00a77{expanded}\u00a78 nodes explored\u00a78)", error=True)
        return False

    mine_steps = sum(1 for _pos, mine_blocks in path if mine_blocks)
    go_echo(
        f"Found path to \u00a77{goal}\u00a7f \u00a78(\u00a77{len(path)}\u00a78 steps, "
        f"\u00a77{mine_steps}\u00a78 needing mining\u00a78)"
    )

    def _on_progress(leg_frac, i, total):
        detail = f"waypoint {i + 1}/{total}, {leg_frac * 100:.0f}% to it" if total else ""
        if progress_label:
            detail = f"{progress_label}, {detail}" if detail else progress_label
        _progress_set(progress_base + leg_frac * progress_span, detail)

    allow_placing = _as_bool(SETTINGS.get("allow_placing_blocks", True))
    walk_path(simplify_path(path, allow_placing=allow_placing), terrain=terrain,
              mine_reward_block=mine_reward_block, progress_cb=_on_progress)
    try:
        px, py, pz = player_position()
        arrived = math.dist((px, pz), (goal[0] + 0.5, goal[2] + 0.5)) <= max(
            1.0, float(SETTINGS.get("waypoint_tolerance", 0.6)))
    except Exception:
        arrived = False
    if arrived:
        go_echo("Arrived.")
        return True
    go_echo(f"Gave up trying to reach \u00a77{goal}\u00a7f (path found but walk timed out / was blocked)",
            error=True)
    return False


def _parse_coord(value: str):
    """Block coordinate parse: floor() so negatives behave like the rest of
    the bot (int(-1.5) is -1, but block -1.5 lives in block -2)."""
    return int(math.floor(float(value)))


def handle_goto(terrain: TerrainCache, start, args):
    goal = None
    if len(args) >= 3:
        try:
            goal = (_parse_coord(args[0]), _parse_coord(args[1]), _parse_coord(args[2]))
        except ValueError:
            goal = None
    elif args:
        try:
            _parse_coord(args[0])
            go_echo("Usage: # goto X Y Z (need all three coordinates)", error=True)
            return
        except ValueError:
            pass  # first arg isn't numeric -> treat as BLOCK_ID below

    if goal is None:
        if not args:
            go_echo(USAGE)
            return
        block_id = normalize_block_id(args[0])
        go_echo(f"Scanning for \u00a77{block_id}\u00a7f...")
        scan_start = time.time()
        goal, block_pos = find_nearby_block(terrain, start, block_id)
        scan_elapsed = max(time.time() - scan_start, 1e-6)
        go_echo(
            f"Scanned in \u00a77{scan_elapsed:.2f}s\u00a7f. "
            f"\u00a78(\u00a77{terrain.tiles_fetched}\u00a78 tiles live, "
            f"\u00a77{terrain.tiles_from_disk_cache}\u00a78 from cache\u00a78)"
        )
        if goal is None:
            go_echo(f"No \u00a77{block_id}\u00a7c found nearby", error=True)
            return
        go_echo(f"Found \u00a77{block_id}\u00a7f at \u00a77{block_pos}\u00a7f, heading to \u00a77{goal}")

    dx = abs(goal[0] - start[0])
    dz = abs(goal[2] - start[2])
    # Stage by longest axis, not dx*dz area: a 2000-block straight-line trip
    # has ~zero area (one axis is ~0) but still needs staging.
    if max(dx, dz) > LONG_DISTANCE_LEG_SIZE:
        go_echo(
            f"Goal is far away \u00a78(\u00a77~{dx}x{dz}\u00a78 block area\u00a78)\u00a7f - "
            f"traveling there in stages instead of one long search..."
        )
        _travel_far(terrain, start, goal)
        return

    run_to(terrain, start, goal)


def handle_mine(terrain: TerrainCache, start, args):
    if not args:
        go_echo(USAGE)
        return

    block_id = normalize_block_id(args[0])

    px, py, pz = player_position()
    cx, cy, cz = int(math.floor(px)), int(math.floor(py)), int(math.floor(pz))
    nearby = {}
    for dx, dy, dz in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
        b = (minescript.getblock(cx+dx, cy+dy, cz+dz) or "minecraft:air").split("[", 1)[0]
        if b != "minecraft:air":
            nearby[(dx,dy,dz)] = b
    if nearby:
        go_echo(f"Blocks touching you right now: \u00a77{sorted(set(nearby.values()))}")
        if block_id not in nearby.values():
            go_echo(f"None of those match \u00a77{block_id}\u00a7f exactly - check the exact ID above")

    try:
        max_targets = int(float(args[1])) if len(args) >= 2 else DEFAULT_MINE_TARGETS
    except ValueError:
        max_targets = DEFAULT_MINE_TARGETS
    if max_targets < 1:
        go_echo(f"MAX_COUNT must be at least 1 (got \u00a77{args[1] if len(args) >= 2 else max_targets}\u00a7f)",
                error=True)
        return

    go_echo(f"Mining up to \u00a77{max_targets}x {block_id}\u00a7f (prioritizing minimal collateral digging)...")
    unreachable = set()
    mined = 0

    for i in range(max_targets):
        _check_stop()
        _progress_set(i / max_targets, f"{i}/{max_targets} {block_id} mined")
        px, py, pz = player_position()
        cur = (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz)))

        goal, block_pos = find_nearby_block(terrain, cur, block_id, exclude=unreachable)
        if goal is None:
            go_echo(f"No more \u00a77{block_id}\u00a7f found nearby \u00a78(\u00a77{mined}\u00a78 mined\u00a78)")
            break

        # If it's already within reach, mine it in place instead of
        # searching/walking a path to get there first. Measured from the
        # eyes (feet + 1.62), matching survival reach, not from the feet.
        try:
            reach = float(SETTINGS.get("reach", REACH_BLOCKS))
        except (TypeError, ValueError):
            reach = float(REACH_BLOCKS)
        bx, by, bz = block_pos
        in_reach = math.dist((px, py + 1.62, pz), (bx + 0.5, by + 0.5, bz + 0.5)) <= reach

        if in_reach:
            go_echo(f"\u00a78[\u00a77{i + 1}/{max_targets}\u00a78]\u00a7f \u00a77{block_id}\u00a7f at "
                    f"\u00a77{block_pos}\u00a7f already in reach, mining directly")
        else:
            go_echo(f"\u00a78[\u00a77{i + 1}/{max_targets}\u00a78]\u00a7f heading to \u00a77{block_id}\u00a7f at \u00a77{block_pos}")
            # Mining is always allowed for this command's own target, and
            # "avoid_mining" is overridden for block_id specifically, but
            # collateral obstacles of other types still respect it.
            ok = run_to(terrain, cur, goal, allow_mining=True, mine_reward_block=block_id,
                        progress_base=i / max_targets, progress_span=1.0 / max_targets,
                        progress_label=f"{i}/{max_targets} {block_id} mined")
            if not ok:
                go_echo(f"Couldn't reach \u00a77{block_id}\u00a7f at \u00a77{block_pos}\u00a7f, skipping it")
                unreachable.add(block_pos)
                continue
            # run_to() may report success while still a block or two short
            # (walk timeout). Re-check reach from the new position instead
            # of swinging at a block that is still far away.
            px2, py2, pz2 = player_position()
            if math.dist((px2, py2 + 1.62, pz2), (bx + 0.5, by + 0.5, bz + 0.5)) > reach:
                go_echo(f"Still out of reach of \u00a77{block_id}\u00a7f at \u00a77{block_pos}\u00a7f, skipping it",
                        error=True)
                unreachable.add(block_pos)
                continue

        try:
            live_block = (minescript.getblock(*block_pos) or "minecraft:air").split("[", 1)[0]
        except Exception:
            live_block = "minecraft:air"
        if live_block == block_id:
            if mine_block(block_pos, terrain=terrain, mine_reward_block=block_id):
                mined += 1
        else:
            # Terrain cache said block_id was here, but the live world
            # disagrees (stale cache entry) - drop it so it isn't offered
            # again, and don't count it as mined.
            terrain.invalidate_block(*block_pos)
            unreachable.add(block_pos)
            go_echo(f"\u00a77{block_pos}\u00a7f no longer matches \u00a77{block_id}\u00a7f "
                    f"\u00a78(\u00a77stale cache\u00a78)\u00a7f, skipping it")

    go_echo(f"Done - mined \u00a77{mined} {block_id}")


def handle_follow(terrain: TerrainCache, args, arrive_dist: float = 2.0,
                   recheck_interval: float = 0.5):
    if not args:
        go_echo(USAGE)
        return

    entity_type = args[0].strip().lower()
    type_pattern = f".*{re.escape(entity_type)}.*"

    go_echo(f"Looking for nearest entity matching \u00a77{entity_type}\u00a7f...")
    matches = minescript.entities(type=type_pattern, sort="nearest", limit=1)
    if not matches:
        nearby = minescript.entities(max_distance=16, sort="nearest", limit=10)
        if nearby:
            seen_types = sorted({e.type for e in nearby})
            go_echo(f"No match for \u00a77{entity_type}\u00a7f - nearby entity types: \u00a77{seen_types}",
                    error=True)
        else:
            go_echo(f"No \u00a77{entity_type}\u00a7c found nearby (nothing within 16 blocks at all)",
                    error=True)
        return

    target_uuid = matches[0].uuid
    uuid_pattern = f".*{re.escape(target_uuid)}.*"
    go_echo(f"Following nearest \u00a77{entity_type}\u00a7f...")
    # "follow" has no fixed endpoint, so there's no meaningful completion
    # fraction to report - leave it at 0 and just surface what's being
    # followed; "#eta" will correctly report it can't estimate one.
    _progress_set(0.0, f"following {entity_type}")

    while True:
        _check_stop()
        found = minescript.entities(uuid=uuid_pattern, limit=1)
        if not found:
            go_echo(f"\u00a77{entity_type}\u00a7f is no longer around, stopping follow")
            break

        ex, ey, ez = found[0].position
        px, py, pz = player_position()

        if math.dist((px, py, pz), (ex, ey, ez)) <= arrive_dist:
            time.sleep(recheck_interval)
            continue

        cur = (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz)))
        bx, by, bz = int(math.floor(ex)), int(math.floor(ey)), int(math.floor(ez))
        goal = _find_stand_position_near(terrain, bx, by, bz)
        if goal is None:
            walk_y = _find_walkable_y_near(terrain, bx, by, bz)
            goal = (bx, walk_y, bz) if walk_y is not None else (bx, by, bz)

        if not run_to(terrain, cur, goal):
            time.sleep(recheck_interval)


def _find_walkable_y_near(terrain: TerrainCache, x: int, y_guess: int, z: int,
                           search_range: int = 24):
    """Scans up/down from `y_guess` for the nearest walkable y at (x, z)."""
    y_lo = getattr(terrain, "y_min", -64)
    y_hi = getattr(terrain, "y_max", 319)
    y_guess = max(y_lo, min(y_hi, y_guess))
    if terrain.is_walkable(x, y_guess, z):
        return y_guess
    for dy in range(1, search_range + 1):
        y_up, y_down = y_guess + dy, y_guess - dy
        if y_up <= y_hi and terrain.is_walkable(x, y_up, z):
            return y_up
        if y_down >= y_lo and terrain.is_walkable(x, y_down, z):
            return y_down
    return None


# ---------------------------------------------------------------------------
# Long-distance travel
# ---------------------------------------------------------------------------
#
# A single A* search straight to a far-off goal has to scan a bounding box
# roughly dx-by-dz blocks wide, and both the search itself (capped out
# around ~20k nodes/s) and get_block_region() (which gets slower the more
# distinct tiles it's asked to cover in one go) fall over long before that
# box gets anywhere close to covering the actual distance. Instead of
# handing find_path() the real goal when it's this far away, goto breaks
# the trip into LONG_DISTANCE_LEG_SIZE-block hops aimed along the straight
# line toward the goal - each hop is a normal, fast run_to() over a small
# area. If a hop's landing spot isn't walkable (or the hop itself fails),
# a handful of random nearby nodes are scanned first to build up the tile
# cache around that dead spot before retrying with a bit of jitter, rather
# than repeatedly banging into the exact same unreachable point.

LONG_DISTANCE_LEG_SIZE = 200              # max blocks walked per staged hop; longer trips are staged
LONG_DISTANCE_AREA_THRESHOLD = LONG_DISTANCE_LEG_SIZE * LONG_DISTANCE_LEG_SIZE  # legacy alias
LONG_DISTANCE_RANDOM_SAMPLES = 10         # random nodes scanned per cache-warm round
LONG_DISTANCE_RANDOM_RADIUS = 48          # radius (blocks) those random nodes are drawn from
LONG_DISTANCE_MAX_RETRIES = 6             # jittered retries before giving up on one hop's target


def _warm_random_nodes(terrain: TerrainCache, cx: int, cz: int, y_guess: int,
                        radius: int = LONG_DISTANCE_RANDOM_RADIUS,
                        samples: int = LONG_DISTANCE_RANDOM_SAMPLES):
    """Scans `samples` random (x, z) offsets within `radius` of (cx, cz),
    pulling their tiles into the terrain cache in parallel (via
    prefetch_positions) without walking anywhere. Used to build up cache
    around a leg target that turned out to be a dead end, instead of just
    retrying the exact same spot over and over."""
    positions = [
        (cx + random.randint(-radius, radius), y_guess, cz + random.randint(-radius, radius))
        for _ in range(samples)
    ]
    terrain.prefetch_positions(positions)


def _travel_far(terrain: TerrainCache, start, goal):
    """Walks from `start` to `goal` in LONG_DISTANCE_LEG_SIZE-block hops
    instead of running one full-distance A* search - see the block
    comment above. Each hop targets walkable ground roughly one leg's
    length along the straight line toward the goal; a hop whose target
    isn't walkable (or that fails outright) triggers a random-node cache
    warm-up around it before retrying with some jitter. Hands off to a
    plain run_to() once within one leg's distance of the real goal."""
    cur = start
    y_guess = start[1]
    total_dist = max(1.0, math.hypot(goal[0] - start[0], goal[2] - start[2]))
    legs_done = 0
    legs_failed = 0
    max_legs = int(total_dist / max(1, LONG_DISTANCE_LEG_SIZE)) + 2 * LONG_DISTANCE_MAX_RETRIES + 5

    while True:
        _check_stop()
        dx = goal[0] - cur[0]
        dz = goal[2] - cur[2]
        dist = math.hypot(dx, dz)

        if dist <= LONG_DISTANCE_LEG_SIZE:
            return run_to(terrain, cur, goal)

        frac = LONG_DISTANCE_LEG_SIZE / dist
        base_tx = cur[0] + dx * frac
        base_tz = cur[2] + dz * frac

        target = None
        for attempt in range(LONG_DISTANCE_MAX_RETRIES):
            _check_stop()
            jx = 0 if attempt == 0 else random.randint(-8, 8)
            jz = 0 if attempt == 0 else random.randint(-8, 8)
            tx = int(round(base_tx)) + jx
            tz = int(round(base_tz)) + jz
            ty = _find_walkable_y_near(terrain, tx, y_guess, tz)
            if ty is not None:
                target = (tx, ty, tz)
                break
            go_echo(f"Leg target \u00a77{tx}, {tz}\u00a7f isn't walkable yet - "
                    f"scanning nearby nodes to build up cache...")
            _warm_random_nodes(terrain, tx, tz, y_guess)

        if target is None:
            go_echo(f"Couldn't find walkable ground for the next leg after "
                     f"{LONG_DISTANCE_MAX_RETRIES} tries, aiming straight at the goal instead",
                     error=True)
            return run_to(terrain, cur, goal)

        remaining = int(dist)
        _progress_set(max(0.0, min(0.98, 1.0 - dist / total_dist)),
                      f"~{remaining} blocks remaining")
        go_echo(f"Long-distance travel: hopping toward \u00a77{target}\u00a7f "
                f"\u00a78(\u00a77~{remaining}\u00a78 blocks remaining\u00a78)")

        ok = run_to(terrain, cur, target)
        px, py, pz = player_position()
        cur = (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz)))
        y_guess = cur[1]
        legs_done += 1

        if not ok:
            legs_failed += 1
            go_echo("Couldn't complete that leg - scanning nearby nodes and retrying...", error=True)
            _warm_random_nodes(terrain, cur[0], cur[2], y_guess)
        else:
            legs_failed = 0

        if legs_done >= max_legs or legs_failed >= LONG_DISTANCE_MAX_RETRIES:
            go_echo(f"Giving up long-distance travel after \u00a77{legs_done}\u00a7f legs "
                    f"(\u00a77{legs_failed}\u00a7f consecutive failures)", error=True)
            return False


WANDER_DEFAULT_RADIUS = 48
WANDER_DEFAULT_LEGS = 10


def handle_wander(terrain: TerrainCache, start, args):
    """Walks random legs to build up the tile cache, using the same
    scan-then-walk pattern long-distance travel uses for dead spots:
    pick a random point, warm random nodes around it into the cache
    (via _warm_random_nodes / prefetch_positions), then run_to() it."""
    try:
        radius = int(float(args[0])) if len(args) >= 1 else WANDER_DEFAULT_RADIUS
    except ValueError:
        go_echo(USAGE, error=True)
        return
    try:
        legs = int(float(args[1])) if len(args) >= 2 else WANDER_DEFAULT_LEGS
    except ValueError:
        go_echo(USAGE, error=True)
        return
    if radius < 8:
        go_echo(f"Radius clamped up to \u00a778\u00a7f blocks (got \u00a77{radius}\u00a78)")
        radius = 8
    if legs < 1:
        go_echo(f"Legs clamped up to \u00a771\u00a7f (got \u00a77{legs}\u00a78)")
        legs = 1

    go_echo(f"Wandering \u00a77{legs}\u00a7f legs within \u00a77~{radius}\u00a7f blocks, "
            f"warming cache as I go...")
    cur = start
    y_guess = start[1]
    for i in range(legs):
        _check_stop()
        _progress_set(i / legs, f"wander leg {i + 1}/{legs}")
        tx = cur[0] + random.randint(-radius, radius)
        tz = cur[2] + random.randint(-radius, radius)
        go_echo(f"Wander leg \u00a78[\u00a77{i + 1}/{legs}\u00a78]\u00a7f: "
                f"scanning nearby nodes around \u00a77{tx}, {tz}\u00a7f to build up cache...")
        _warm_random_nodes(terrain, tx, tz, y_guess)
        ty = _find_walkable_y_near(terrain, tx, y_guess, tz)
        if ty is None:
            go_echo(f"No walkable ground near \u00a77{tx}, {tz}\u00a7f, skipping leg")
            continue
        target = (tx, ty, tz)
        ok = run_to(terrain, cur, target,
                    progress_base=i / legs, progress_span=1.0 / legs,
                    progress_label=f"wander {i + 1}/{legs}")
        px, py, pz = player_position()
        cur = (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz)))
        y_guess = cur[1]
        if not ok:
            go_echo("Couldn't complete that leg - scanning nearby nodes and retrying...",
                     error=True)
            _warm_random_nodes(terrain, cur[0], cur[2], y_guess)

    go_echo(f"Wander done \u00a78(\u00a77{terrain.tiles_fetched}\u00a78 tiles live, "
            f"\u00a77{terrain.tiles_from_disk_cache}\u00a78 from cache\u00a78)")


def dispatch(terrain: "TerrainCache", start, args):
    """Runs a single # subcommand (goto/mine/follow/wander/waypoints/settings)
    against an already-open TerrainCache. Shared by the one-shot CLI path
    (main(), args passed on the command line) and the background chat
    daemon (run_chat_daemon(), args parsed from a "#..." chat message).
    "stop"/"eta" are daemon-only and handled inline by run_chat_daemon(),
    not here."""
    if not args:
        go_echo(USAGE)
        return

    cmd = args[0].lower()

    if cmd == "settings":
        handle_settings(args[1:])
        return

    if cmd in ("waypoints", "wp"):
        handle_waypoints(terrain, start, args[1:])
        return

    if cmd == "mine":
        handle_mine(terrain, start, args[1:])
        return

    if cmd == "follow":
        handle_follow(terrain, args[1:])
        return

    if cmd == "goto":
        handle_goto(terrain, start, args[1:])
        return

    if cmd == "wander":
        handle_wander(terrain, start, args[1:])
        return

    go_echo(USAGE)


def _run_task(args):
    """Runs one dispatch() call on a background thread, for the chat
    daemon. Opens/closes its own TerrainCache and clears _stop_event when
    done (whether it finished, errored, or was stopped) so the next "#..."
    command is free to start."""
    _progress_begin(args[0] if args else "task")
    try:
        px, py, pz = player_position()
        start = (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz)))
        terrain = TerrainCache()
        try:
            dispatch(terrain, start, args)
        finally:
            terrain.close()
    except TaskStopped:
        go_echo("Stopped.")
    except Exception as e:
        go_echo(f"Task failed: {e}", error=True)
    finally:
        _stop_event.clear()
        _progress_end()


def _run_task_chain(args):
    """Runs `args`, then - if "allow_queueing_tasks" left anything waiting
    in _task_queue - pulls the next one off the front and runs that too,
    and so on, all on this same background thread. This is what makes
    queued tasks actually start on their own once the current one (and
    anything queued ahead of it) finishes: _task_thread stays "alive"
    across the whole chain, so it doesn't need the daemon's event loop to
    notice a task ended and kick off the next one itself."""
    _run_task(args)
    while True:
        with _task_queue_lock:
            nxt = _task_queue.popleft() if _task_queue else None
        if nxt is None:
            return
        go_echo(f"Starting queued task: \u00a77{' '.join(nxt)}")
        _run_task(nxt)


def _start_task(args):
    global _task_thread
    _stop_event.clear()
    _task_thread = threading.Thread(target=_run_task_chain, args=(args,), daemon=True)
    _task_thread.start()


def run_chat_daemon():
    """Listens for chat messages sent with a '#' prefix (intercepted
    before they're actually sent, same as the old '#' command line but
    without blocking chat while a task runs) and dispatches them as #
    commands on a background thread, e.g. "#goto 100 64 100" or
    "#mine iron_ore 32".

    Only one task runs at a time. By default, typing another "#..."
    command while one is in flight is refused; setting
    "allow_queueing_tasks" to true instead queues it (FIFO) - see
    _run_task_chain() above for how queued tasks actually get picked up.
    "#stop" sets _stop_event, which every long-running loop in this file
    (the A* search, block-mining polls, path-walking, follow, explore)
    checks so the task can unwind cleanly - releasing any held movement/
    attack keys - instead of leaving the bot standing there swinging or
    holding forward. "#stop" also clears anything still queued, since a
    request to stop is taken as "stop everything", not just the one task
    currently running."""
    global _task_thread

    load_settings()

    with minescript.EventQueue() as eq:
        eq.register_outgoing_chat_interceptor(prefix="#")
        while True:
            event = eq.get()

            if event.type == minescript.EventType.OUTGOING_CHAT_INTERCEPT:
                message = getattr(event, "message", None) or ""
                if message.startswith("#"):
                    minescript.append_chat_history(message)
                    message = message[1:]
                try:
                    args = shlex.split(message)
                except ValueError:
                    args = message.split()

                if not args:
                    go_echo(USAGE)
                    continue

                if args[0] == "stop":
                    if _task_thread is not None and _task_thread.is_alive():
                        with _task_queue_lock:
                            queued = len(_task_queue)
                            _task_queue.clear()
                        extra = f" and cleared \u00a77{queued}\u00a7f queued" if queued else ""
                        go_echo(f"Stopping current task...{extra}")
                        _stop_event.set()
                    else:
                        go_echo("Nothing running")
                    continue

                if args[0] == "eta":
                    # Handled inline, same as "stop" - it just reads shared
                    # progress state, so it works even while a task is
                    # already running on _task_thread.
                    handle_eta()
                    continue

                if _task_thread is not None and _task_thread.is_alive():
                    if _as_bool(SETTINGS.get("allow_queueing_tasks", False)):
                        with _task_queue_lock:
                            _task_queue.append(args)
                            position = len(_task_queue)
                        go_echo(f"Queued \u00a78(\u00a77position {position}\u00a78)\u00a7f: "
                                f"\u00a77{' '.join(args)}")
                    else:
                        go_echo("A task is already running - '#stop' it first "
                                "\u00a78(\u00a77allow_queueing_tasks\u00a78 setting can queue these instead)",
                                error=True)
                    continue

                _start_task(args)

            try:
                current_input = minescript.chat_input()[0]
            except Exception:
                current_input = ""
            try:
                if current_input.startswith("#"):
                    minescript.set_chat_input(color=0xCCCCCC)
                else:
                    minescript.set_chat_input(color=0xFFFFFF)
            except Exception:
                pass


def main():
    args = sys.argv[1:]

    # Set up minescript/pf (+ cache subfolder) and write pf_pref.json with
    # defaults on first run; on later runs this just loads what's there.
    load_settings()

    if not args:
        # No CLI args - run as the long-lived chat daemon (see
        # run_chat_daemon() above): "\go" with nothing after it now starts
        # listening for "#command" chat messages instead of just printing
        # the usage string.
        run_chat_daemon()
        return

    if args[0] == "settings":
        handle_settings(args[1:])
        return

    px, py, pz = player_position()
    start = (int(math.floor(px)), int(math.floor(py)), int(math.floor(pz)))

    terrain = TerrainCache()
    try:
        dispatch(terrain, start, args)
    finally:
        terrain.close()


if __name__ == "__main__":
    try:
        main()
    except TaskStopped as e: pass
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        go_echo(f"Fatal error: {tb_str}\nStopping...", error=True)

# To minify for chat-paste limits: python -m python_minifier go.py (third-party tool, optional).
