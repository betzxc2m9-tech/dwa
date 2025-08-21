import asyncio
import re
import sys
import os
import csv
import threading
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any, Tuple

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception:
    # Allow headless environments
    tk = None  # type: ignore
    ttk = None  # type: ignore
    messagebox = None  # type: ignore
    filedialog = None  # type: ignore

import aiohttp


ROBLOX_GROUP_API = "https://groups.roblox.com/v1/groups/{groupId}"
ROBLOX_GROUP_ROLES_API = "https://groups.roblox.com/v1/groups/{groupId}/roles"
ROBLOX_GROUP_ROLE_USERS_API = (
    "https://groups.roblox.com/v1/groups/{groupId}/roles/{roleId}/users?sortOrder=Asc&limit=100{cursor}"
)
ROBLOX_GROUP_SOCIALS_API = "https://groups.roblox.com/v1/groups/{groupId}/social-links"
ROBLOX_USER_PROFILE_API = "https://users.roblox.com/v1/users/{userId}"
ROBLOX_USER_COLLECTIBLES_API = (
    "https://inventory.roblox.com/v1/users/{userId}/assets/collectibles?sortOrder=Asc&limit=100{cursor}"
)


@dataclass
class UserWealth:
    user_id: int
    username: str
    display_name: str
    rap_total: int
    limited_count: int
    discord_matches: List[str]
    profile_url: str


