# Roblox Headless Multi-Instance Launcher

Launches your **main** account in a normal, visible Roblox client (the "GUI"),
and your **alt** accounts in **hidden / headless** Roblox clients — all at the
same time on one Windows PC.

**Default flow (follow-main mode) — no Place ID needed:**

```
1. Click "Launch All"
2. Main account starts now  ──►  visible Roblox client (home screen);
                                   you don't type a Place ID
3. Pick any game inside the client and join it (as you normally would)
4. Detected via Roblox presence API (every ~1s) ──► alts launch (hidden)
   and join the EXACT same server you're in (same job ID)
5. ...leave the game... ──► alts close automatically
6. ...join another game... ──► alts join you there again
7. Close the main client window ──► all alts close, follow stops
8. If an alt ever disconnects (Roblox closes it, it crashes, it gets kicked)
   ──► detected within ~1 second and it reconnects into your current server
```

- Main ──► visible GUI window (alts follow it continuously)
- Alt1 ──► hidden (headless) client, joined into main's server
- Alt2 ──► hidden (headless) client, joined into main's server

There is also a **simultaneous mode** where all accounts join a configured
place at once (this one needs a Place ID in `config.json`).

---

## Requirements

| Requirement | Notes |
|---|---|
| Windows 10/11 | the Win32 code in this program is Windows-only |
| Python 3.8+ | any python.org install; **no extra packages needed** (stdlib only) |
| Roblox Player | installed from **https://www.roblox.com/download** (web installer). The Microsoft-Store version is **not** supported |
| One `.ROBLOSECURITY` cookie per account | yours only — see below |

---

## 1. Add your accounts with the GUI (automatic config)

Run the program and click **Add Account**:

- **Login with browser (capture cookie)…** — this is the easy path. A real
  Microsoft Edge / Google Chrome window opens at `roblox.com/login` for that
  account. Log in (complete 2FA if asked) and the `.ROBLOSECURITY` cookie is
  captured from that session automatically — no copy/pasting required. The
  browser and its temp profile are disposed of afterwards, and the cookie
  never leaves your machine.
- Or paste a `.ROBLOSECURITY` cookie manually (how to get one below).
- Choose **Main (visible GUI)** or **Alt (hidden / headless)**.

Everything (accounts, place, mode) is saved to `config.json` automatically —
no manual editing needed. You can also hand-edit `config.json` or start from
`config.example.json`.

### Switching accounts on and off

Every account has a **☑ / ☐ checkbox** in the first column:

- **Click the checkbox** to toggle that one account.
- **Select rows and press Space** (or Enter) to toggle them together.
- **Off accounts are greyed out and skipped** by *Launch All* — a disabled alt
  is never launched, and if it was already running it gets closed and won't be
  relaunched.
- **On/Off Selected**, **All On** and **All Off** buttons do the same for the
  selection or the whole list.

This is saved per account as `"enabled": true/false` in `config.json`. Accounts
saved before this option existed have no such key and are treated as **on**, so
nothing disappears when you update.

### Editing an account

Select a row and click **Edit Selected** to change its name, role (main/alt),
headless flag, cookie, job ID, or enabled state. Renaming an account that is
currently running keeps the launcher attached to the same client. Selecting
several rows and clicking **Edit Selected** offers a batch rename instead.

### Getting a cookie manually (optional)

1. Log into the account at https://www.roblox.com in your browser.
2. Press `F12` (DevTools) → **Application** tab → **Cookies** → `https://www.roblox.com`.
3. Double-click the value of the row named **`.ROBLOSECURITY`** and copy it (Ctrl+C).
   Keep it private — anyone with this value can log in as you.

---

## 2. Configure (config.json)

The GUI writes this for you. For reference:

```jsonc
{
  "placeId": "",                  // optional in follow mode; required in simultaneous
  "launchMode": "follow",        // "follow" (default) or "simultaneous"
  "joinTimeoutSec": 900,         // kept for compatibility; follow mode now tracks continuously
  "jumpIntervalSec": 10,         // seconds between anti-AFK jumps per alt; 0 = disabled
  "jumpMethod": "focus",         // "focus" (default, invisible) or "message" (no focus at all)
  "jumpSkipWhenMainFocused": true, // hold jumps off while your main client is the active window
  "jumpMaxGapSec": 180,          // ...but never leave alts unjumped longer than this (0 = never)
  "altThrottle": true,           // run alts as background work (EcoQoS + low priority)
  "noRender": false,             // also minimize alts (stops rendering -> much less CPU)
  "serverQueueRetrySec": 15,      // alt in the wrong server? close it and re-queue (0 = accept any)
  "joinMethod": "api",            // "api" (works) or "pageurl" (measured: does not join)
  "statusOverlay": true,          // on-screen top-right readout, click-through
  "statusOverlayAlpha": 210,      // 20-255
  "altPresenceCheckSec": 3,       // presence poll while something is happening (one batched request covers all alts)
  "altPresenceIdleSec": 20,       // ...and when everything is stable, to stay under the API rate limit
  "dropConfirmCount": 2,          // consecutive bad readings before an alt is closed/requeued
  "dropGraceSec": 15,             // ignore drops for this long after joining (presence lags)
  "rejoinPauseDrops": 6,          // this many drops inside the window -> pause rejoin
  "rejoinPauseWindowSec": 120,    // ...counted over this window
  "rejoinPauseSec": 60,           // ...pauses rejoin for this long
  "relaunchKillWaitSec": 2,       // wait for a dropped client's processes to die before relaunching
  "closeExistingRoblox": false,  // true = close running clients before launching
  "accounts": [
    { "name": "Main", "role": "main", "headless": false, "enabled": true,
      "cookie": "REPLACE_ME", "jobId": null },
    { "name": "Alt1", "role": "alt",  "headless": true, "enabled": true,
      "cookie": "REPLACE_ME", "jobId": null }
  ]
}
```

> `"enabled"` is optional — it controls the ☑/☐ checkbox in the GUI. Missing
> means **on**, so hand-edited or older configs keep working unchanged.

- `launchMode: "follow"` — alts wait until the main is **in a game**, then join
  the **exact same server** (same job ID).
- `launchMode: "simultaneous"` — all accounts join the configured place at once.
- `role: "main"` → always launched visible.
- `role: "alt"` → launched hidden (headless).
- `headless: true` overrides the role and forces the window hidden.
- `jobId` (optional) → join a specific server (the long `jobId` from `?gameId=`).
- `jumpIntervalSec` → every N seconds each alt briefly focuses its window and
  jumps (Space) to avoid AFK kicks. The jump is invisible: the window is shown
  only as a fully transparent (alpha 0) layered window, so no window ever
  appears, and focus is returned to your previous window right after. `0`
  disables it. The default is 10.
