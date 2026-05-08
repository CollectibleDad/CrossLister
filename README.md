# CrossLister

Automated reseller tool that identifies trading cards and collectibles from photos, prices them using real eBay data, and lists them on Mercari, Depop, and eBay — automatically.

Works for: **Pokemon, Sports Cards, MTG, Yu-Gi-Oh, Hot Wheels, and any collectible.**

---

## What It Does

1. You drop card photos into a folder
2. CrossLister identifies each card using AI (runs on your computer, no internet needed)
3. It looks up recent sold prices on eBay to set a smart price
4. It automatically creates listings on Mercari, Depop, and eBay
5. Every day it checks if anything sold — if it did, it removes the listing from the other sites
6. It generates a daily report showing what sold and what's new

---

## Requirements

- Windows 10 or 11
- Python 3.11 or newer — [python.org/downloads](https://www.python.org/downloads/)
- Google Chrome browser
- Ollama (free, runs AI locally) — [ollama.com](https://ollama.com)
- Active accounts on Mercari, Depop, and eBay (logged in on Chrome)

---

## Setup — Do This Once

### Step 1: Install Python

Download Python from [python.org/downloads](https://www.python.org/downloads/).  
During install, **check the box that says "Add Python to PATH"**.

### Step 2: Install Ollama and the llava model

1. Download Ollama from [ollama.com](https://ollama.com) and install it
2. Open Command Prompt and run:
   ```
   ollama pull llava
   ```
   This downloads the AI that reads card images. Takes a few minutes.

### Step 3: Install CrossLister dependencies

Open Command Prompt, navigate to the CrossLister folder, and run:

```
cd C:\Users\mrozo\Desktop\CrossLister
pip install -r requirements.txt
playwright install chromium
```

### Step 4: Set up Chrome for automation

CrossLister needs to control your Chrome browser where you're already logged in.

Create a shortcut (or run this in Command Prompt) to start Chrome in automation mode:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\ChromeData
```

> **Important:** Log in to Mercari, Depop, and eBay in this Chrome window before running CrossLister.

To make this easy, create a file called `StartChrome.bat` on your Desktop with that line in it. Double-click it each morning before running CrossLister.

### Step 5: Create your card input folder

Create this folder if it doesn't exist:
```
C:\Users\mrozo\OneDrive\Desktop\CardsToList
```

Photos you put here will be automatically processed.

### Step 6: Schedule daily automation (optional)

Right-click Command Prompt → "Run as Administrator", then:

```
cd C:\Users\mrozo\Desktop\CrossLister
python scheduler.py install
```

This makes CrossLister run every day at 9:00 AM automatically.

---

## Daily Use

### To list new cards:

1. Photograph your cards and put the photos in:
   ```
   C:\Users\mrozo\OneDrive\Desktop\CardsToList
   ```
2. Start Chrome (double-click `StartChrome.bat` if you made one)
3. Make sure Ollama is running (`ollama serve` in Command Prompt)
4. Run CrossLister:
   ```
   cd C:\Users\mrozo\Desktop\CrossLister
   python main.py
   ```

That's it! CrossLister will identify each card, price it, list it everywhere, and move the photos to the `Processed` folder when done.

### To just check sales (without listing new items):

```
python sales_checker.py
```

### To generate a report:

```
python report.py
```

### To export your full inventory as a spreadsheet:

```
python report.py --csv
```

---

## Photo Tips

- One card per photo
- Lay the card flat on a plain background
- Good lighting — no glare
- Capture the full card including edges
- Supported formats: JPG, PNG, WEBP, BMP, TIFF

---

## Folder Structure After Setup

```
C:\Users\mrozo\Desktop\CrossLister\
├── main.py              ← Run this to do everything
├── crosslister.db       ← Your inventory database (auto-created)
├── logs\                ← Daily log files
├── reports\             ← Daily report files

C:\Users\mrozo\OneDrive\Desktop\CardsToList\
├── (drop your card photos here)
├── Processed\           ← Photos move here after listing
├── Failed\              ← Photos that couldn't be identified
```

---

## Pricing

CrossLister calculates prices from real eBay data:
- Looks at the last 5 **sold** listings (60% weight)
- Looks at the 3 closest **active** listings (40% weight)
- Minimum price is **$1.22** (never lists for less)
- Depop listings are always **$2.00** (flat rate for quick sales)

---

## Troubleshooting

**"Cannot connect to Chrome on port 9222"**  
→ Chrome isn't running in automation mode. Run the StartChrome command above.

**"Ollama not reachable"**  
→ Run `ollama serve` in a Command Prompt window and leave it open.

**"llava model not found"**  
→ Run `ollama pull llava` in Command Prompt.

**Card identified incorrectly**  
→ Open the database with [DB Browser for SQLite](https://sqlitebrowser.org/) (free) and edit the `crosslister.db` file to correct any fields before re-listing.

**Listing failed on one platform**  
→ Check the `logs\` folder for details. The item will be in `Failed\` if all platforms failed, or still active on the platforms that succeeded.

---

## Database Fields

| Field | Description |
|-------|-------------|
| card_name | Name of the card |
| card_number | Number (e.g. 4/102) |
| set_name | Set or series name |
| rarity | Holo Rare, Common, etc. |
| card_type | Pokemon, Sports, MTG, YuGiOh, HotWheels, Other |
| condition | Near Mint, Lightly Played, etc. |
| mercari_id | Mercari listing ID |
| depop_id | Depop listing ID |
| ebay_id | eBay item number |
| asking_price | Listed price |
| sold_price | What it actually sold for |
| platform_sold | Where it sold |
| status | pending / active / sold / deleted / error |

---

## GitHub

[github.com/CollectibleDad/CrossLister](https://github.com/CollectibleDad/CrossLister)

---

## Notes

- CrossLister uses your existing Chrome login sessions — it never stores your passwords
- All AI identification runs locally on your computer via Ollama — no data is sent to the cloud
- eBay price data is scraped from public listings — no API key needed
- The database file `crosslister.db` is your master record — back it up regularly