class RobloxCommunityScraper:
    def __init__(
        self,
        max_concurrency: int = 20,
        request_timeout_sec: int = 20,
        max_retries: int = 5,
        log_fn=lambda msg: None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_sec)
        self._max_retries = max_retries
        self._session: Optional[aiohttp.ClientSession] = None
        self._log = log_fn

    async def __aenter__(self) -> "RobloxCommunityScraper":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()

    async def _request_json(self, url: str) -> Dict[str, Any]:
        assert self._session is not None
        headers = {
            "Accept": "application/json",
            "User-Agent": "RobloxCommunityScraper/1.0 (+https://github.com)"
        }
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._semaphore:
                    async with self._session.get(url, headers=headers) as resp:
                        if resp.status == 429:
                            # Rate limited; back off
                            retry_after = float(resp.headers.get("Retry-After", "0"))
                            delay = max(retry_after, min(2 ** attempt, 10))
                            self._log(f"429 from {url}. Backing off {delay:.1f}s (attempt {attempt})")
                            await asyncio.sleep(delay)
                            continue
                        if 500 <= resp.status < 600:
                            delay = min(2 ** attempt, 10)
                            self._log(f"{resp.status} from {url}. Retry in {delay:.1f}s (attempt {attempt})")
                            await asyncio.sleep(delay)
                            continue
                        resp.raise_for_status()
                        return await resp.json()
            except Exception as e:
                last_exc = e
                delay = min(2 ** attempt, 10)
                self._log(f"Error fetching {url}: {e}. Retry in {delay:.1f}s (attempt {attempt})")
                await asyncio.sleep(delay)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Failed to fetch {url}")

    @staticmethod
    def extract_group_id_from_url(url: str) -> Optional[int]:
        patterns = [
            r"communities/(\d+)",
            r"groups/(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return int(m.group(1))
        return None

    async def fetch_group_info(self, group_id: int) -> Dict[str, Any]:
        return await self._request_json(ROBLOX_GROUP_API.format(groupId=group_id))

    async def fetch_group_discord(self, group_id: int) -> Optional[str]:
        try:
            data = await self._request_json(ROBLOX_GROUP_SOCIALS_API.format(groupId=group_id))
            for item in data.get("data", []):
                url = item.get("url", "")
                if self._looks_like_discord_link(url):
                    return url
        except Exception as e:
            self._log(f"Failed to fetch group socials: {e}")
        # Fallback: try group description
        try:
            info = await self.fetch_group_info(group_id)
            desc = info.get("description", "") or ""
            discord_links = self._find_discord_links(desc)
            if discord_links:
                return discord_links[0]
        except Exception:
            pass
        return None

    async def fetch_group_members(self, group_id: int, max_members: Optional[int] = None) -> List[Dict[str, Any]]:
        roles_data = await self._request_json(ROBLOX_GROUP_ROLES_API.format(groupId=group_id))
        roles = roles_data.get("roles", [])
        all_members: List[Dict[str, Any]] = []
        for role in roles:
            role_id = role.get("id")
            if role_id is None:
                continue
            cursor: Optional[str] = None
            while True:
                cursor_q = f"&cursor={cursor}" if cursor else ""
                url = ROBLOX_GROUP_ROLE_USERS_API.format(groupId=group_id, roleId=role_id, cursor=cursor_q)
                data = await self._request_json(url)
                users = data.get("data", [])
                all_members.extend(users)
                if max_members is not None and len(all_members) >= max_members:
                    return all_members[:max_members]
                cursor = data.get("nextPageCursor")
                if not cursor:
                    break
        return all_members

    async def fetch_user_profile(self, user_id: int) -> Dict[str, Any]:
        return await self._request_json(ROBLOX_USER_PROFILE_API.format(userId=user_id))

    async def fetch_user_collectibles(self, user_id: int) -> Tuple[int, int]:
        total_rap = 0
        count = 0
        cursor: Optional[str] = None
        while True:
            cursor_q = f"&cursor={cursor}" if cursor else ""
            url = ROBLOX_USER_COLLECTIBLES_API.format(userId=user_id, cursor=cursor_q)
            data = await self._request_json(url)
            for item in data.get("data", []):
                rap = item.get("recentAveragePrice")
                if isinstance(rap, (int, float)):
                    total_rap += int(rap)
                count += 1
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
        return total_rap, count

    @staticmethod
    def _looks_like_discord_link(text: str) -> bool:
        return bool(re.search(r"discord\.(gg|com)/[\w\-]+", text, re.IGNORECASE))

    @staticmethod
    def _find_discord_links(text: str) -> List[str]:
        links = re.findall(r"https?://(?:discord\.gg|discord\.com/invite)/[\w\-]+", text, flags=re.IGNORECASE)
        return list(dict.fromkeys(links))

    @staticmethod
    def _find_discord_handles(text: str) -> List[str]:
        if not text:
            return []
        matches: List[str] = []
        # Invite links
        matches.extend(RobloxCommunityScraper._find_discord_links(text))
        # Legacy discriminator pattern name#1234
        matches.extend(re.findall(r"\b[\w.]{2,32}#\d{4}\b", text))
        # New username style (best effort): look for 'discord: username' or '@username' near 'discord'
        for m in re.findall(r"discord\s*[:=\-]\s*(@?[a-z0-9_.]{2,32})", text, flags=re.IGNORECASE):
            matches.append(m)
        for m in re.findall(r"(@[a-z0-9_.]{2,32})", text, flags=re.IGNORECASE):
            # Heuristic: only keep if the word 'discord' appears nearby in a 40-char window
            idx = text.lower().find(m.lower())
            if idx != -1:
                window = text[max(0, idx - 40): idx + 40].lower()
                if "discord" in window:
                    matches.append(m)
        # Deduplicate preserving order
        deduped: List[str] = list(dict.fromkeys(matches))
        return deduped

    async def compute_leaderboard(
        self,
        members: List[Dict[str, Any]],
        top_n: int = 50,
        fetch_discord_from_bio: bool = True,
    ) -> List[UserWealth]:
        # Prepare tasks with controlled concurrency
        results: List[UserWealth] = []

        async def process_member(member: Dict[str, Any]) -> Optional[UserWealth]:
            try:
                user = member.get("user") or {}
                user_id = int(user.get("userId") or user.get("id"))
                username = user.get("username") or ""
                # Some role members API uses id/username directly
                if not username:
                    username = member.get("username", "")
                rap_total, count = await self.fetch_user_collectibles(user_id)
                profile = await self.fetch_user_profile(user_id)
                display_name = profile.get("displayName", "") or ""
                description = profile.get("description", "") or ""
                discord_matches: List[str] = []
                if fetch_discord_from_bio:
                    discord_matches = self._find_discord_handles(description)
                return UserWealth(
                    user_id=user_id,
                    username=username or profile.get("name", ""),
                    display_name=display_name,
                    rap_total=rap_total,
                    limited_count=count,
                    discord_matches=discord_matches,
                    profile_url=f"https://www.roblox.com/users/{user_id}/profile",
                )
            except Exception as e:
                self._log(f"Failed to process member: {e}")
                return None

        # Run in batches to avoid overwhelming APIs
        tasks = [process_member(m) for m in members]
        for chunk_start in range(0, len(tasks), 100):
            chunk = tasks[chunk_start:chunk_start + 100]
            chunk_results = await asyncio.gather(*chunk)
            for r in chunk_results:
                if r is not None:
                    results.append(r)
            self._log(f"Processed {min(chunk_start + 100, len(tasks))}/{len(tasks)} members...")

        results.sort(key=lambda u: u.rap_total, reverse=True)
        return results[:top_n]


class AppGUI:
    def __init__(self) -> None:
        if tk is None:
            raise RuntimeError("Tkinter GUI is not available in this environment.")
        self.root = tk.Tk()
        self.root.title("Roblox Community Leaderboard Scraper")
        self.root.geometry("1000x650")

        # Inputs frame
        frm_inputs = ttk.Frame(self.root, padding=10)
        frm_inputs.pack(fill=tk.X)

        ttk.Label(frm_inputs, text="Community URL:").grid(row=0, column=0, sticky=tk.W)
        self.entry_url = ttk.Entry(frm_inputs, width=90)
        self.entry_url.grid(row=0, column=1, sticky=tk.W)
        self.entry_url.insert(0, "https://www.roblox.com/communities/35461612/Z9-Market#!/about")

        ttk.Label(frm_inputs, text="Max members to scan:").grid(row=1, column=0, sticky=tk.W, pady=(8,0))
        self.entry_max_members = ttk.Entry(frm_inputs, width=20)
        self.entry_max_members.grid(row=1, column=1, sticky=tk.W, pady=(8,0))
        self.entry_max_members.insert(0, "500")

        ttk.Label(frm_inputs, text="Top N:").grid(row=2, column=0, sticky=tk.W, pady=(8,0))
        self.entry_top_n = ttk.Entry(frm_inputs, width=20)
        self.entry_top_n.grid(row=2, column=1, sticky=tk.W, pady=(8,0))
        self.entry_top_n.insert(0, "50")

        self.btn_run = ttk.Button(frm_inputs, text="Run Scraper", command=self.on_run)
        self.btn_run.grid(row=0, column=2, padx=(10,0))

        self.btn_export = ttk.Button(frm_inputs, text="Export CSV", command=self.on_export, state=tk.DISABLED)
        self.btn_export.grid(row=1, column=2, padx=(10,0))

        # Log box
        frm_log = ttk.Frame(self.root, padding=10)
        frm_log.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm_log, text="Log").pack(anchor=tk.W)
        self.txt_log = tk.Text(frm_log, height=8)
        self.txt_log.pack(fill=tk.X)

        # Results table
        frm_table = ttk.Frame(self.root, padding=10)
        frm_table.pack(fill=tk.BOTH, expand=True)
        columns = ("rank", "username", "display_name", "rap_total", "limited_count", "discord", "user_id")
        self.tree = ttk.Treeview(frm_table, columns=columns, show="headings")
        self.tree.heading("rank", text="Rank")
        self.tree.heading("username", text="Username")
        self.tree.heading("display_name", text="Display Name")
        self.tree.heading("rap_total", text="RAP Total")
        self.tree.heading("limited_count", text="# Limiteds")
        self.tree.heading("discord", text="Discord Matches")
        self.tree.heading("user_id", text="User ID")
        self.tree.column("rank", width=60, anchor=tk.CENTER)
        self.tree.column("username", width=160)
        self.tree.column("display_name", width=160)
        self.tree.column("rap_total", width=120, anchor=tk.E)
        self.tree.column("limited_count", width=100, anchor=tk.CENTER)
        self.tree.column("discord", width=260)
        self.tree.column("user_id", width=120, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(self.root, textvariable=self.status_var, padding=10)
        self.status_label.pack(anchor=tk.W)

        self._results: List[UserWealth] = []
        self._group_discord: Optional[str] = None

    def log(self, msg: str) -> None:
        self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)
        self.root.update_idletasks()

    def on_export(self) -> None:
        if not self._results:
            messagebox.showinfo("Export", "No results to export.")
            return
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", ".csv")],
            title="Save leaderboard as CSV",
        )
        if not file_path:
            return
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            header = [
                "Rank",
                "User ID",
                "Username",
                "Display Name",
                "RAP Total",
                "Limited Count",
                "Discord Matches",
                "Profile URL",
                "Group Discord",
            ]
            writer.writerow(header)
            for i, user in enumerate(self._results, start=1):
                writer.writerow([
                    i,
                    user.user_id,
                    user.username,
                    user.display_name,
                    user.rap_total,
                    user.limited_count,
                    "; ".join(user.discord_matches),
                    user.profile_url,
                    self._group_discord or "",
                ])
        messagebox.showinfo("Export", f"Saved to {file_path}")

    def on_run(self) -> None:
        url = self.entry_url.get().strip()
        max_members_str = self.entry_max_members.get().strip()
        top_n_str = self.entry_top_n.get().strip()
        try:
            max_members = int(max_members_str)
        except Exception:
            max_members = None
        try:
            top_n = int(top_n_str)
        except Exception:
            top_n = 50

        self.btn_run.configure(state=tk.DISABLED)
        self.btn_export.configure(state=tk.DISABLED)
        self.tree.delete(*self.tree.get_children())
        self._results = []
        self._group_discord = None
        self.status_var.set("Running...")
        self.log("Starting scraper...")

        def worker():
            try:
                asyncio.run(self._run_scraper(url, max_members, top_n))
            except Exception as e:
                self.log(f"Error: {e}")
            finally:
                self.btn_run.configure(state=tk.NORMAL)
                self.btn_export.configure(state=(tk.NORMAL if self._results else tk.DISABLED))
                self.status_var.set("Done" if self._results else "Idle")

        threading.Thread(target=worker, daemon=True).start()

    async def _run_scraper(self, url: str, max_members: Optional[int], top_n: int) -> None:
        group_id = RobloxCommunityScraper.extract_group_id_from_url(url)
        if not group_id:
            self.log("Invalid community URL. Expected format like https://www.roblox.com/communities/<id>/... ")
            return

        async with RobloxCommunityScraper(log_fn=self.log) as scraper:
            self.log(f"Fetching group info for {group_id}...")
            info = await scraper.fetch_group_info(group_id)
            group_name = info.get("name", "Unknown")
            self.log(f"Group: {group_name} (ID {group_id})")

            self._group_discord = await scraper.fetch_group_discord(group_id)
            if self._group_discord:
                self.log(f"Group Discord: {self._group_discord}")
            else:
                self.log("No group Discord link found.")

            self.log("Fetching group members across roles...")
            members = await scraper.fetch_group_members(group_id, max_members=max_members)
            self.log(f"Found {len(members)} members to process")

            leaderboard = await scraper.compute_leaderboard(members, top_n=top_n)
            self._results = leaderboard
            self._populate_table()

    def _populate_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, user in enumerate(self._results, start=1):
            self.tree.insert(
                "",
                tk.END,
                values=(
                    i,
                    user.username,
                    user.display_name,
                    f"{user.rap_total:,}",
                    user.limited_count,
                    "; ".join(user.discord_matches),
                    user.user_id,
                ),
            )