- `jumpMethod` → how the jump is delivered:
  - `"focus"` (default) — **the only method that works.** A real injected key
    can only be delivered to the foreground window, so the alt is shown as a
    fully transparent (alpha 0) layered window, focused for a few milliseconds,
    given a real Space, then hidden again and focus handed back. Nothing is ever
    drawn on screen.
  - `"message"` — posts Space straight to the window, never taking focus. Kept
    selectable, but **it does not work with Roblox — this was measured, not
    assumed.** See *Limitations* for the full matrix. Leave it on `"focus"`.
- `jumpSkipWhenMainFocused` → **this is the real answer to "don't unfocus my
  main"**, and it matters more than `jumpMethod`. With it on, jumps are **held
  off entirely while the main client is your active window**, and resume the
  moment you switch away. Your main then never loses focus — not "briefly", not
  ever. Windows promotes anything that takes focus to foreground, and that
  cannot be bypassed (measured — see *Limitations*), so *not jumping while you
  are playing* is the only way to guarantee zero interference.
  Set to `false` and your main will flicker every `jumpIntervalSec`.
- `jumpMaxGapSec` → safety net for the above: if alts haven't been jumped for
  this long, one jump happens even if the main is in use, so they can't idle
  out. Default `180` (3 minutes). `0` = never jump while the main is focused
  (zero interference, but alt AFK kicks become possible if you play for hours).
- `altThrottle` → runs alt clients as **background work**: Windows EcoQoS
  (`SetProcessInformation`/`PROCESS_POWER_THROTTLING_EXECUTION_SPEED`) plus
  `BELOW_NORMAL` priority, so their hidden rendering is scheduled onto
  efficient cores / lower frequency and yields to whatever you're doing. The
  main is never throttled. Default `true`.
- `altFramerateCap` → **experimental, off by default (`0`)** — runs alts at a
  capped frame rate. The client accepts `-g /globalBasicSettingsPath`, so the alt
  would be launched with **its own `GlobalBasicSettings_13.xml`** (a copy of
  yours with only `FramerateCap` / `SavedQualityLevel` changed), leaving your
  main's settings alone.
  **Why it's off by default:** that switch is only reachable by launching
  `RobloxPlayerBeta.exe` directly, and a directly-spawned client **loses
  Roblox's singleton-mutex race** against the instances already running — its
  log fills with `waitForNewPlayerProcess ... WAIT_TIMEOUT` and the alt never
  joins (observed here: 1 of 5 alts joined). The normal `roblox-player:` URL
  launch doesn't hit that. Leave this at `0` unless you are testing with a
  single client.
- `joinMethod` → **how an alt is put into the MAIN's exact server.**
  - `"api"` (default) — the launcher's own endpoint,
    `assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGameJob&…&gameId=<jobId>`.
    This asks for one specific job, but Roblox may answer with a *different*
    server when the target is full.
  - `"pageurl"` — hands the client the server's own page URL,
    `https://www.roblox.com/games/<placeId>/<jobId>` (the "Copy Server Link" /
    join-a-friend route). **This was implemented and measured, and it does not
    work**: the client fails to join entirely, because that is a web page, not
    something it can use as a launch target. Roblox's page carries no launch URL
    to extract either — it has zero `roblox-player:`/`assetgame` references and
    the jobId is not even present in the served HTML (the join is done by
    client-side JavaScript). It is kept selectable only for reference. Leave it
    on `"api"`.

  **The join outcome is measured, not assumed.** `PlaceLauncher` refuses calls
  from outside the Roblox client (`{"isValid":false}` with a cookie, with an
  auth header, and with both), so the pin cannot be probed ahead of time. But it
  can be *observed* a few seconds after each join, because the alt's presence
  reports the server it is actually in. The launcher therefore logs the outcome
  of every join and keeps a tally:

  ```
  [AltA] landed in the main's server — the join was pinned (7 pinned / 0 corrected).
  [AltB] landed in a DIFFERENT server (…) than the main's (…) — correcting (7 pinned / 1 corrected).
  ```

  So after a session you can see the real rate instead of guessing, and a run of
  "corrected" lines means `serverQueueRetrySec` is doing the work that the join
  request should have done.
- `statusOverlay` → an on-screen readout in the **top-right corner**, showing one
  line per enabled account: its name, the **server it is actually in** (the one
  presence last reported, not merely the one we asked for), and its state —
  `joined`, `joining`, `queued`, `paused` or `disconnected`, each with a
  coloured dot.

  It is **click-through and never takes focus**: `WS_EX_TRANSPARENT` plus
  answering `WM_NCHITTEST` with `HTTRANSPARENT` means clicks go to the game
  underneath, and `WS_EX_NOACTIVATE` means it can never become the foreground
  window — which matters here, because the anti-AFK jump decides where to send
  focus and an overlay that could activate would interfere with it. It is also
  `WS_EX_TOOLWINDOW`, so it stays out of the taskbar and Alt-Tab.
  Transparency is a fixed alpha (`statusOverlayAlpha`, default `210`) applied by
  Windows, which keeps the text properly anti-aliased.
  Toggle it from the launcher with the **status overlay** checkbox, or set
  `statusOverlay` in `config.json`. Disabled accounts are not listed.
- `serverQueueRetrySec` → **keeps alts in YOUR server instead of any server**
  (default `15`, `0` = accept whatever server Roblox picks). Roblox has no real
  "wait in line" for a full server: when you request the main's server by ID and
  it's full, the client gets quietly moved into a *different* server of the same
  place — which looks healthy, so the alt just stays there. With this on, the
  health monitor compares each alt's server ID to the main's, and if they
  differ it **closes that alt** (so it stops squatting in the wrong server) and
  re-requests the main's exact server after a backoff — 15s, 30s, 60s, 120s, then
  capped — until a slot frees up. It deliberately does *not* count as a
  "disconnect", so a busy server can't trigger the 3-drops anti-thrash pause.
  It also means that if the main moves to another server, the alts follow it.
  The alt shows as **queued** in the GUI while it waits.

### How fast an alt comes back

Two different failures, detected two different ways:

- **The client process is gone** (crashed, closed, killed). This is checked
  locally every second, so it is acted on immediately — no API call involved.
