#!/usr/bin/env python3
"""
M&A Deal Dashboard Auto-Updater
Scrapes TRANBI / Batonz → updates GitHub Pages → notifies Telegram.
Designed to run as a daily cron job on a VPS.
"""

import requests
import json
import base64
import time
import re
import logging
import sys
from datetime import datetime
from bs4 import BeautifulSoup

# ─── Config ───
try:
    from config import (
        GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH, GITHUB_DATA_PATH,
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    )
except ImportError:
    print("ERROR: config.py not found. Copy config.example.py → config.py and fill in credentials.")
    sys.exit(1)

# ─── Logging ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("updater.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── HTTP Session ───
S = requests.Session()
S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
})
S.timeout = 20

DELAY = 0.6          # seconds between requests
BATCH_DELAY = 2.0    # pause every N requests
BATCH_SIZE = 5


# ════════════════════════════════════════
#  GitHub API
# ════════════════════════════════════════

def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

def github_get_file():
    """Download deals_data.json from the repo. Returns (dict, sha)."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_DATA_PATH}"
    r = requests.get(url, headers=gh_headers(), params={"ref": GITHUB_BRANCH}, timeout=30)
    r.raise_for_status()
    blob = r.json()
    content = base64.b64decode(blob["content"]).decode("utf-8")
    return json.loads(content), blob["sha"]

def github_push(data_dict, sha, message):
    """Commit updated JSON back to the repo."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_DATA_PATH}"
    encoded = base64.b64encode(
        json.dumps(data_dict, ensure_ascii=False).encode("utf-8")
    ).decode()
    r = requests.put(url, headers=gh_headers(), json={
        "message": message,
        "content": encoded,
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }, timeout=30)
    r.raise_for_status()
    log.info("GitHub push OK")


# ════════════════════════════════════════
#  Telegram
# ════════════════════════════════════════

