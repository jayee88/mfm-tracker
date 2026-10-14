# Changelog

## 2610.15.004 — 2026-10-14

### Changed
- **Scroll as fast as you like.** The screen is captured continuously — like a
  recording — and the reading works through the captures at its own pace, so rows
  aren't missed when you scroll or hop shops faster than the OCR can read. The
  status bar and a new *Frames waiting* stat show when it's catching up.
- Pressing **Stop** still reads the frames captured just before; press Stop again
  to discard them.

## 2610.15.003 — 2026-10-14

### Changed
- Megaphones with **C/O** count as selling.
- After you pick your channel when the app starts, channel changes aren't detected for
  15 seconds, so closing the game's Change Channel window doesn't ask again.

## 2610.15.002 — 2026-10-14

### Fixed
- Megaphones were listed again as the chat scrolled, when a name was read a little
  differently.
- Player names in megaphones are read more accurately, and match names the app has
  seen before.

### Changed
- Megaphones are kept for 24 hours, then deleted. Set how long in the Megaphones tab.

## 2610.15.001 — 2026-10-14

### New
- **Megaphones in every colour:** pink, teal and yellow megaphones, and the blue ones
  from your own channel (they get the channel from the Channel box).
- Megaphones show in their in-game colour.
- **What the OCR sees** in the Megaphones tab: the chat window, with the megaphones
  being read outlined.

### Fixed
- The chat window wasn't found while you were typing in the chat bar.

## 2610.14.001 — 2026-10-14

### New
- **Megaphones tab:** while monitoring, buying and selling megaphones are read from the
  chat window and listed with their time, player, channel and message: selling on top,
  buying below.
- **Chat area** button, for when the chat window isn't found by itself.
- The channel picker works with the keyboard: arrow keys to move, Enter to pick.

### Changed
- The channel picker shows 20 channels by default again.
- While the channel picker is open, channel changes in the game aren't detected.

## 2610.13.001 — 2026-10-13

### New
- **My Inventory:** a new tab at the far right of the tab row to keep track of the items
  you own and what they're worth.
  - Add items with **Add by name**, or let **Auto Detect (beta)** read your Item
    Inventory window in the game.
  - See each item's market price, price range, recent sales and more.
  - Set a **List price** and **Notes**: click a selected row's cell to edit it.
  - Mark items **Sold** when they sell (or **Unsell** to undo). Sales are added to your
    prices.
  - **Net worth** totals, including listed items value and realized profit, with the 3%
    meso tax taken off.
  - **Export**, **Import** and **Clear My Inventory**.
- **Notes on listings:** right-click a listing → **Add note** for things like +5 ATK.
- **Watched listings:** right-click a listing → **Watch this listing** to get an alert when
  that player's listing is for sale again.

### Changed
- The **Alerts** tab is now **Watched**, with watched items on top and watched listings
  below.
- The channel picker shows 18 channels by default.
- Buttons and menu items no longer end in "…".

## 2610.12.002 — 2026-10-13

### Fixed
- The map wasn't found on some maps, e.g. Victoria Road: Sunset Sky.

## 2610.12.001 — 2026-10-13

### New
- **Channel prompt:** the app asks for your channel when it starts and after you change
  channel in the game. On by default; turn it off in Settings.
- **Channel picker:** the Channel box's arrow opens a grid of channel buttons. Set how many
  it shows in Settings (default 20).
- **Exports include the date and time** in the file name, e.g.
  `mfm-tracker market prices 2026-10-13 08-11-00.csv`.
- Install instructions for the major Linux distributions.

### Changed
- The app is now called **Free Market Tracker**, "Free Market price tracker for MapleStory
  Classic World". Its technical name is `mfm-tracker`; from source, run
  `python mfm-tracker gui`.
- **Capture every** defaults to 0.15 seconds.
- **Required sightings before judging** (was "Outliers: judge after this many sightings")
  now also applies to DEAL / HIGH: an item needs 5 sightings before it gets a market price.
  **Flag DEAL / HIGH** moved under it in Settings.
- From source on Linux, `pynput` is only needed for the `hotkey` command.
- Simpler wording in the app, the README and the changelog.

### Fixed
- Free Market map names were sometimes read as junk.
- Tesseract wasn't found on Void Linux.

## 2610.11.002 — 2026-10-13

### Fixed
- The downloaded apps couldn't connect to meowdb ("certificate verify failed").

## 2610.11.001 — 2026-10-13

### New
- **Items tab filters:** name, category, class, level and "Sold by NPCs".
- **NPC buy** and **NPC sell** prices in the Items tab.
- **Shop** and **Player** are separate columns.
- Maps are saved with their full name, e.g. "Hidden Street: Free Market <2>".
- Wide tables scroll sideways.

### Changed
- A listing's price is the meso amount shown; "699 for" quantities are ignored.
- Column headers are **Price** and **Max price**.

### Fixed
- Some map names were misread or cut short.
- Required level 0 showed as unknown.

### Upgrading
The first start converts saved prices and map names, and offers to re-download the item
and map lists once.

## 2610.7.001 — 2026-10-12

First release: Windows, macOS (Apple silicon) and Linux apps.