- **The client is alive but out of the game** (kicked, or the server dropped
  it). This can only be seen from Roblox's presence service, so it costs an API
  call. Every alt that is due is now checked in **one batched request** rather
  than one request per alt, which is what makes this cheap.

  That API has a hard rate limit, though, and asking on every one-second health
  tick (~60 requests/minute) gets the launcher throttled. Because a throttled
  check is required to decide *nothing*, being throttled takes the whole health
  net offline silently — no kick detection, no wrong-server correction, and no
  error anywhere. That is exactly what happened: alts sat in the wrong server
  and were never corrected. So the poll rate is **adaptive**, with a hard floor
  between calls:
  - something is happening (an alt joined or dropped in the last 60s, or a
    server mismatch is being corrected) → every `altPresenceCheckSec` (`3`s);
  - everything stable → every `altPresenceIdleSec` (`20`s), i.e. ~3 requests a
    minute instead of ~60;
  - any 429 → back off 30s, 60s, 120s, up to 5 minutes, so a throttled run
    settles instead of feeding the limit; those warnings log at most once every
    2 minutes.

  A reading is only acted on after `dropConfirmCount` consecutive bad readings
  (`2` by default) — a single stale or blipped reading must never start a
  close-and-relaunch cycle.

  Most importantly, **absence is only evidence of a drop once we have actually
  seen that alt in a game since it joined.** Roblox's presence service lags
  reality: for tens of seconds after a client enters a server it can still
  report "not in game". Judging on a timer alone is a guess, and a wrong guess
  closes a perfectly healthy client — which is exactly what happened here when
  the post-join grace was shortened: alts survived 16–17s per pass (the grace
  plus two confirmations) where the same setup had previously given 61–96s, and
  the launcher was killing its own alts and logging them as "kicked from game".
  The evidence-based rule fixed it; `dropGraceSec` (`30`s) is now only a
  secondary floor, not the primary guard.

  That rule has a hole, though, and the status overlay found it: an alt whose
  join **never succeeds** can never satisfy it. Presence showed four of six alts
  as `Offline` while the log said "joined main's server" for every one of them —
  because that line is printed when the *client process starts*, not when it
  joins. Never seen in-game → absence never counts → nothing ever retried. A
  full server refuses the join (this place caps at **7 players**, and one main
  plus six alts is exactly seven), and the alt sat there forever as a live
  client that was not in the game.

  So the not-in-game branch has a second exit. Past `joinConfirmSec` (`90`s from
  the launch) with `dropConfirmCount` consecutive definitive readings agreeing,
  the join itself has failed and it is relaunched. It is still evidence-based,
  not a timer: inside the window presence lag is tolerated indefinitely, and one
  odd reading never starts a close/relaunch cycle.

### `userPresenceType` is not the discriminator — `gameId` is

  A live log line gave it away:

      [hhhhhhohkk3] confirmed in a game by presence after 1s.

  A Roblox client cannot be in a game one second after starting. Dumping the
  endpoint's payload explains why:

      main         type=2  gameId=fdba3109-…  placeId=107778070777162  "Steal An Egg"
      hhhhhhohkk3  type=2  gameId=null        placeId=null             ""

  Roblox also returns `type=2` ("InGame") with `gameId`, `placeId` **and**
  `lastLocation` all null — a phantom left over from an earlier session. There
  is no timestamp field anywhere in the payload, so freshness cannot be proven
  at all. A genuine in-game reading always carries a `gameId`, so that is the
  evidence, and readings are now classified three ways:

  | verdict | meaning | used for |
  |---|---|---|
  | `in` | in a game, and we know which server | confirms arrival, wrong-server detection |
  | `out` | definitively not in a game | kick detection, failed-join retry |
  | `unknown` | Roblox said something unusable | **decides nothing** |

  A phantom is not proof of arrival *and* not an absence, so it can neither
  confirm a join nor close a client.

### "no signal" — telling "still joining" apart from "we are blind"

  Presence answers with HTTP 429, and a throttled check decides nothing. Every
  alt then sat on `joining` indefinitely, which is indistinguishable from an
  alt that really was still loading — so a launcher that had stopped being able
  to see looked like one that was merely slow. `unknown` now has its own
  overlay state, **`no signal`**, and every early return from the sweep
  (429, network error, empty reply) records it.

### Request rate

  `altPresenceCheckSec` `3` → **`6`**, `altPresenceIdleSec` `20` → **`25`**.

  The old "settling" test was *any join in the last 60s*, and `_join_since` is
  never pruned — so with six alts launching over 90 seconds it was true for the
  whole session, and the launcher ran at the fast rate continuously. That is
  what earned the 429 in the log (`pausing presence checks 30s`) that left every
  alt unable to be confirmed. The fast rate now applies only while an alt is
  genuinely **awaiting confirmation** — launched, and not yet proven in a game
  — bounded by `joinConfirmSec` so an alt that cannot join at all (a full
  server) cannot pin the launcher at the fast rate forever.

## The client writes down which server it joined — read that instead

  This is the one that actually explained "the alts join a random server". From
  one live session:

      12:35:20  [follow] hhhhhhohkk joined a game — launching alts (job fdba3109-…)
      12:35:51  ! Joining game 'c2995992-ae7b-42ac-b5b5-a35245cf4e53' place 107778070777162

  The launcher aimed six alts at `fdba3109` **31 seconds before** the main
  actually joined `c2995992`. `fdba3109` was the *previous session's* server:
  the main's client had been closed and relaunched, and presence went on
  reporting the old job. Every alt was then pinned **perfectly — to the wrong
  server.** Nothing was random. The launcher was simply aiming at a server the
  main had already left.

  So the authority on "which server is this account in" is now the client's own
  log, which records the join as it happens:

      ! Joining game '<jobId>' place <placeId> at <ip>

  It is first-hand, needs no API call, cannot be cached, cannot be throttled,
  and cannot be stale. It settled a long-running question for free too:
  `RequestGameJob` **does** honour the requested server — five of six alt logs
  contained the main's exact job id and server IP. The `gameId=` parameter was
  never the problem.

  | source | now used for |
  |---|---|
  | client log | arrival, which server, wrong-server correction, failed-join retry |
  | presence API | kicks only — leaving a server leaves no log line, so absence of a line proves nothing |

  Logs carry neither pid nor account name, so a client is matched to its log by
  **boot time** (the first timestamped line), taken from the moment we launched
  it. The mapping is cached, and invalidated on every relaunch, because a
  reused mapping would point an account at its predecessor's server — the exact
  staleness being eliminated. Reads scan backward from the end of the file and
  stop at the first match, so an 8 MB log resolves in ~20 ms.

  Presence remains as a fallback: if the log directory is missing or unreadable,
  everything still works, just without the improvement.

