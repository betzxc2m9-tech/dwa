## Roblox Community Leaderboard Scraper

Desktop app to:

- Input a Roblox community URL (example: `https://www.roblox.com/communities/35461612/Z9-Market#!/about`)
- Fetch all members, compute a richest leaderboard by total RAP from their Limited/Collectible items
- Detect the community Discord invite link from group socials/description
- Attempt to extract Discord handles/links from top players' Roblox bios
- Display results and export to CSV

### Run locally (GUI)

Requirements: Python 3.10+ with Tkinter installed

```bash
pip install -r requirements.txt
python app.py
```

If you are in a headless environment (no display), run a quick test without the GUI:

```bash
HEADLESS_TEST=1 python app.py
```

### Build a Windows .exe

PyInstaller cannot cross-compile reliably. Build on Windows:

```bash
py -m pip install -r requirements.txt
py -m PyInstaller --noconsole --onefile --name RobloxCommunityScraper app.py
```

Your exe will be at `dist/RobloxCommunityScraper.exe`.

### Notes

- The app uses public Roblox APIs: group info/roles/members, users, and collectibles inventory.
- RAP is computed as the sum of `recentAveragePrice` across all collectible assets owned by a user.
- Discord handle extraction from bios is best-effort and may miss some formats.
- Large groups can take time to scan; you can limit the number of members to process.
