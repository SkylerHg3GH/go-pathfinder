# go-pathfinder

A* pathfinding bot for [Minescript](https://minescript.net/) 

## Requirements

- Minecraft: Java Edition
- `go.py` in your Minescript scripts folder

## Install

1. Copy `go.py` to your Minescript folder (the one with `system/lib/minescript.py`).
2. In-game, run:
   ```
   \go
   ```
   This starts the background daemon. It listens for chat messages starting with `#` (intercepted before they're sent, so nothing goes public).
3. Then just type in chat, e.g.:
   ```
   #goto 100 64 100
   #mine iron_ore 32
   #stop
   ```

> One-shot CLI mode also works: `\go goto 100 64 100`, `\go settings list`, etc. But the daemon + `#...` chat commands is the normal way.

## Commands

Prefix every command with `#` in chat. `#` alone prints help. `#eta` reports progress, `#stop` cancels the running task (and clears the queue).

| Command | What it does | Example |
|---|---|---|
| `# goto X Y Z` | Walk to coordinates (staged in legs if far) | `#goto 100 64 -200` |
| `# goto BLOCK_ID` | Find nearest block and walk to it | `#goto diamond_ore` / `#goto minecraft:oak_log` |
| `# mine BLOCK_ID [MAX_COUNT]` | Repeatedly path to + mine a block type (default 64) | `#mine iron_ore 32` |
| `# follow ENTITY_TYPE` | Follow nearest matching entity (substring/regex) | `#follow zombie` / `#follow villager` |
| `# wander [RADIUS] [LEGS]` | Random walks to explore / warm the cache | `#wander 64 5` |
| `# down <Y>` | Dig straight down to feet-Y, one level at a time | `#down 11` |
| `# pillar <Y>` | Tower straight up to feet-Y using hotbar blocks | `#pillar 90` |
| `# wp add NAME [X Y Z]` | Save waypoint (no coords = current pos). `new` = alias | `#wp add home` / `#wp add mine 100 11 -200` |
| `# wp to NAME` | Walk to a waypoint (`goto` = alias) | `#wp to home` |
| `# wp remove NAME` | Delete a waypoint (`rm` / `delete` = alias) | `#wp remove home` |
| `# wp list` | List this world's waypoints | `#wp list` |
| `# wp clear` | Delete all waypoints for this world | `#wp clear` |
| `# settings ...` | View / edit tunables (see below) | `#settings list` |
| `# debug` | Dump vars + state to `minescript/pf/debug/DDMMYY-HHMMSS.txt` | `#debug` |
| `# eta` | Rough progress / time-of-arrival for current task | `#eta` |
| `# stop` | Cancel current task + clear queued tasks | `#stop` |

Block IDs are lowercase `minecraft:name`, `[blockstate]` stripped. The `minecraft:` prefix is optional (`iron_ore` = `minecraft:iron_ore`).

Queueing: by default a second `#...` while one runs is refused. Set `allow_queueing_tasks=true` to queue them FIFO instead.

## Settings

Stored per-install in `minescript/pf/pf_pref.json`. Edit in-game:

```
#settings list [PAGE]
#settings get KEY
#settings set KEY VALUE
#settings add KEY VALUE      (list settings, one value)
#settings remove KEY VALUE   (list settings, one value)
#settings unset KEY          (reset to default)
#settings clear KEY          (empty a list setting)
#settings toggle KEY         (bool settings)
```

| Key | Default | Notes |
|---|---|---|
| `allow_breaking_blocks` | `true` | If `false`, never mines; `#down` refuses |
| `allow_placing_blocks` | `true` | If `false`, never towers / `#pillar` refuses |
| `allow_sprinting` | `true` | Sprint on straight legs |
| `allow_queueing_tasks` | `false` | Queue `#...` commands FIFO instead of refusing |
| `precise_landing` | `false` | Slower, tighter arrival check |
| `mine_penalty` | `6.0` | Cost of mining one obstacle block vs walking (~1.0/step) |
| `walk_penalty` | `0.0` | Extra cost per step |
| `jump_penalty` | `0.0` | Extra cost per step-up |
| `turn_penalty` | `0.4` | Extra cost per direction change |
| `weight` | `1.1` | A* heuristic weight. `1.0` = optimal, `>1` = faster/greedier |
| `reach` | `4` | Mining reach in blocks (eyes to block center) |
| `tile_scan_size` | `12` | Terrain tile size for lazy `get_block_region` reads |
| `threads_for_searching` | `cpu_count` | Clamped 2–32 |
| `max_fall_distance` | `4` | Max drop `#down` / pathing will accept |
| `waypoint_tolerance` | `0.6` | How close (xz) counts as "arrived" |
| `per_block_timeout` | `10.0` | Seconds per block before walk leg gives up |
| `max_cache_size` | `1000000` | Soft cap (bytes) on on-disk tile cache |
| `cache_expiration_duration` | `7` | Days before a cached tile is re-fetched. `0` = off, `-1` = never expires |
| `avoid_stepping_on` | `[]` | Block IDs to never stand on |
| `avoid_mining` | `[]` | Block IDs to never mine through (`#mine` target overrides) |
| `passables` | `[]` | Extra block IDs treated as walk-through |
| `passable_block_substrings` | `[]` | Extra substrings treated as walk-through |

List setters take comma lists: `#settings set avoid_mining chest,furnace`. Add/remove take one value at a time.

## Files

```
minescript/
  pf/
    pf_pref.json      # settings (created on first run)
    waypoints.json    # per-world waypoints
    blockitems.json   # per-MC-version placeable list (auto-fetched)
    cache/            # per-world terrain tile caches
    debug/            # #debug dumps
```

1.1 <br>
*this readme is ai generated im so sorry*