## Anti-AFK: why a posted key can never work (and what was still wrong with it)

  `jumpMethod: "message"` posts `WM_KEYDOWN` to the alt's window, which needs
  no focus at all. It has been retried, and the reason it cannot work is now
  proven rather than assumed.

  A `WH_KEYBOARD_LL` hook (the `LowLevelKeyboardProc` callback) is called only
  for real keyboard input — from the driver, or injected via `keybd_event` /
  `SendInput`. A harness that installs a system-wide hook and a real window,
  then sends each kind of input, gives a two-sided result:

  | sent | delivered to the WndProc | seen by the keyboard hook |
  |---|---|---|
  | `keybd_event` (a real key) | no | **yes** |
  | `PostMessage(WM_KEYDOWN)` | **yes** | **no** |
  | `SendMessage(WM_KEYDOWN)` | **yes** | **no** |

  So the posted message really is delivered and really is well-formed — it
  simply never becomes a keyboard event. Roblox reads raw input, and raw input
  only ever comes from a real key event, and a real key event is delivered by
  the system to the **foreground** window. There is no API to aim one at a
  different `HWND`. That is why the working method has to bring the alt to the
  foreground for a few milliseconds.

  Two genuine bugs were found while retrying it:

  * the key-down `lParam` was `0x00390001`, omitting bit 30 (previous key
    state), so a window that decoded it had no reason to treat it as a key. It
    is now `0x40390001` (key-up `0xC0390001`).
  * the key-up was gated on `SendMessageW`'s return value — but a window
    procedure legitimately returns `0` for `WM_KEYDOWN`, so **every key-up was
    silently swallowed** and the key was left logically held down.

  And one that the *existing* overlay test caught, in newly added code:
  `SendMessageW`'s return is `LRESULT`, which is **signed**. Declaring it
  `c_size_t` turned `HTTRANSPARENT` (-1) into `18446744073709551615` and broke
  every hit test depending on it. The undeclared version was truncating the
  64-bit `HWND` instead, so both the `argtypes` and the signed `restype` were
  needed.

  The method now also tries a second window state that had never been tested:
  fully **opaque but parked at `-32000,-32000`**, where Windows still treats it
  as an ordinary visible window but no user can see it. The earlier finding was
  never "posting does not work" — it was "hidden and transparent windows are
  ignored", which leaves this variant open.

  **It still cannot be verified quickly.** The client log records no input at
  all, so the only ground truth is whether Roblox eventually kicks the alt for
  idling, which takes many minutes. Judging it takes a full idle cycle with
  `jumpMethod: "message"` and the alts otherwise untouched.

### …and it turns out the off-screen variant partly works

  Measured, first-hand: the transparent variant never made the character jump.
  The **opaque-but-off-screen** variant (`-32000,-32000`, still an ordinary
  visible window to Windows) jumps for the first few attempts and then stops.
  So the mechanism has traction — Roblox is reading ordinary window messages
  after all — and something is degrading rather than the path being wrong.

  Two concrete causes, both in this codebase:

  * **`SW_RESTORE` was sent before `SW_SHOW`.** These alts sit minimized *and*
    hidden (`hide_window` does `SW_MINIMIZE` then `SW_HIDE`), and restoring a
    window that is not visible is unreliable — it can be accepted and leave the
    window iconic. A message posted to an iconic window **is delivered and
    silently ignored**, which is the original "posting does not work" symptom
    arriving intermittently. The order is now show-then-restore, and the result
    is **verified** (`IsIconic` / `IsWindowVisible`) with up to
    `_JUMP_RESTORE_ATTEMPTS` retries before anything is sent. If the window still
    cannot be presented, the jump reports failure and says so instead of
    pretending to have worked.
  * **The watcher was racing it.** The watcher re-minimizes every visible alt
    window every 2 seconds, so it could re-minimize an alt mid-send. The watcher
    now skips any account with a jump in flight.

  Each attempt also reports what it actually observed (`restore_attempts`,
  `iconic`, `visible`, `reason`), so if it still stops after a few jumps the log
  will say whether the window refused to present itself or the message went
  somewhere useless.

### …and the fix for that made it steal focus. `SW_SHOW` is the wrong call.

  Reported: with the show-then-restore reorder, `message` started behaving
  exactly like `focus` — it took the focus — *and* stopped jumping. One cause,
  and it was mine.

  `SW_SHOW` is documented as *"Activates the window and displays it"*. These
  alts have to be brought back before a message means anything, and the call
  used to bring them back was activating. Measured on real windows that are
  genuinely minimized+hidden, with another window genuinely in the foreground:

  | call | brings the window back | keeps the user's focus |
  |---|---|---|
  | `SW_SHOW` (5) | **no** | **no** — stole it |
  | `SW_RESTORE` (9) | yes | **no** — stole it |
  | **`SW_SHOWNOACTIVATE` (4)** | **yes** | **yes** |
  | `WM_SYSCOMMAND` `SC_RESTORE` | yes | **no** — stole it |

  So `SW_SHOW` is wrong on *both* counts: it activates the window, and on a
  minimized+hidden one it does not even un-minimize it. Using it made this
  method steal focus like `focus` while also failing to jump — precisely the two
  things reported. Only `SW_SHOWNOACTIVATE` does both jobs, and the off-screen
  move now carries `SWP_NOACTIVATE` as well.

  The invariant is asserted by test now, not assumed: the jump path must never
  call `SW_SHOW` or `SW_RESTORE`. (`show_window` and `jump_alt_instance` still
  use them, and should — showing an alt to the user, and the focus method, are
  both meant to activate.)

### `jumpMethod: "none"` — because there is no AFK kick to prevent

  With the restore fixed and the focus promise kept, `message` still did not
  make the character jump. That is the expected result, and it is worth being
  blunt about why the earlier "it worked a few times" was not the jump:

  * the `WH_KEYBOARD_LL` harness proved a posted `WM_KEYDOWN` is delivered to
    the window's WndProc and **never** becomes a system keyboard event, so no
    raw-input consumer can see it; and
  * gameplay input is engine-level, not window-message-level. A posted message
    can drive a UI (the chat box reads characters) but not a character jump.

  So `message` cannot work, and the only mechanism that reaches a Roblox jump is
  a real key event — which the system delivers to the **foreground** window.
  `focus` is therefore not a preference, it is the only method.

  Which raises the question of whether any of it is needed. Across a whole
  recorded session:

  | disconnect reason | count |
  |---|---|
  | failed to join (a full server refused it) | 157 |
  | client closed (process gone) | 9 |
  | failed to join (old presence-based) | 1 |
  | **AFK / idle kicks** | **0** |

  Not one idle kick, ever. So `focus` was costing the user their active window
  — briefly activated, once per alt, every interval — to prevent something that
  has never happened, while the real problem (157 refusals) went untouched.

  Hence `jumpMethod: "none"`: anti-AFK explicitly off, no thread started, and
  nothing in the jump path can touch a window. It is a first-class option rather
  than a huge interval because a 6-hour interval that looks like it is still
  doing something is worse than saying "off".

  The remaining real cost is 157 failed joins, which is a **capacity** problem,
  not a join-mechanism problem: the place caps servers at 7 players.

