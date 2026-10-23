# Free Market Tracker

**Free Market price tracker for MapleStory Classic World.**

Free Market Tracker reads the shop windows you open in the Free Market, logs every item and
price, and marks each listing as a **DEAL**, **fair** or **HIGH** compared with the market
price. It only looks at your screen; it never touches the game.

Technical name (command line, data folder, repository): `mfm-tracker`.
What's new: [CHANGELOG.md](CHANGELOG.md).

## Install

Download the app for your system from the
[Releases page](https://github.com/jayee88/mfm-tracker/releases). Each download has the
version in its name, like `Free-Market-Tracker-2610.18.003-Windows.zip`. Nothing else
needs to be installed.

- **Windows:** unzip it and run `Free Market Tracker.exe`. If Windows shows "Windows
  protected your PC", click **More info → Run anyway**.
- **macOS** (Apple silicon; on an Intel Mac, build it yourself, below): unzip it and drag
  **Free Market Tracker** into Applications. If macOS won't open it, go to System
  Settings → Privacy & Security and click **Open Anyway**. When you first press **Start
  monitoring**, allow **Screen Recording** (System Settings → Privacy & Security → Screen
  Recording), then reopen the app.
- **Linux:** extract it and double-click **Free Market Tracker** (if it opens as text:
  right-click → Properties → "Is executable"). Runs on 64-bit distros with glibc 2.35 or
  newer (Ubuntu 22.04+, Debian 12+, Fedora 36+, openSUSE, Arch and others). On Wayland,
  the app reads the game's window directly when the game runs through Wine (Lutris, Steam
  or plain Wine), as it does by default: Wine's window lives in XWayland, and the app copies
  its picture in a few milliseconds, with no prompt. If the game window can't be read that
  way, the app falls back to your desktop's screenshot tool, which takes about
  a second per screenshot: `spectacle` (KDE), `gnome-screenshot` (GNOME) or `grim`
  (Hyprland, Sway; grim doesn't work on KDE or GNOME).

Play the game **windowed or borderless**, not exclusive fullscreen. Two game settings
matter, and the app says so in a red bar at the bottom of its window if one is off:

- **Chat timestamps** on (in the game's settings), so each chat line starts with its time,
  like `[13:45]`. Without it, megaphones get the time they were read instead of the time they were sent.
- **The minimap showing the map name** (the street and map name above the map). The app
  reads the map you're on from there.

The app reads the game's window on its own, so it keeps reading while other windows cover
the game (not while it's minimized), on Windows, macOS and Linux. The window is found by
"MapleStory" in its title; change that, or turn this off to read the whole screen
instead, in **Settings → Reading → Window detection**. If the game window can't be read on its
own, the whole screen is read, and then the game has to be visible.

### Updates

When a new version comes out, the app asks if you want it when it opens. Click **Update
now** and it downloads the new version, puts it in place of the old one and restarts.
Your prices and settings are kept. **Later** asks again next time; **Skip this version**
doesn't ask about that version again. **Settings → General → Check for updates** looks
right away, and the checkbox there turns the question at launch off. On macOS, keep the app
in your Applications folder so it can update itself.

### Run from source (optional)

**Windows:** install [Python](https://www.python.org/downloads/) (tick **Add to PATH**)
and [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki), then in PowerShell:
```powershell
cd mfm-tracker
py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python mfm-tracker gui
```

**macOS:**
```bash
brew install tesseract python@3.12 python-tk@3.12
cd mfm-tracker
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python mfm-tracker gui
```

**Linux:** install the packages for your distro, then run the same three commands as on
macOS with `python3`:

| Distro | Packages |
|---|---|
| Ubuntu, Debian, Mint, Pop!_OS | `sudo apt install python3 python3-venv python3-tk tesseract-ocr tesseract-ocr-eng` |
| Fedora | `sudo dnf install python3 python3-tkinter tesseract tesseract-langpack-eng` |
| openSUSE | `sudo zypper install python3 python3-tk tesseract-ocr tesseract-ocr-traineddata-english` |
| Arch, Manjaro, EndeavourOS | `sudo pacman -S python tk tesseract tesseract-data-eng` |
| Void | `sudo xbps-install python3 python3-tkinter tesseract-ocr tesseract-ocr-eng` |
| Alpine | `sudo apk add python3 python3-tkinter tesseract-ocr tesseract-ocr-data-eng` |
| RHEL, Rocky, AlmaLinux 10 | `sudo dnf install epel-release`, then `sudo dnf install python3 python3-tkinter tesseract tesseract-langpack-eng` |

### Build the app yourself (optional)
- **Windows:** double-click `packaging/Build Windows App.bat`.
- **macOS:** double-click `packaging/Build macOS App.command`.
- **Linux:** run `packaging/build_tesseract_static.sh`, then `packaging/build_linux.sh`.
- **All three on GitHub:** run the **Build apps** workflow from the Actions tab.

The finished app is in the `dist` folder.

## Getting started

1. Open Free Market Tracker. The first time, it offers to download the MapleStory Classic
   item and map lists. Say yes; they correct misread item and map names.
2. Pick your **Channel**.
3. Click **Start monitoring**, then play normally: open shops and scroll through them.
   Left of the button, a green arrow means it's monitoring and a red square means it's
   stopped. Next to the channel, **Map:** shows the map you're on, read from the
   minimap every few seconds while monitoring.

The shop window and the minimap are found automatically, on any monitor. Each listing is
saved with its item, price, shop, player, map, channel and time. The panel on the right
shows what's being read.

If the shop, map, chat, inventory or Change Channel window is ever not found, draw a box
around it in **Settings → Reading → Window detection** (**Draw area**). That section lists each area
as **Automatic** or **Manual**, with the coordinates of a drawn one. **Automatic** next to
an area, or **Auto Detection** for all of them, goes back to finding them by themselves.
An area is drawn on what's being read (the game window, or the whole screen), and is only
used while the same is read.

## Channel

Click the arrow on the **Channel** box to pick from the channel picker, or type the
number. In the picker, the arrow keys move between channels and Enter picks one. The
channel stays set until you change it.

The app also asks for your channel when it starts. When you change channel in the game,
it reads the channel highlighted in the game's Change Channel window (the blue button,
like CH.19). It sets it only if the screen fades to black within 3 seconds of the window
closing, which is what the game does when it changes channel; it only asks if it can't
read it. Closing the window with **CANCEL** or **Esc**, or a change the game refuses (a
restricted map, or in combat), has no fade, so the channel stays as it was. It looks for
the window as often as **Capture every (seconds)** in Settings (0.1 by default). For 15 seconds after you pick
your channel at start, channel changes aren't detected, in case you had the Change Channel
window open to check. Turn this off, or set how many
channels the picker shows, in **Settings**.

With hotkeys turned on (they're off at first; see Hotkeys under Settings), **F4** and **F3**
set the app's channel to the next and previous one, and **F5** starts and stops
monitoring, even while the game has focus.

## Widget

A small card that stays on top of the game, so you can see the app without leaving it:
how long you've been monitoring, Start / Stop (▶ / ■), Next shop (⟳), Settings (⚙), a
hint line (what to do next, or a warning in yellow: the reading is behind, no channel
set), and three rows: **Monitoring** (Running / Stopped), **Frames waiting** and
**Channel**, with ‹ › to change it. Drag it from anywhere but its buttons; it opens
where you left it, and on Windows clicking it never takes the focus from the game. Turn it
on or off in **Settings → General → Widget**, with the **Widget** box in the toolbar, or
with its ✕. It shows over the game when
the game runs in a window (or borderless), not in exclusive full screen.

## Tabs

- **Megaphones:** see below.
- **Live:** the shop you're in, filling in as you scroll. Green = DEAL, red = HIGH,
  grey = no market price yet, faded = sold.
- **Deals:** listings still for sale, best deals first. Tick **Include sold** to see sold
  ones, or **Watched items** to see only watched items and listings. **NPC resale** keeps
  only listings priced under what an NPC pays for the item, biggest profit first, whatever
  the market rate says: the **NPC sell** and **NPC profit** columns show what an NPC pays
  for one and what you'd make buying the whole bundle and selling it to an NPC. Select a
  listing you bought and click **Bought** (or right-click → **Bought this listing**): it's
  marked sold, so it leaves Deals and counts as a sale at that price.
- **Items:** every item you've seen: the cheapest listing still for sale and where it is,
  listed and sold prices, market price, the median prices in selling and buying
  megaphones, and the NPC buy / sell prices. Filter by name,
  category, class, level or "Sold by NPCs" with the bar above it.
- **Review:** things that didn't match an item in the database, in two tabs:
  - **Shop listings:** names read from shops, with the price, shop, player and channel of
    the newest listing. Press **Enter** to accept the suggested item, **M** to pick one
    yourself, **K** to keep the name, **Delete** to remove it. **Remove all listings**
    deletes the listings of every name in the list.
  - **Megaphones:** what megaphones sell or buy that isn't matched to an item yet, with a
    **Buying / Selling** column. **Enter** accepts the suggested item, **M** picks one,
    **Delete** marks it as "not an item". **Delete megaphone** deletes the megaphones a
    row came from (a misread line, spam) without adding an alias; **Remove all megaphones**
    deletes every megaphone with something in the list. See [Aliases](#aliases).

  Don't need it? Turn the Review tab off in **Settings → General**.
- **Watched**, **Ignored** and **My Inventory:** see below.

Double-click any item for its **price history**: a chart and every sighting, plus a
**Megaphones** tab with every megaphone selling or buying it. Right-click a sighting to
change its price or channel, add a note, watch it, or delete it.

Right-click items in any table for more options: watch the item, add a note, watch the
listing, ignore, match to the right item, mark a listing you bought as sold, delete a bad
read, or look the item up on meowdb.

## Megaphones

While monitoring, the app also reads your chat window for megaphones: the pink, teal,
yellow and blue chat lines. Ones that are buying or selling are listed in the
**Megaphones** tab, **Selling** on top and **Buying** below, in their in-game colour, with
the time (in your local time), player, channel and message. Blue megaphones are from your
own channel, so they get the channel from the **Channel** box. The panel on the right
shows what's being read.

- Selling: S>, WTS, C/O, sell, selling, have, trade, trading.
- Buying: B>, WTB, buy, buying, looking for, LF, need, want.
- A megaphone with both shows in both lists.
- Long megaphones that wrap onto more lines are read in full. Repeated symbols at the end
  (like @@@@@@) are left out.

Each item in a megaphone gets its own line, with the item's icon, name, price and market
price, in the megaphone's in-game colour. The date and time come first; the time, player,
channel and message are shown on a megaphone's first line only, so the lines below it are
its other items. For example, "S> Black cat beanie 520k, B> Fondue Scepter" shows the beanie at
520,000 under Selling and the scepter under Buying. The app finds the item from:

- an item link in brackets, like `[Korean Fan]`;
- an alias it has learned, like "kumbis";
- the item's name, even with a plural "s".

Prices like 90k, 1.5m, 3,2 mil and 250,000 are read. "c/o" (current offer), "each" and
"PM offer" are shown next to the price. If the item isn't found, the row shows the
megaphone's message as it was read (with no price), and the phrase goes to
**Review → Megaphones** until you match it.

Megaphones are kept for 72 hours and then deleted; change this with the box at the top of
the tab. Right-click a megaphone to match it to an item, mark it as "not an item", see the
item's history, copy its message or player name, or delete it. **Filter** at the top shows
only megaphones whose message has every word you type (any order, upper or lower case).
**Watched items** shows only megaphones with a watched item or a watched word.
If the chat window isn't
found, draw it in **Settings → Reading → Window detection** (Chat).

### Aliases

Players call items many things ("kumbis" for Kumbi Throwing-Stars). When you match a
megaphone phrase to an item (in **Review → Megaphones**, or by right-clicking it), the app
learns it as an alias and uses it for every megaphone from then on, including older ones.
"Not an item" teaches it to ignore a phrase. A phrase also matches an item when it has
every word of the item's name, in any order ("gloves attack scroll intermediate").
In Review → Megaphones, **Type: Item / Words** at the bottom sets what **Accept
suggestion** and **Match manually** teach: an item, or words (see **Words** below; Match
manually then asks for the words).

Two buttons at the top of the Megaphones tab and of Review → Megaphones:

- **Edit aliases** opens a sheet of every alias: **Megaphone term** (what
  players write), **Alias** (what it means) and **Type**. Click a cell to type in it; the last
  row adds a new one, and **Delete** removes the selected ones. Click the Type cell to
  switch between:
  - **Item:** the phrase is that item ("kumbis" = Kumbi Throwing-Stars). If the name you
    type isn't exact, you're asked to pick the item. Leave it empty for "not an item".
  - **Words:** the phrase stands for other words, for any item. For example "60%" =
    Intermediate, "10%" = Greater, "100%" = Lesser and "att" = Attack: then "60% gloves att"
    matches Gloves Attack Scroll: Intermediate, and "10% claw att" matches Claw Attack
    Scroll: Greater, without an alias for each scroll. Common shorthand comes already
    filled in as word aliases ("att"/"atk" = attack, "matk" = magic attack, "acc",
    "avoid", "ohs", "2h", "bw", "xbow", "overall", "top", "pants", "helm" and more); change
    or remove them like any other. The scroll grades ("60%" = Intermediate and so on) are
    yours to fill in.
- **Refresh** matches every megaphone again with the current aliases. Phrases that now
  match an item leave Review → Megaphones.

## Notes

Add a note to a listing for anything the item name doesn't say, like **+5 ATK** or **7
slots**: right-click it in Live, Deals or an item's history → **Add note**. Notes show in
the **Notes** column. The next time you see the same item from the same player at the same
price, it gets the note too.

### Selecting several rows
- **Alt + click** adds or removes a row; **Ctrl + Alt + drag** selects every row you pass
  over. Ctrl + click, Shift + click and Ctrl + A work too.
- On a Mac, use **Option** instead of Alt and **Cmd** instead of Ctrl.
- Right-click the selection to act on all of it, or press Delete.

## Watched

Watched items and listings get an **alert**: a sound, and the app comes to the front. Their
listings are shown in bold red text in the shop tables. Megaphones with a watched item are
shown the same way (all their lines): selling at or under your max price (or with
no price given), or buying at any price. The **Watched** tab has two lists.

**Watched items** (top): any listing of an item.
1. Type the item, optionally a **max price**, and click **Watch**. Or right-click any item →
   **Watch this item**
2. When it's for sale in a shop you open, you get an alert, and the price, shop, player,
   map, channel and time are shown.

**Watched words**, for things that can't be matched to an item: click **Type: Item** so it
says **Type: Word**, type the words and click **Watch**. Megaphones whose message has them
are shown in bold red, and the newest one is shown in the list.

**Watched listings** (bottom): one player's listing, e.g. a Korean Fan with +5 ATK.
1. Right-click a listing in Live, Deals or an item's history → **Watch this listing**, and
   enter a note for it.
2. When that listing is for sale again, you get an alert. Its latest price, place and time
   are shown, and it's marked sold once it sells.

Right-click a watched item or listing to see its history, change the max price or note, or
remove it. Pick your own alert sound with **Choose sound**. Sound and "bring to front" can
each be turned off.

**Ignored:** items you don't care about. They never count as deals, but their prices are
still logged and watched items still get alerts. Right-click any item → **Ignore deals on**

## My Inventory

Keep track of what the items you own are worth. Click **My Inventory** at the far right of
the tab row.

**Adding items**
- **Add by name:** type an item's name and how many you have.
- **Auto Detect (beta):** reads your **Item Inventory** in the game (open it with the I
  key). Your items show up as you switch between its tabs. Icons that couldn't be matched
  are highlighted: press **Enter** to accept the suggested item, **M** to pick one yourself,
  or **Delete** to remove the row; your choice is remembered next time. Select the items
  you want and click **Add**. If your inventory isn't found, draw it in **Settings → Window
  detection** (Item inventory). Matching
  works best with all item icons downloaded (Settings → Game data → Update).

**Editing:** click a highlighted row's **Qty**, **List price** or **Notes** to type in it,
or right-click the row. **List price** is the price you're selling it for.

**Selling:** select items and click **Sold** (or right-click → **Mark as sold**). If you
have more than one of an item, you're asked how many sold. Sold items are faded, their sale
is added to your prices, and their list price counts toward **Realized profit**. To undo,
select them and click **Unsell** (or right-click → **Unmark as sold**).

For each item, the tab shows its market price, the range of prices seen, how many
sightings there are, how many sold in the last 24 hours, and the average of its last 3
sales. **Net worth** at the bottom adds them up, with the 3% meso tax taken off:
- **Possible range:** the lowest and highest prices of the items you own.
- **Estimated value (market prices)** and **(last 3 sales)**.
- **Listed items value:** the list prices of the items you own.
- **Realized profit:** what your sold items sold for.

**Export** and **Import** save and load My Inventory as a CSV file. **Clear My
Inventory** deletes it all (it asks first and keeps a backup).

## Market price

An item's market price is:
1. a reference price you imported, or
2. the median of the prices you've seen over the last 14 days (only sold prices, once it
   has sold at least twice).

Your own sightings only count once an item has been seen **5 times** (Settings →
**Required sightings before judging**). Until then it shows no DEAL or HIGH.

**Outliers:** troll prices and misreads far from an item's usual price are left out of its
market price. Very low ones still show as deals; very high ones show as **OUTLIER**. Adjust
or turn this off in Settings.

**Sold items:** listings that are faded in a shop are logged as sold. Coming back to the
same shop doesn't log the same listing twice.

## Shared Database

Share prices with your friends or guild through a small server that one of you runs
(see [server/README.md](server/README.md) for setting it up with Docker). Everyone
connected sees each other's prices in the app as they're logged.

1. Whoever runs the server gives you its **address** (e.g. `203.0.113.5:8765`), your
   **user name** and a **password**.
2. Click **Shared Database**, enter them and click **Connect**. The app stays signed in,
   so you don't need the password again. **Change password** sets your own;
   **Sign out** forgets the sign-in on this computer.
3. The dot next to **Shared Database** turns green while it's connected and syncing; red
   means it isn't.

While connected, your new sightings are saved both here and on the server, sold and
seen-again updates go to it too, and everyone else's sightings are copied into your
prices every few seconds. Only items from the item list are shared: a name in Review is
shared once you match it to an item. Megaphones are shared too: when several players
read the same one, the clearest read is kept. Aliases are shared as well, so a new player
starts with everything the group has taught, including phrases marked "Not an item". The
Review list itself isn't shared: it's worked out in each app from its megaphones. If the
server's admin deletes data on the server (`mfm-server purge`), your app deletes the same
from its own data the next time it syncs. The Shared Database window counts the prices,
megaphones and aliases sent, received, and removed this way ("purged"). When your app and the
server disagree, the server's copy wins. The server keeps megaphones for 72 hours;
you see the hours set in your Megaphones tab. Each time it connects, it catches up:
prices, megaphones and aliases logged while you weren't connected are sent, and anything you
missed is read from the server. It stays connected when you restart the app;
**Stop sharing** disconnects. Everything you send is marked with your account, so use
your own.

## Import and export

- **Export prices** saves a CSV of market prices (one row per item) or every sighting.
- **Import prices** loads reference prices from a CSV with the columns `item,price`, for
  example prices you copied from meso.watch.
- **Settings → Game data:** **Update** downloads the latest item and map lists (and item
  icons) from NiaMeowDB, with OSMS Data Explorer as a backup. **Import** / **Export**
  saves or restores each list; an item list exported as **.zip** includes every icon.

Exported files get the date and time in their name, e.g.
`mfm-tracker market prices 2026-10-13 08-11-00.csv`.

## Settings

Settings has three tabs: **General** (capturing, market prices, data retention, the
channel and updates), **Reading** (Window detection and OCR training) and **Game data** (the item and
map lists). Rest the mouse on any setting for a short explanation.

| Setting | Default | What it does |
|---|---|---|
| Capture every (seconds) | 0.1 | how often the screen is captured while monitoring (the reading catches up at its own pace) |
| Frames read at once | 3 | while you scroll, this many captured frames are read at the same time |
| Frames to confirm a row | 2 | identical reads needed before a listing is saved |
| Extra reads for names not in database | 2 | extra reads before an unknown name is saved |
| Empty frames = shop closed | 3 | reads without a shop before it counts as closed |
| Market median window (days) | 14 | how many days of sightings make the market price |
| OCR upscale factor | 3 | raise it if small text is misread |
| Required sightings before judging | 5 | sightings needed before an item gets a market price, DEAL / HIGH, and outliers |
| Flag DEAL / HIGH at ± % | 15 | how far from the market price a listing must be |
| Outliers: ceiling / floor | 200% / 75% | how far above / below the usual price counts as an outlier |
| Ask for the channel at launch and after a channel change | on | the channel prompt |
| Channels in the channel picker | 40 | buttons in the channel picker |
| Hotkeys | off (F5, F4, F3) | keys that work while the game has focus, see below |
| Data retention | 30 days, archive | see below |

Megaphones and shop names are read with the app's own OCR model (trained on the game's
chat and shops) together with Tesseract's standard English one. On lines it hadn't seen,
it got about 0.8% of characters wrong instead of 5.6%. To use only the standard
model, put `"trained_ocr": false` in `settings/config.json`.

**OCR training** helps make that model better from what your app reads.
- **Collecting:** under **Collect for training the OCR**, tick what to keep:
  - **Chat lines:** a picture of each megaphone line the app reads, with what it read.
  - **Store names:** each shop's title (the white bar at the top of the shop window), once
    per shop.
  - **Player names:** each shop owner's name (the dark box under the title), once per shop.

  Up to 2,500 pictures are kept in all (about 45 MB). They include player names from
  public chat and shops.
- **Automatic text:** a couple of minutes after a line was last read, the app works out
  its text. If the line was read the same way several times, or the standard English
  model read it exactly like the app's own model, that's its text ("agreed"). Reads with
  the OCR's usual slips (g read as q, a stray `|` or `?`, an item name that isn't on the
  item list) are never agreed. A store or player name is the same picture every time, so
  it's agreed when the standard model reads it exactly like the app's own model; a player
  name is also agreed when the same name was read in a megaphone.
- **Check lines:** the other lines wait in **Check lines** (**Show** picks chat lines,
  store names or player names). Each shows the picture and its likeliest text. Fix the text so it matches the picture exactly and press **Enter**, or
  **Skip** a line you can't read. You can also look through the agreed ones and fix them.
- **Sending:** with **Send them to the Shared Database server** on (and connected to a
  server 2.5.0 or newer; 2.7.0 or newer for store and player names), agreed and checked
  lines go to your server. Its admin exports
  them with `mfm-server export-ocr` for the next training run. Lines still to check
  aren't sent.

**Hotkeys** are off at first: tick **Keys that work while the game has focus** in
**Settings → General → Hotkeys** to use them. They do their action in whichever window
has focus, the game's included: **F5**
starts and stops monitoring, **F4** and **F3** set the channel to the next and previous
one (the same as ‹ › on the widget), so the app's channel keeps up with
the game without leaving it. Click a key to change it, then press the new one (with Ctrl,
Alt or Shift if you like); × leaves an action without a key. The game sees the keys too,
so pick ones it doesn't use. On Wayland they work while an X11 window has focus, like the
game run through Wine or Proton. From source, they need `pip install pynput`.

**Data retention:** listings not seen for 30 days are copied to an archive file and then
removed (or just deleted, or kept forever). This runs at start, every 6 hours, and when you
click **Clean up now**. A red warning appears if the price database grows past 100 MB.

## Your data

Everything is kept in one folder: `%APPDATA%\mfm-tracker` (Windows),
`~/Library/Application Support/mfm-tracker` (macOS) or `~/.local/share/mfm-tracker`
(Linux). **Settings → Open data folder** opens it.

| Folder | Contents |
|---|---|
| `prices/` | your prices and megaphones (`prices.db`) |
| `inventory/` | My Inventory (`inventory.db`) |
| `settings/` | settings, watched items and listings, ignore list, manual matches, Shared Database sign-in |
| `cache/` | downloaded item and map lists, icons |
| `archives/` | old listings moved out by data retention |
| `backups/` | copies made before anything is cleared |

**Settings → Clear pricing data** and **Reset personal settings** ask twice and save a
backup first.

## Command line

The app and `mfm-tracker` (from source) also take commands:
```bash
mfm-tracker deals                   # recent listings ranked against the market price
mfm-tracker item "dark scroll"      # one item's price history
mfm-tracker watch --channel 5       # monitor in the terminal
mfm-tracker report                  # an HTML report
mfm-tracker export prices.csv       # export prices (--all for every sighting)
mfm-tracker import-ref prices.csv   # import reference prices
mfm-tracker meowdb                  # update the item and map lists
mfm-tracker where                   # show the data folder
mfm-tracker --help                  # every command
```
With the app, use `"Free Market Tracker" deals` and so on. From source:
`python mfm-tracker deals`.

## Credits

Also in the app: **Settings → About**.

**Created by**

- Jayee (Discord: @jayee88)

**Made with**

- [Claude Opus 5.5 (claude-opus-5-5) by Anthropic](https://www.anthropic.com/claude): AI model that wrote the code, tests and documentation, working with you in Claude

**Game data**

- [NiaMeowDB (meowdb.com) — the meowdb team](https://meowdb.com/msclassic/): Item and map lists, item ids and icons for MapleStory Classic World
- [OSMS Data Explorer (osmsdataexplorer.com) — @ohmi](https://osmsdataexplorer.com/): Backup item and map lists and icons, mined from MapleStory Classic World

**Open source**

- [Tesseract OCR 5.3.4](https://github.com/tesseract-ocr/tesseract): reads the text in shop windows · Apache-2.0
- [Tesseract English data (tessdata_fast)](https://github.com/tesseract-ocr/tessdata_fast): OCR language model · Apache-2.0
- The app's own OCR model (`ocr/mfm.traineddata`) is [tessdata_best](https://github.com/tesseract-ocr/tessdata_best)
  English, trained further on lines from the game's chat and shops with
  [tesstrain](https://github.com/tesseract-ocr/tesstrain) · Apache-2.0
- [Leptonica 1.87.0](https://github.com/DanBloomberg/leptonica): image library used by Tesseract · BSD-2-Clause
- [libpng and zlib](https://github.com/pnggroup/libpng): image compression used by Leptonica · libpng and zlib licenses
- [Python 3.13](https://www.python.org/): the language the app is written in · PSF License
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone): portable Python used to build the Linux app · MPL-2.0
- [Tcl/Tk 9.0.3](https://github.com/tcltk/tk): the window toolkit (tkinter) · Tcl/Tk license (BSD-style)
- [libXft 2.3.9](https://github.com/openbsd/xenocara): smooth fonts for Tk on Linux, from OpenBSD's xenocara · HPND (MIT-style)
- [Pillow](https://github.com/python-pillow/Pillow): image processing · MIT-CMU (HPND)
- [pytesseract](https://github.com/madmaze/pytesseract): Python bridge to Tesseract · Apache-2.0
- [python-mss](https://github.com/BoboTiG/python-mss): screen capture · MIT
- [pyspellchecker](https://github.com/barrust/pyspellchecker): English word list for shop titles · MIT
- [SQLite](https://www.sqlite.org/): the price database · public domain
- [Inter typeface](https://github.com/rsms/inter): the app's font · SIL Open Font License 1.1
- [PyInstaller 6.22 (with pyinstaller-hooks-contrib, altgraph)](https://github.com/pyinstaller/pyinstaller): packs everything into one app · GPL-2.0 with bootloader exception, Apache-2.0, MIT
- [pynput](https://github.com/moses-palmer/pynput): the hotkeys (keys that work while the game has focus) and the command-line `hotkey` mode · LGPL-3.0

MapleStory is a trademark of NEXON. This is an unofficial fan tool, not affiliated with or endorsed by NEXON, NiaMeowDB or OSMS Data Explorer.

## License

Free Market Tracker is free software under the [GNU General Public License v3.0](LICENSE). The open-source components listed above keep their own licenses.