def run_gui_app() -> None:
    if tk is None:
        raise RuntimeError("Tkinter is not available.")
    app = AppGUI()
    app.root.mainloop()


async def headless_test(url: str, max_members: int = 50, top_n: int = 10) -> None:
    print("Running headless test...", flush=True)
    logs: List[str] = []
    def log_fn(msg: str) -> None:
        logs.append(msg)
        print(msg, flush=True)
    group_id = RobloxCommunityScraper.extract_group_id_from_url(url)
    if not group_id:
        print("Invalid URL provided", flush=True)
        return
    async with RobloxCommunityScraper(log_fn=log_fn) as scraper:
        info = await scraper.fetch_group_info(group_id)
        print(f"Group: {info.get('name')} ({group_id})", flush=True)
        discord = await scraper.fetch_group_discord(group_id)
        if discord:
            print(f"Discord: {discord}", flush=True)
        members = await scraper.fetch_group_members(group_id, max_members=max_members)
        print(f"Members fetched: {len(members)}", flush=True)
        leaderboard = await scraper.compute_leaderboard(members, top_n=top_n)
        for i, u in enumerate(leaderboard, start=1):
            print(f"#{i} {u.username} ({u.display_name}) RAP={u.rap_total} Limiteds={u.limited_count} Discord={'; '.join(u.discord_matches)}", flush=True)


if __name__ == "__main__":
    # If HEADLESS_TEST is set, run without GUI for CI/Container environments
    if os.environ.get("HEADLESS_TEST"):
        test_url = os.environ.get(
            "TEST_COMMUNITY_URL",
            "https://www.roblox.com/communities/35461612/Z9-Market#!/about",
        )
        try:
            test_max_members = int(os.environ.get("TEST_MAX_MEMBERS", "20"))
        except Exception:
            test_max_members = 20
        try:
            test_top_n = int(os.environ.get("TEST_TOP_N", "5"))
        except Exception:
            test_top_n = 5
        asyncio.run(headless_test(test_url, max_members=test_max_members, top_n=test_top_n))
        sys.exit(0)
    # Otherwise try to run the GUI
    if tk is None:
        print("Tkinter GUI not available in this environment. Set HEADLESS_TEST=1 to run CLI test.")
        sys.exit(1)
    run_gui_app()