## `altBackend`: start alts through the headless CLI (optional)

  Three ways to start an alt client now, all launching the same official exe:

  | backend | how the process starts |
  |---|---|
  | `protocol` (default) | `roblox-player:` URL via the OS handler |
  | `directLaunch: true` (per account) | exe + `--app -t <ticket> -j <url>` inline |
  | `altBackend: "cli"` | `python -m roblox_headless join …` in `headless/` |

  The CLI route launches the same exe with the same arguments, so joining
  behaviour is identical — same server pin, same client. What it adds is a
  stable, independently tested boundary (JSON in/out, `--dry-run`, pid
  tracking). Everything downstream is untouched: hidden windows, EcoQoS,
  health monitor, overlay, and client-log reading.

  Two rules that matter:

  * The joinScriptUrl is built by the **launcher**, never the CLI — the CLI's
    own builder uses `RequestGame` (no server pin). The CLI is only ever handed
    an explicit `RequestGameJob` URL for the main's exact server.
  * The main opening the home screen never uses the CLI (there is no place to
    join). Per-account `"backend": "cli"` / `"backend": "protocol"` overrides
    the global `altBackend`.

  Config: `"altBackend": "protocol"`, plus optional `"cliPython"` (default: the
  running python) and `"cliExePath"` (default: CLI resolves it). Failures raise
  instead of silently launching nothing; if the CLI falls back to a dry-run
  (exe not found) the launcher proceeds down its normal wait-for-process path.


## Cross-checked against JunkBeat/AntiAFK-Roblox

  An independent, actively maintained tray tool (19 stars, 49 commits) that
  solves the same problem. Worth recording what it does, because it settles the
  argument above from a second direction: it also **briefly focuses the window
  and sends a real key press**. Its own README describes the Energy-Efficient
  mode as bringing the window "into focus" for a configurable time, the
  High-Performance mode as minimized windows "briefly maximize", and offers a
  "Hide window contents" option to make the window invisible while focused —
  which is the same alpha-0 technique used here.

  So it does not offer a focus-free jump either. Two independent implementations
  converging on "briefly focus, real key" is much stronger than my own harness
  alone. Three things in it *are* better, and have been adopted:

  | from the tool | what it says | adopted as |
  |---|---|---|
  | "Open for … sec" | *"set a duration long enough for the window contents to load"* | `jumpSettleSec`, default `0.35` (was a flat 50 ms) |
  | "CameraShift" | *"involves more interactions with the Roblox window"*, more reliable | `jumpAction: "camera"` — jump, then nudge the camera both ways |
  | advance notice | *"you'll get a notification ~3 seconds before a window comes into focus, so you can pause your work"* | the launcher now **warns before** taking focus |

  The first is the most likely reason the focus jump was unreliable: a client
  that has been minimized and hidden has to resume rendering and its own loop
  before it acts on input, and a key sent into that gap is simply lost. Their
  troubleshooting says exactly that — *"if Roblox windows don't focus in time or
  key presses fail to register, try increasing the delay"* — and 50 ms was a
  guess on our side.

  Not adopted: the README also suggests **FPS Unlocker** and **Mem Reduct**.
  FPS Unlocker patches the Roblox client, which is outside what this tool will
  do; the other is just an external memory trimmer and not our business.

## The overlay shows when the next anti-AFK jump happens

  A line under the state list, because "when" has five honest answers and a bare
  countdown gets four of them wrong:

  | shown | when |
  |---|---|
  | `anti-AFK: next in 7:42` | a real countdown to the next pass |
  | `anti-AFK: held off - main is active` | skipped because the main is the window in use |
  | `anti-AFK: idle - main not in a game` | no alts are in a game, so none is due |
  | `anti-AFK: off` | `jumpMethod: none`, or `jumpIntervalSec: 0` |
  | `anti-AFK: jumping now` | overdue, or within 5s (turns amber) |

  **The held-off case is the one a timer would most badly misreport.**
  `_last_jump_at` is deliberately *not* advanced while a jump is being held off,
  so the naive arithmetic reads `0:00` and claims a jump is imminent when none
  is scheduled. It also turns amber 5 seconds before a real jump, so the focus
  steal is visible *before* it happens rather than noticed afterwards.

  It is **one line for the whole set, not one per row**, on purpose: a pass
  visits every alt within the same second (they are staggered by a fraction of
  one), so the same number repeated seven times would be noise pretending to be
  data.

  The countdown is part of the overlay's content fingerprint, deliberately — that
  is what makes it repaint. A timer frozen on whatever second it was drawn at
  would look authoritative while being wrong. It is one repaint per second, which
  is not the repaint storm the `BeginPaint`/`EndPaint` work was about; that was
  Windows re-sending `WM_PAINT` continuously.

## Two bugs the status overlay surfaced, both in the log-authority code

**"queued" forever, over accounts that were in the right server.**

    [16:34:39] [hhhhhhohkk2] main's server a782d754 was full — this alt was put
               in server 798f46fa instead; closing it and retrying
    [16:35:10] [hhhhhhohkk2] its own client log confirms it joined server
               a782d754 after 1s.        ← correct server, confirmed

  …and the overlay still read `queued`. The wrong-server flag was cleared in
  exactly one place — the presence sweep's all-good branch — and making the
  client log authoritative made that branch unreachable for log-backed alts. So
  the flag could be *set* but never *cleared*: sticky, accumulating one more
  account every few minutes. Clearing now lives wherever the verdict is
  reached, and the log is where that happens.

  The same change had two related holes, both closed at the same time:

  * the log path had **no debounce** where presence had one. It runs every
    second and `_queue_for_main_server` *closes* the client, so a single
    reading could kill a healthy client. It now needs
    `dropConfirmCount` consecutive agreements — and `_good_reading` is only
    called on the all-good branch, because calling it unconditionally popped
    the counter every second and made two readings impossible to ever reach.
  * a freshly launched client may not have written its log yet, so "log nearest
    this launch time" could resolve to the **previous** client's log — reading
    another client's server, which looks exactly like a wrong-server join.
    A log must now have *started at or after* the launch, or nothing matches
    and the caller waits.

**A client the whole health net could not reach.**

    [15:56:03] [hhhhhhohkk6] anti-AFK jump not delivered: its window is gone
    …repeating every 10 minutes, for hours.

  The process was alive (so the process check passed), there was no client log
  (so the log check had nothing to say), and presence was blind (so nothing
  could confirm or reject it). No window **and** no log is purely local
  evidence that the client never came up, and it now triggers a relaunch —
  after `joinConfirmSec`, so a client that is merely young is left alone.