def tg_send(text):
    """Send an HTML-formatted Telegram message."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    r.raise_for_status()


# ════════════════════════════════════════
#  Date helpers
# ════════════════════════════════════════

def normalise_date(raw):
    """'2025/6/3' or '2025-06-03' → '2025-06-03'"""
    if not raw:
        return ""
    raw = raw.replace("/", "-")
    parts = raw.split("-")
    if len(parts) == 3:
        return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    return raw


# ════════════════════════════════════════
#  Batonz
# ════════════════════════════════════════

def batonz_check(deal_id):
    """
    Fetch a Batonz detail page.
    Returns (status, extra_data)
      status: 'active' | 'deleted' | 'closed' | 'error'
      extra_data: dict with updated fields (only when active)
    """
    url = f"https://batonz.jp/sell_cases/{deal_id}"
    try:
        r = S.get(url)
        if r.status_code == 404:
            return "deleted", None
        r.raise_for_status()
        html = r.text

        if "ご指定のページが見つかりません" in html:
            return "deleted", None
        if "この案件の募集は終了しています" in html:
            return "closed", None

        upd = ""
        m = re.search(r"最終更新日[：:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})", html)
        if m:
            upd = normalise_date(m.group(1))

        return "active", {"upd": upd}
    except Exception as e:
        log.warning("Batonz %s error: %s", deal_id, e)
        return "error", None


def batonz_scrape_new(deal_id):
    """Scrape full deal info from a Batonz detail page. Returns dict or None."""
    url = f"https://batonz.jp/sell_cases/{deal_id}"
    try:
        r = S.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        html = r.text
        if "ご指定のページが見つかりません" in html or "この案件の募集は終了しています" in html:
            return None

        soup = BeautifulSoup(html, "lxml")

        # title
        title_el = soup.select_one("h1.sellCaseTitle, h1.case-title, h1")
        title = title_el.get_text(strip=True) if title_el else ""

        # financial details from definition-list style elements
        rev = prof = price = region = ""
        for th in soup.select("th, dt"):
            label = th.get_text(strip=True)
            val_el = th.find_next_sibling("td") or th.find_next_sibling("dd")
            if not val_el:
                continue
            val = val_el.get_text(strip=True)
            if "売上" in label:
                rev = val
            elif "利益" in label:
                prof = val
            elif "譲渡" in label and ("価格" in label or "額" in label):
                price = val
            elif "所在" in label or "都道府県" in label or "エリア" in label or "地域" in label:
                region = val

        upd = ""
        m = re.search(r"最終更新日[：:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})", html)
        if m:
            upd = normalise_date(m.group(1))

        return {
            "id": str(deal_id), "title": title,
            "rev": rev, "prof": prof, "price": price,
            "region": region, "industry": "",
            "source": "batonz", "pub": "", "upd": upd,
        }
    except Exception as e:
        log.warning("Batonz scrape %s failed: %s", deal_id, e)
        return None


def batonz_scan_listing(existing_ids, max_pages=15):
    """Return list of new deal IDs found on the Batonz listing pages."""
    new_ids = []
    for page in range(1, max_pages + 1):
        try:
            r = S.get(f"https://batonz.jp/sell_cases", params={"page": page})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")

            links = soup.select('a[href*="/sell_cases/"]')
            page_new = 0
            for a in links:
                m = re.search(r"/sell_cases/(\d+)", a.get("href", ""))
                if m and m.group(1) not in existing_ids:
                    new_ids.append(m.group(1))
                    page_new += 1

            log.info("Batonz listing p%d → %d new IDs", page, page_new)

            # Stop if no next-page link
            next_link = soup.select_one('a[rel="next"], li.next a, a.next_page')
            if not next_link:
                break
            time.sleep(DELAY)
        except Exception as e:
            log.warning("Batonz listing p%d error: %s", page, e)
            break

    return list(dict.fromkeys(new_ids))  # dedupe, keep order


# ════════════════════════════════════════
#  TRANBI
# ════════════════════════════════════════

def tranbi_check(deal_id):
    """Check a TRANBI deal. Returns (status, extra_data)."""
    url = f"https://www.tranbi.com/buy/detail/?id={deal_id}"
    try:
        r = S.get(url)
        if r.status_code == 404:
            return "deleted", None
        r.raise_for_status()
        html = r.text

        if "ご指定のページが見つかりません" in html or "ページが見つかりません" in html:
            return "deleted", None
        if "success__message" in html or "この案件の募集は終了しています" in html:
            return "closed", None

        pub = upd = ""
        m = re.search(r"公開日[：:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})", html)
        if m:
            pub = normalise_date(m.group(1))
        m = re.search(r"更新日[：:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})", html)
        if m:
            upd = normalise_date(m.group(1))

        return "active", {"pub": pub, "upd": upd}
    except Exception as e:
        log.warning("TRANBI %s error: %s", deal_id, e)
        return "error", None


def tranbi_scrape_new(deal_id):
    """Scrape full deal info from a TRANBI detail page."""
    url = f"https://www.tranbi.com/buy/detail/?id={deal_id}"
    try:
        r = S.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        html = r.text
        if "success__message" in html or "この案件の募集は終了しています" in html:
            return None
        if "ご指定のページが見つかりません" in html or "ページが見つかりません" in html:
            return None

        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one("h1")
        title = title_el.get_text(strip=True) if title_el else ""

        rev = prof = price = region = ""
        for th in soup.select("th, dt"):
            label = th.get_text(strip=True)
            val_el = th.find_next_sibling("td") or th.find_next_sibling("dd")
            if not val_el:
                continue
            val = val_el.get_text(strip=True)
            if "売上高" in label:
                rev = val
            elif "営業利益" in label:
                prof = val
            elif "譲渡希望" in label:
                price = val
            elif "所在地" in label or "エリア" in label:
                region = val

        pub = upd = ""
        m = re.search(r"公開日[：:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})", html)
        if m:
            pub = normalise_date(m.group(1))
        m = re.search(r"更新日[：:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})", html)
        if m:
            upd = normalise_date(m.group(1))

        return {
            "id": str(deal_id), "title": title,
            "rev": rev, "prof": prof, "price": price,
            "region": region, "industry": "",
            "source": "tranbi", "pub": pub, "upd": upd,
        }
    except Exception as e:
        log.warning("TRANBI scrape %s failed: %s", deal_id, e)
        return None


def tranbi_scan_listing(existing_ids, max_pages=15):
    """Return list of new deal IDs from TRANBI listing pages."""
    new_ids = []
    for page in range(1, max_pages + 1):
        try:
            r = S.get("https://www.tranbi.com/buy/list/", params={"page": page})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")

            links = soup.select('a[href*="/buy/detail/"]')
            page_new = 0
            for a in links:
                m = re.search(r"[?&]id=(\d+)", a.get("href", ""))
                if m and m.group(1) not in existing_ids:
                    new_ids.append(m.group(1))
                    page_new += 1

            log.info("TRANBI listing p%d → %d new IDs", page, page_new)

            # Check for next page link
            next_link = soup.find("a", string=re.compile(r"次へ"))
            if not next_link:
                break
            time.sleep(DELAY)
        except Exception as e:
            log.warning("TRANBI listing p%d error: %s", page, e)
            break

    return list(dict.fromkeys(new_ids))


# ════════════════════════════════════════
#  Core pipeline
# ════════════════════════════════════════

def check_existing(deals, check_fn, label):
    """
    Run check_fn on every deal.
    Returns (active_deals, removed_dict, error_count).
    """
    active = []
    removed = {"deleted": [], "closed": []}
    errors = 0

    for i, deal in enumerate(deals):
        # rate-limit
        if i > 0:
            if i % BATCH_SIZE == 0:
                time.sleep(BATCH_DELAY)
            else:
                time.sleep(DELAY)

        status, extra = check_fn(deal["id"])

        if status == "active":
            if extra:
                for k, v in extra.items():
                    if v:
                        deal[k] = v
            active.append(deal)
        elif status in ("deleted", "closed"):
            removed[status].append(deal)
            log.info("%s %s %s: %s", label, deal["id"], status, deal["title"][:40])
        else:
            errors += 1
            active.append(deal)  # keep on error

        if (i + 1) % 20 == 0:
            log.info("%s progress: %d / %d", label, i + 1, len(deals))

    return active, removed, errors


def scrape_new_deals(new_ids, scrape_fn, label):
    """Scrape details for a list of new deal IDs."""
    deals = []
    for i, did in enumerate(new_ids):
        if i > 0:
            if i % BATCH_SIZE == 0:
                time.sleep(BATCH_DELAY)
            else:
                time.sleep(DELAY)
        d = scrape_fn(did)
        if d:
            deals.append(d)
            log.info("%s new deal: %s %s", label, did, d["title"][:30])
    return deals


# ════════════════════════════════════════
#  Notification builder
# ════════════════════════════════════════

def build_notification(stats):
    """Build an HTML-formatted Telegram message."""
    s = stats
    lines = [
        f'📊 <b>M&A Dashboard 更新完了</b>',
        f'📅 {s["date"]}  ⏱ {s["elapsed"]}秒',
        "",
        f'📈 合計: <b>{s["total"]}</b> 件',
        f'  ├ Batonz: {s["batonz_count"]}',
        f'  └ TRANBI: {s["tranbi_count"]}',
    ]

    if s["new_batonz"] or s["new_tranbi"]:
        lines.append("")
        lines.append(f'🆕 新規: {len(s["new_batonz"]) + len(s["new_tranbi"])} 件')
        for d in s["new_batonz"][:5]:
            lines.append(f'  • <a href="https://batonz.jp/sell_cases/{d["id"]}">{d["title"][:35]}</a>')
        for d in s["new_tranbi"][:5]:
            lines.append(f'  • <a href="https://www.tranbi.com/buy/detail/?id={d["id"]}">{d["title"][:35]}</a>')

    rm_total = s["batonz_del"] + s["batonz_cls"] + s["tranbi_del"] + s["tranbi_cls"]
    if rm_total:
        lines.append("")
        lines.append(f'🗑 除外: {rm_total} 件')
        for d in s["removed_list"][:8]:
            lines.append(f'  [{d["reason"]}] {d["title"][:35]}')

    if s["errors"]:
        lines.append("")
        lines.append(f'⚠️ チェック失敗: {s["errors"]} 件')

    lines.append("")
    lines.append('🔗 <a href="https://ashlynx.github.io/ma-deal-dashboard/">ダッシュボード</a>')

    return "\n".join(lines)


# ════════════════════════════════════════
#  Main
# ════════════════════════════════════════

def main():
    log.info("=" * 50)
    log.info("M&A Deal Dashboard Update — START")
    t0 = time.time()

    # 1) Fetch current data from GitHub
    log.info("Fetching data from GitHub …")
    try:
        data, sha = github_get_file()
    except Exception as e:
        log.error("GitHub fetch failed: %s", e)
        tg_send(f"❌ Dashboard更新失敗\nGitHubデータ取得エラー:\n{e}")
        return

    batonz = data.get("batonz", [])
    tranbi = data.get("tranbi", [])
    log.info("Current data: Batonz %d, TRANBI %d", len(batonz), len(tranbi))

    # 2) Check existing deals
    log.info("── Checking Batonz deals ──")
    bz_active, bz_rm, bz_err = check_existing(batonz, batonz_check, "Batonz")

    log.info("── Checking TRANBI deals ──")
    tr_active, tr_rm, tr_err = check_existing(tranbi, tranbi_check, "TRANBI")

    # 3) Scan listing pages for new deals
    log.info("── Scanning Batonz listings ──")
    bz_existing_ids = {d["id"] for d in bz_active}
    bz_new_ids = batonz_scan_listing(bz_existing_ids)
    log.info("Batonz new IDs found: %d", len(bz_new_ids))

    log.info("── Scanning TRANBI listings ──")
    tr_existing_ids = {d["id"] for d in tr_active}
    tr_new_ids = tranbi_scan_listing(tr_existing_ids)
    log.info("TRANBI new IDs found: %d", len(tr_new_ids))

    # 4) Scrape new deal details
    new_bz = scrape_new_deals(bz_new_ids, batonz_scrape_new, "Batonz")
    new_tr = scrape_new_deals(tr_new_ids, tranbi_scrape_new, "TRANBI")

    # 5) Merge
    bz_final = bz_active + new_bz
    tr_final = tr_active + new_tr

    now = datetime.now().strftime("%Y-%m-%d")
    updated = {
        "published_date": data.get("published_date", now),
        "updated_date": now,
        "batonz": bz_final,
        "tranbi": tr_final,
    }

    # 6) Push to GitHub
    rm_total = (len(bz_rm["deleted"]) + len(bz_rm["closed"])
                + len(tr_rm["deleted"]) + len(tr_rm["closed"]))
    new_total = len(new_bz) + len(new_tr)

    parts = []
    if new_total:
        parts.append(f"+{new_total}新規")
    if rm_total:
        parts.append(f"-{rm_total}除外")
    if not parts:
        parts.append("日次チェック")
    commit_msg = f"Auto-update: {', '.join(parts)} ({now})"

    try:
        github_push(updated, sha, commit_msg)
    except Exception as e:
        log.error("GitHub push failed: %s", e)
        tg_send(f"❌ Dashboard更新失敗\nGitHub pushエラー:\n{e}")
        return

    # 7) Build stats & notify
    elapsed = int(time.time() - t0)

    removed_list = []
    for d in bz_rm["deleted"]:
        removed_list.append({"title": d["title"], "reason": "Batonz 削除"})
    for d in bz_rm["closed"]:
        removed_list.append({"title": d["title"], "reason": "Batonz 終了"})
    for d in tr_rm["deleted"]:
        removed_list.append({"title": d["title"], "reason": "TRANBI 削除"})
    for d in tr_rm["closed"]:
        removed_list.append({"title": d["title"], "reason": "TRANBI 終了"})

    stats = {
        "date": now,
        "elapsed": elapsed,
        "total": len(bz_final) + len(tr_final),
        "batonz_count": len(bz_final),
        "tranbi_count": len(tr_final),
        "new_batonz": new_bz,
        "new_tranbi": new_tr,
        "batonz_del": len(bz_rm["deleted"]),
        "batonz_cls": len(bz_rm["closed"]),
        "tranbi_del": len(tr_rm["deleted"]),
        "tranbi_cls": len(tr_rm["closed"]),
        "removed_list": removed_list,
        "errors": bz_err + tr_err,
    }

    msg = build_notification(stats)
    try:
        tg_send(msg)
        log.info("Telegram notification sent")
    except Exception as e:
        log.error("Telegram send failed: %s", e)

    log.info("DONE — total %d deals, %d new, %d removed, %ds",
             stats["total"], new_total, rm_total, elapsed)
    log.info("=" * 50)


if __name__ == "__main__":
    main()