## A DISABLED account was being anti-AFK'd forever

    [15:25:54] [hhhhhhohkk6] anti-AFK jump not delivered: its window is gone
               (client restarting?) (repeats are suppressed for 60s)
    ... every ten minutes, for hours, with nothing else in the log.

  `hhhhhhohkk6` was not dead — it had been switched **off**. `set_enabled` did
  the right thing (killed the client, then `sync_enabled()` dropped it from
  `mgr.accounts`), and because `_alt_account` searches that list the health
  monitor correctly ignored it. But `_jump_tick` walked `self.running` and
  checked nothing at all, so it went on poking an account the user had
  deliberately turned off — loudly enough to bury a real problem.

  Fixing that exposed two more, both from the same "bookkeeping outlives the
  decision" pattern:

  * `kill_instance` killed the pids but left the `running` record and the
    `_joined` marker behind, and since the health monitor skips disabled
    accounts, **nothing** ever cleaned them up. So re-enabling the account later
    would never launch it: the stale `_joined` made the follow loop skip it and
    the health monitor only walks `running`. `forget_account()` now kills *and*
    unregisters, and is used by the off toggle and by remove-account.
    `kill_instance` still only kills, because "Kill Selected" exists precisely
    to test auto-reconnect and needs the record to survive.
  * the follow loop's `alt_accounts` was a **one-time snapshot** taken when the
    loop started, so an account switched off mid-session would still be launched
    and relaunched. The `enabled` flag is now re-checked every pass — and the
    "stale `_joined`" relaunch recovery explicitly refuses to revive a disabled
    account, because fixing that without this would have brought one back from
    the dead.

  The general lesson, now covered by tests: a live client is the truth, and any
  bookkeeping set is only a hint about it.

## "joined" was a one-way street

  Reported: once an alt showed `joined` it never changed again, even after it
  left the game.

  That was a real flaw in the log-authority design. The evidence that an alt had
  joined was the presence of a `! Joining game '<job>'` line — but **that line
  persists in the log after the client leaves.** So "this log mentions a join"
  was being read as "it is joined", permanently. Kick detection was left to
  presence, which in practice never once worked:

  | drop reason | count in one session |
  |---|---|
  | failed to join (own client log records no join) | 157 |
  | client closed (process gone) | 9 |
  | **left the server (kicked from game)** | **0** |

  The log does record leaving a game, as a session state machine. Real lines,
  from a client that joined, left, and rejoined:

      08:28:31  Session history: IASP     Entered play session.
      08:30:19  Session history: IASPSA   Entered app session.   <- left, alive
      08:30:25  Session history: IASP…    Entered play session.  <- rejoined
      08:30:56  Session history: IASPSAT  Tearing down.          <- shutting down

  The **last letter** is the current state: `P` in a game, `A` back at the
  app/home screen, `T` tearing down. A history that never reached `P` means it
  has not been in a game yet. So the log now answers two questions with two
  different pieces of evidence — which server (the join line) and still in it
  (the session state) — and kick detection no longer depends on the API at all.

  Two details that matter:

  * the **state** flips on the first observation while the **teardown** stays
    debounced by `dropConfirmCount`. `_seen_in_game` is the historical proof and
    never goes stale on its own, so the overlay now requires it *and* a live
    "in" verdict; otherwise the panel would keep claiming `joined` for the
    second or two before the client is actually closed.
  * Roblox writes `! Joining game` a moment **before** `Session history: IASP`,
    so there is a real sub-second window with a join and no session state. A
    missing session state is **not** evidence of absence — falling through to
    "not in a game" there would close a healthy, joined client. With a join line
    and no session line, the join line is used; with neither, the log is
    positive evidence the client is running but has not entered a session.

## …and the same trap, twice more

  Reported: *"the main account always shows in game after I join a game, and it
  doesn't change unless the window closes"* and *"alt does the same thing but it
  doesn't change even after the window closes."*

  **The main.** Its `in_game` was derived from the presence of a
  `! Joining game` line in its own log. That line persists after leaving, so
  `in_game` was `True` forever once the main had ever joined, and with the
  window still open nothing could change it. This is precisely the trap above —
  fixed for the alts, missed in the follow loop. The main now reads the same
  session state, and the log wins **both** ways: presence can be stale forward
  (the previous session's job) and stale backward (a game already left). The log
  says so explicitly now:

      [follow] hhhhhhohkk left a game (its own client log: session IASPSA).

  **The alt.** The overlay tested `joined` *before* "no client":
  `_seen_in_game` is historical proof that outlives the client, and closing an
  alt — with the main, or by hand — pops its record but leaves that flag set. So
  an alt with nothing running still read `joined`. A client that does not exist
  cannot be in a game, so that check now comes first, and closing alts clears
  the per-alt in-game bookkeeping so a re-enabled alt starts clean.

  The pattern across all three: **a live client is the truth, and a line in a
  log or a flag in a set is only evidence — which goes stale, and has to be
  dated against the live thing before it is believed.**

  If an alt dies within `quickFailSec` (`90`s) of joining, that is a rejection
  rather than a random drop — the server is refusing it. Retrying instantly
  just re-enters the loop (a relaunch every ~15s, which also drove the
  presence API into HTTP 429s), so the retry waits `quickFailBackoffSec`
  (`30`s) and grows: 30, 60, 120, 240, capped. An alt that was genuinely up and
  playing reconnects immediately with no delay.

  If an alt keeps getting dropped, `rejoinPauseDrops` pauses its auto-reconnect
  for `rejoinPauseSec`. **These were deliberately loosened** (was: 3 drops in
  5 minutes → a 5-minute pause, with a 60s grace and 6s per-alt polls). That
  combination parked an alt for five minutes during ordinary server churn, and
  the debounce plus per-alt polling meant a kick took 15–25s to notice. If you
  want the old eager behaviour, set `dropConfirmCount: 1`,
  `rejoinPauseDrops: 3`, `rejoinPauseSec: 300`.
- `altQualityLevel` → graphics level used by the above (default `1` = lowest,
  `0` = leave whatever your settings file has).
- `noRender` → **experimental, off by default (`false`)** — hidden alone is not
  enough to stop a client drawing frames, so this keeps each alt **minimized as
  well as hidden** (the mechanism from the
  [rbxsilent](https://github.com/toastering/rbxsilent) project, reimplemented in
  ctypes). It works and was verified on real windows, but it interferes with the
  launcher's own window discovery, so it's opt-in: turn it on for one client at
  a time if you want to test it.

> **`placeId` is optional in follow mode.** If you leave it empty, "Launch All"
> opens the main account to the Roblox home screen — you pick any game and
> start playing, and the alts join that exact server as soon as presence detects
> you in-game. Set a `placeId` if you want the main to auto-join a specific game
> (it's required in simultaneous mode).

---

## 3. Run it

### GUI (recommended)

```bat
python roblox_launcher.py
```

(or double-click `run.bat`)

In the window:
- **Mode** drop-down: `follow` (default) or `simultaneous`
- **Launch Main** / **Launch All** — no Place ID needed. Launch All opens the
  main client to the home screen; pick a game inside it and the alts follow
  (join when you join, close when you leave, close all when you close the client)
- **Add Account…** — name, role (main/alt), optional server jobId, and
  **Login with browser…** to capture the cookie automatically
- Select accounts to **Launch Selected**, **Hide/Show**, **Kill**
- **Verify Cookies** — checks every cookie against Roblox and shows usernames
- A green status line shows follow-mode progress ("Waiting for Main to join a
  game…" → "Main in-game — launching alts…" → "Main left the game — closing alts…")
- **Auto-reconnect**: every alt is health-checked once a second. If Roblox
  closes it, it crashes, or it gets kicked from the server, the log shows
  `DISCONNECTED` and the alt reconnects into your current server by itself.
  Alts that drop 3+ times within 5 minutes get a 5-minute reconnect pause
  (so Roblox can't make it loop forever) — the log tells you when that happens.
- **Anti-AFK jumps**: while the main is in a game, every alt jumps (Space)
  once every 10 seconds (`jumpIntervalSec` in config — `0` turns it off, the
  log confirms the interval at launch). Roblox needs the window focused for the
  key to register (raw input), but the jump is **flicker-free**: each alt is
  briefly shown as a fully transparent layered window (alpha 0) so nothing ever
  appears on screen, then re-hidden and focus is handed straight back to
  whatever window you were using. Set `"jumpMethod": "message"` to post the key
  straight to the hidden window instead — no show, no focus, no restore at all
  (see the config notes; Roblox may ignore it).
- **Your main never gets unfocused**: jumps are held off while the main client
  is your active window (`jumpSkipWhenMainFocused`), and resume as soon as you
  switch away, so the window you're playing in is never stolen. A safety net
  (`jumpMaxGapSec`, default 3 min) still jumps occasionally so alts can't idle
  out during long sessions.
- **Alts run as background work** (`altThrottle`): Windows EcoQoS +
  `BELOW_NORMAL` priority for alt clients only, so their hidden rendering costs
  noticeably less CPU/GPU and never competes with what you're doing.
- **Alts stay in your server** (`serverQueueRetrySec`): if Roblox turns an alt
  away because your server is full, the tool closes that alt and re-queues it
  for your exact server (with backoff) instead of letting it sit in a random
  other server. It shows as "queued" while waiting.
- **Rate-limit safe**: a failed or `429` presence check is never mistaken for
  "the main left the game" — that used to close every alt and relaunch them a
  minute later. Failed checks now back off (5s → 60s, reset on the next good
  reply), the per-alt checks are jittered so they don't burst, and any decision
  to close/relaunch an alt must be confirmed by **two consecutive** readings.
- **Session log on disk**: everything the GUI shows is also written to
  `launcher.log` next to `config.json`, so a session can be reviewed after
  closing the app.
- **Optional experiments** (both **off by default**): `noRender` keeps alts
  minimized so they stop drawing, and `altFramerateCap` gives each alt its own
  settings file for a 1 fps cap. Neither is on out of the box — `noRender`
  interferes with window discovery, and the frame cap needs a direct exe launch
  that loses Roblox's singleton-mutex race, so only one alt ends up joining.
- Log pane shows everything that happens

### Standalone .exe (no console)

`dist\RobloxMultiInstance.exe` is the GUI compiled to a single console-less
executable (built with PyInstaller). Just double-click it — no console window,
no Python install needed on the target machine. The exe always looks for
`config.json` in its **own folder**, so keep your config next to the exe.

To rebuild it, double-click `build.bat` (or run the command below). It requires
PyInstaller once: `python -m pip install pyinstaller`.

```bat
python -m PyInstaller --noconsole --onefile --name RobloxMultiInstance ^
    --hidden-import cookie_login --clean roblox_launcher.py
```

Notes about the exe:
- `--noconsole` hides the console, so CLI flags (`--cli`, `--verify`, …) still
  work but print nothing visible. Use the Python script for CLI output.
- Single-file (`--onefile`) builds can occasionally trip antivirus heuristics —
  it is just your own Python + Tkinter compiling to one file.
- **Windows Smart App Control blocks every new build.** The exe is unsigned, so
  Code Integrity refuses it with *"did not meet the Enterprise signing level
  requirements"* and launching fails with `WinError 4551` /
  *"An Application Control policy has blocked this file."* Each rebuild is a new
  hash with no reputation, so this repeats on **every** build — it is not a
  problem with your code. Turn it off once:
  **Settings → Privacy & security → Windows Security → App & browser control →
  Smart App Control → Off**, then reboot. (The alternative is buying a
  code-signing certificate; Defender exclusions do *not* bypass it.)
  To confirm what blocked a file:
  `Get-WinEvent -LogName "Microsoft-Windows-CodeIntegrity/Operational" -MaxEvents 5`

### CLI

```bat
:: launch main + all alts (follow mode by default — no placeId needed)
python roblox_launcher.py --cli --launch-all

:: same, but join a fixed place at once (requires placeId in config or --place-id)
python roblox_launcher.py --cli --launch-all --mode simultaneous --place-id 4483381587

:: launch only the main account
python roblox_launcher.py --cli --launch-main

:: launch specific accounts by name
python roblox_launcher.py --cli --launch Main Alt1

:: check your cookies / config without launching
python roblox_launcher.py --cli --verify
```

The CLI holds the multi-instance guard and keeps hidden windows hidden until
you press **Ctrl+C**, at which point hidden windows are shown again.

---

## How it works

1. **Auth ticket per account** — for each cookie we do the standard
   `auth.roblox.com/v1/authentication-ticket` (CSRF) dance to get a short-lived
   ticket for *that* account. No stored-cookie file surgery is needed.
2. **Launch** — each client is started with the official
   `roblox-player:1+launchmode:play+gameinfo:<ticket>...` protocol URL, which is
   how the Roblox website itself launches a game for a specific logged-in
   account. This is a flavor of what popular open-source tools (e.g. Roblox
   Account Manager) use.
3. **Follow-main sync (continuous)** — after the main launches we poll Roblox's
   `presence.roblox.com/v1/presence/users` about every second. When the main
   reports `userPresenceType: 2` (in game) we read the `placeId` and `gameId`
   (server job ID) and launch the alts with
   `request=RequestGameJob&gameId=…` so they land in that exact server. When the
   main leaves the game the alts are closed; when it joins again they join
   again; when the main's client process exits everything is closed and the
   follower stops. If the presence API is throttled, no decision is made and
   nothing is closed — it just waits and re-checks.
3b. **Alt health monitoring** — a thread checks every alt once a second: first
   that its client process is still alive (catches Roblox closing/crashing an
   alt instantly), and every ~5s per alt that it's still in-game via presence
   (catches kicks/server drops). A disconnected alt is launched again into the
   main's current server automatically. Freshly joined alts get a 60s grace
   period before presence is trusted, and an alt that drops 3+ times in 5
   minutes gets a 5-minute reconnect pause to avoid thrash loops.
4. **Cookie capture** — `cookie_login.py` launches Edge/Chrome with a temporary
   profile and `--remote-debugging-port`, opens `roblox.com/login`, and reads
   `.ROBLOSECURITY` over the Chrome DevTools Protocol once you've signed in.
5. **Multi-instance guard** — while the program runs it owns the
   `ROBLOX_singletonMutex` and keeps Roblox's `RobloxCookies.dat` open with
   share=none. This is the community-known mitigation for Roblox closing extra
   instances / error-773 resets.
6. **Headless alts** — after each alt client creates its window we call
   `ShowWindow(SW_HIDE)` and add `WS_EX_TOOLWINDOW` (no taskbar entry). A
   watcher thread re-hides the window if Roblox recreates it (teleports, etc.).

---

## Honest limitations & disclaimer

- **Roblox actively detects and closes multiple instances** (anti-farming).
  This tool is best-effort — Roblox may still close instances after a random
  amount of time. Every alt is health-checked once a second and reconnected
  automatically when it drops, so a closed instance usually comes right back
  (unless an alt drops 3+ times in 5 minutes, which pauses its reconnects for 5
  minutes to avoid a relaunch loop). There is no way to *guarantee* concurrent
  instances.
- **"Headless" here means hidden, and by default the alts don't draw either** —
  hidden clients are kept minimized (`noRender`), which stops them presenting
  frames, so each alt costs far less than a normal client. They still run the
  real game engine (needed for the session), so they aren't free — CPU and
  memory are still used, and the anti-AFK jump briefly restores the window.
- **There is no deeper "network-level" headless mode available** — this was
  investigated and ruled out with live tests on this machine:
  - *Running each alt on its own non-interactive Windows desktop*
    (`CreateDesktopW`) does hide windows completely, but Windows never lets a
    window on an inactive desktop become foreground and never routes injected
    keys to it — so the anti-AFK jump could not work at all.
  - *DWM cloaking* (`DWMWA_CLOAK`) makes a window invisible without touching
    its styles, but a cloaked window **cannot be activated** either, so it
    cannot receive Roblox's raw input.
  - A *protocol-level* client (talking to the game server directly, no real
    Roblox client) is not possible: the join requires Roblox's own
    authenticated client engine.
  - The only remaining trick — faking raw input without focus — requires
    injecting a DLL into the game process, which is out of scope here.

  So hidden-but-rendering is the practical limit, with the jump made invisible
  (transparent + focus-restored).
- **Posted keyboard messages do not move a Roblox character** — measured, with
  pixel-level verification against a live client, not assumed. A posted
  `WM_KEYDOWN` behaves like this:

  | Alt window state | Posted Space registers? |
  |---|---|
  | visible, opaque, focused | **yes** |
  | visible, opaque, backgrounded | **yes** |
  | shown but transparent (`WS_EX_LAYERED`, alpha 0) | no |
  | hidden (`SW_HIDE`) | no |
  | hidden + minimized | no |

  The interesting part is that Roblox *does* accept posted keys — it just
  requires the window to be genuinely visible **and opaque**. A layered alpha-0
  window still reports `IsWindowVisible == TRUE`, but no frames are presented
  for it and the input is ignored. So the one case that would have delivered
  anti-AFK with no unfocus at all is the one case that does not work, and making
  it work means the alt is on screen. `jumpMethod: "message"` is therefore
  **not** recommended.
- **A dead window handle silently breaks anti-AFK** — worth knowing because the
  symptom is invisible. A Roblox process owns several top-level windows (the
  client plus hidden `Default IME` / `MSCTFIME UI` helpers), and it recreates
  its client window when the render device resets. Two failure modes, both fixed
  here:
  - Posting to the *old* handle fails outright — `PostMessage` returns 0.
  - Re-resolving it as "the first window this pid owns" lands on a hidden IME
    helper, because `EnumWindows` returns windows in z-order and a hidden alt's
    real window is itself hidden. `PostMessage` to a valid helper **succeeds**,
    so nothing is ever logged as an error and the character simply never jumps.

  The launcher now resolves the client window by *identity* (keep the trusted
  handle while it is still valid and still owned by that alt; otherwise require
  a window whose title contains "Roblox"; never an IME/unnamed helper; report
  nothing rather than guess if only helpers exist). It logs a one-line notice
  when the handle genuinely changes, and rate-limits jump warnings to once a
  minute so a 1-second interval cannot bury the log.
- **Your main still blurs if you let the jump steal focus** — this is a Windows
  rule, measured on real windows here: the foreground window is whatever holds
  keyboard focus, and Windows promotes a window to foreground as soon as it
  takes focus. Focusing an alt "without" making it foreground is not possible
  (an `AttachThreadInput` + `SetFocus` probe confirmed the foreground follows
  the focus immediately, and the main receives `WM_KILLFOCUS` either way). So a
  real key press to an alt *always* costs the main a brief unfocus. This tool
  therefore skips jumps while your main is the active window
  (`jumpSkipWhenMainFocused`) instead of pretending the problem doesn't exist.
  With that on, the guarantee is absolute: **your main never loses focus, ever** —
  not "briefly".
- **Terms of Service**: automating accounts / running multiple clients may
  violate Roblox's ToS. Use this only with **your own accounts** and for
  legitimate purposes (e.g. game development, testing). You are responsible
  for how you use it.
- Cookies are stored in plain text in `config.json` — keep that file private.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not obtain a CSRF token` | Cookie is expired/invalid. Re-capture with **Login with browser…** or **Verify Cookies**. |
| `Roblox does not exist` | Install Roblox from roblox.com/download (not MS Store). |
| Alts never launch after main logs in | Follow mode waits for the main's *presence* to flip to "in game". Make sure `role: "main"` is set on the account you play with, and give it up to `joinTimeoutSec`. |
| Roblox closes extra instances | Expected; Roblox's anti-farm detection. Reduce alt count, relaunch. |
| `Neither Microsoft Edge nor Google Chrome was found` | Browser login needs Edge or Chrome on the machine. |
| `tkinter is not available` | Use the CLI mode, or reinstall Python from python.org with Tcl/Tk included. |
| Exe won't start: *"An Application Control policy has blocked this file"* | Smart App Control is rejecting the unsigned build — see the **Standalone .exe** section; turn it off and reboot. |